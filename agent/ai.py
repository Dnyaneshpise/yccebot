"""OpenRouter (OpenAI-compatible) LLM helpers.

The model is fully configurable via ``OPENROUTER_MODEL`` so the agent keeps
working when free model availability changes. Every failure is surfaced as an
``AIError`` with a sanitized message - never a raw API key or request body.
"""

from __future__ import annotations

import json
import re
from urllib import error, request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Sent as HTTP-Referer / X-Title, which OpenRouter uses for attribution.
APP_REFERER = "https://github.com/aws-builder-badge-agent"
APP_TITLE = "AWS Builder Badge Agent"

DEFAULT_TIMEOUT = 90

SYSTEM_PROMPT = (
    "You are an AWS technical writing assistant helping a developer write "
    "genuinely useful contributions to the AWS Builder Center community. "
    "Rules you must always follow:\n"
    "1. Never invent experiments, benchmarks, deployments, employers, job "
    "titles, certifications, or personal experiences. Only reason from the "
    "provided text and general, public AWS knowledge.\n"
    "2. Never claim the user used or deployed an AWS service unless the "
    "provided context says so. Always phrase unknown specifics as questions or "
    "conditional reasoning ('if the workload uses X, then ...').\n"
    "3. Never ask for likes, upvotes, follows, or reciprocal engagement.\n"
    "4. Never mention badges, points, rewards, or milestones as a reason for "
    "posting.\n"
    "5. No generic praise ('Great article!'). Every sentence must add technical "
    "value: a trade-off, a failure mode, a concrete next step, or a clarifying "
    "question.\n"
    "6. Be specific and concise. Prefer 3-6 sentences for comments.\n"
    "7. Output only the requested artifact, with no preamble or meta-commentary."
)


class AIError(RuntimeError):
    """Raised when the LLM request or response cannot be completed."""


class AIAssistant:
    """Thin wrapper around the OpenRouter chat completions endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: int = DEFAULT_TIMEOUT,
        referer: str = APP_REFERER,
        title: str = APP_TITLE,
    ):
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.referer = referer
        self.title = title

    def is_configured(self) -> bool:
        return bool(self.api_key and self.model)
    def _chat(self, messages: list[dict[str, str]], max_tokens: int = 800, temperature: float = 0.4) -> str:
        """Perform a chat completion, falling back across free models.

        OpenRouter retires ``:free`` slugs and rate-limits the survivors, so a
        single configured model is not reliable enough on its own. We try the
        configured model first, then walk the free fallback list.

        Raises ``AIError`` with a sanitized message if every attempt fails.
        """
        if not self.is_configured():
            raise AIError(
                "OpenRouter is not configured (missing OPENROUTER_API_KEY or OPENROUTER_MODEL)"
            )

        attempts: list[str] = [self.model]
        attempts += [m for m in DEFAULT_FREE_MODELS if m != self.model]
        # Drop known-retired slugs without spending an API call on them.
        attempts = [m for m in attempts if m not in RETIRED_MODELS]

        last_error = ""
        for model in attempts:
            try:
                return self._chat_once(model, messages, max_tokens, temperature)
            except AIError as exc:
                last_error = str(exc)
                message = str(exc)
                # 401/402/403 mean the key itself is the problem: retrying other
                # models cannot help, so surface it immediately.
                if any(code in message for code in ("HTTP 401", "HTTP 402", "HTTP 403")):
                    raise
                continue

        raise AIError(f"All OpenRouter models failed. Last error: {last_error}")

    def _chat_once(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """One completion attempt against a specific model."""
        payload = json.dumps(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        ).encode("utf-8")

        req = request.Request(
            OPENROUTER_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": self.referer,
                "X-Title": self.title,
            },
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            detail = _safe_error_body(exc)
            raise AIError(
                f"OpenRouter HTTP {exc.code} for model '{model}'"
                + (f": {detail}" if detail else "")
                + _hint_for_status(exc.code)
            ) from None
        except error.URLError as exc:
            raise AIError(f"OpenRouter request failed: {exc.reason}") from None
        except Exception as exc:  # pragma: no cover - unexpected transport error
            raise AIError(f"OpenRouter request failed: {type(exc).__name__}") from None

        return _extract_content(body, model)

    def summarize_activity(self, title: str, content: str = "") -> str:
        """Summarize a Builder Center post in 2-4 sentences."""
        user = (
            "Summarize the following AWS Builder Center item in 2-4 sentences. "
            "State only what the text supports. Do not add invented details.\n\n"
            f"TITLE: {title or '(untitled)'}\n\n"
            f"VISIBLE CONTENT:\n{(content or '(no body text available)').strip()[:6000]}\n\n"
            "Return only the summary."
        )
        return self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=300,
            temperature=0.2,
        ).strip()

    def can_contribute(self, summary: str, title: str = "") -> bool:
        """Decide whether a genuinely substantive contribution is possible."""
        user = (
            "Decide whether a technically substantive, non-generic comment can be "
            "written about the item below WITHOUT inventing personal experiences, "
            "experiments, or deployments. Answer YES only if the topic supports a "
            "concrete technical point (trade-off, failure mode, design question). "
            "Answer with exactly one word: YES or NO.\n\n"
            f"TITLE: {title or '(untitled)'}\n\nSUMMARY:\n{summary[:3000]}"
        )
        reply = self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=8,
            temperature=0.0,
        ).strip()
        return _is_affirmative(reply)

    def classify_activity(self, title: str, content: str = "") -> str:
        """Classify an item into a coarse topic label for relevance ranking."""
        user = (
            "Classify the item below with ONE short topic label chosen from: "
            "serverless, containers, databases, data-engineering, networking, "
            "security, observability, devops, ai-ml, java, architecture, other. "
            "Return only the label.\n\n"
            f"TITLE: {title or '(untitled)'}\n\nCONTENT:\n{(content or '')[:2000]}"
        )
        return self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=10,
            temperature=0.0,
        ).strip().lower()
    def draft_comment(self, title: str, summary: str, content: str = "") -> str:
        """Draft exactly ONE substantive comment for a Builder Center item."""
        user = (
            "Write ONE comment (3-6 sentences) for the AWS Builder Center item "
            "below. Requirements:\n"
            "- Add real technical value: a trade-off, a failure mode, a concrete "
            "next step, or a focused clarifying question.\n"
            "- Never invent experiments, benchmarks, deployments, employers or "
            "personal experience. Use conditional phrasing for anything unknown.\n"
            "- Do not request likes, upvotes, or reciprocal engagement.\n"
            "- Do not mention badges, rewards, points, or this tool.\n"
            "- Do not compliment the author generically.\n"
            "- Plain text only, no markdown headings, no signature.\n\n"
            f"TITLE: {title or '(untitled)'}\n\n"
            f"SUMMARY:\n{summary[:2000]}\n\n"
            f"VISIBLE CONTENT:\n{(content or '(none)').strip()[:4000]}\n\n"
            "Return only the comment text."
        )
        return self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=500,
            temperature=0.5,
        ).strip()

    def draft_article(
        self,
        topic: str,
        summary: str = "",
        outline: str = "",
        context: str = "",
    ) -> str:
        """Draft a short technical article/changelog style post."""
        user = (
            "Draft a short technical article (400-700 words) for AWS Builder "
            "Center about the topic below. Requirements:\n"
            "- Structure: problem, approach/design, trade-offs, when NOT to use it, "
            "and open questions. Use short paragraphs and a few plain-text lists.\n"
            "- Do NOT invent benchmarks, metrics, deployments, or personal "
            "experience. Label anything hypothetical explicitly as hypothetical.\n"
            "- Do not mention badges, rewards, points, or this tool.\n"
            "- No signatures, no call-to-action for likes or follows.\n\n"
            f"TOPIC: {topic or '(unspecified)'}\n\n"
            f"KNOWN CONTEXT (the only personal facts you may assume):\n"
            f"{context.strip() if context else '(none provided - keep the article general and hypothetical)'}\n\n"
            f"REFERENCE SUMMARY:\n{summary[:2000]}\n\n"
            f"OPTIONAL OUTLINE:\n{outline[:1000]}\n\n"
            "Return only the article."
        )
        return self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=1200,
            temperature=0.6,
        ).strip()

def _extract_content(body: str, model: str) -> str:
    """Pull the assistant message out of an OpenRouter response body."""
    try:
        data = json.loads(body)
    except ValueError:
        raise AIError(f"OpenRouter returned a non-JSON response for model '{model}'") from None

    if isinstance(data, dict) and data.get("error"):
        message = data["error"].get("message") if isinstance(data["error"], dict) else str(data["error"])
        raise AIError(f"OpenRouter error for model '{model}': {message}")

    try:
        choices = data["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError(f"OpenRouter response missing choices for model '{model}'") from None

    if isinstance(content, list):
        # Some providers return content as a list of parts.
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

    if not isinstance(content, str) or not content.strip():
        raise AIError(f"OpenRouter returned an empty completion for model '{model}'")
    return content


def _safe_error_body(exc: "error.HTTPError") -> str:
    """Read a small, sanitized slice of an HTTP error body."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")[:400]
    except Exception:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return str(data["error"].get("message", ""))[:200]
    except ValueError:
        pass
    return raw.replace("\n", " ")[:200]


def _hint_for_status(code: int) -> str:
    """Actionable hint for common OpenRouter failures."""
    hints = {
        401: " (check OPENROUTER_API_KEY)",
        402: " (insufficient credits)",
        403: " (model not permitted for this key)",
        404: " (model may no longer exist - set a different OPENROUTER_MODEL)",
        408: " (timeout)",
        429: " (rate limited)",
        502: " (provider unavailable)",
        503: " (no provider available - the free model may be temporarily unavailable)",
    }
    return hints.get(code, "")

# Free models to try, best first. OpenRouter retires :free slugs regularly, so
# the client walks this list instead of failing when one disappears.
DEFAULT_FREE_MODELS: tuple[str, ...] = (
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3.8-27b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "liquid/lfm-2.5-2.6b:free",
)

# Model slugs that are known-retired. Skipped without burning an API call.
RETIRED_MODELS = frozenset({"google/gemma-4-26b-a4b-it:free"})


def list_free_models(api_key: str, timeout: int = 20) -> list[str]:
    """Fetch the free-tier model list from OpenRouter. Best effort."""
    url = "https://openrouter.ai/api/v1/models"
    req = request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    return sorted(
        m["id"] for m in data.get("data", []) if str(m.get("id", "")).endswith(":free")
    )

def _is_affirmative(reply: str) -> bool:
    """Interpret a YES/NO answer from a small model.

    Small free models frequently answer with a sentence, a leading "Answer: YES",
    or markdown instead of a bare token. We look for an explicit affirmative
    anywhere in a short reply, but an explicit negative always wins so a reply
    like "NO - this is too generic" is never read as approval.
    """
    if not reply:
        return False
    text = reply.strip().strip("*_`").upper()
    first_line = text.splitlines()[0].strip() if text.splitlines() else text

    if re.search(r"\bNO\b", first_line):
        return False
    if re.search(r"\bYES\b", first_line):
        return True

    # Fall back to the whole reply only when it is short enough to be a
    # single-token answer plus noise.
    if len(text) <= 60:
        if re.search(r"\bNO\b", text):
            return False
        return bool(re.search(r"\bYES\b", text))
    return False