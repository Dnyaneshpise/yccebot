"""Telegram Bot API notifications.

All messages are concise by design and are length-capped to Telegram's
4096-character limit. Secrets are redacted from any text before it leaves the
process, so a raw exception containing a token can never reach the chat.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

TELEGRAM_API = "https://api.telegram.org"
MESSAGE_LIMIT = 4096
DEFAULT_TIMEOUT = 30
SAFE_LIMIT = 3500

HEADER = "\u2601\ufe0f AWS Builder Badge Agent"


class TelegramError(RuntimeError):
    """Raised when a Telegram request cannot be completed."""


class TelegramNotifier:
    """Minimal Telegram Bot API client used for progress notifications."""

    def __init__(self, token: str, chat_id: str, timeout: int = DEFAULT_TIMEOUT):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.timeout = timeout

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def redact(self, text: str) -> str:
        """Remove configured secrets from arbitrary text."""
        safe = str(text)
        for secret in (self.token, self.chat_id):
            if secret and len(secret) > 4:
                safe = safe.replace(secret, "***REDACTED***")
        return safe

    def send_message(self, text: str) -> bool:
        """Send a plain-text message, splitting it if it is too long."""
        text = self.redact(text)
        if not self.is_configured():
            print("Telegram is not configured; skipping notification")
            return False

        chunks: list[str] = []
        while text:
            chunks.append(text[:SAFE_LIMIT])
            text = text[SAFE_LIMIT:]

        ok = True
        for chunk in chunks or ["(empty message)"]:
            ok = self._post(chunk) and ok
        return ok
    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Call a Bot API method and return the parsed response.

        Raises on transport or API failure so callers can decide how to
        handle it. ``_post`` builds on this and swallows errors instead.
        """
        if not self.is_configured():
            raise TelegramError("Telegram is not configured")

        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        try:
            with request.urlopen(
                url, data=parse.urlencode(payload).encode("utf-8"), timeout=self.timeout
            ) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except error.HTTPError as exc:
            raise TelegramError(f"Telegram HTTP {exc.code}: {_safe_body(exc)}") from None
        except error.URLError as exc:
            raise TelegramError(f"Telegram request failed: {exc.reason}") from None
        except ValueError:
            raise TelegramError("Telegram returned a non-JSON response") from None

        if not body.get("ok"):
            raise TelegramError(str(body.get("description", "unknown API error")))
        return body

    def _post(self, text: str) -> bool:
        """Post a single chunk to Telegram."""
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        payload = parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")

        try:
            with request.urlopen(url, data=payload, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except error.HTTPError as exc:
            print(f"Telegram HTTP {exc.code}: {_safe_body(exc)}")
            return False
        except error.URLError as exc:
            print(f"Telegram request failed: {exc.reason}")
            return False
        except (ValueError, OSError) as exc:
            print(f"Telegram response error: {type(exc).__name__}")
            return False

        if not body.get("ok"):
            print(f"Telegram API error: {body.get('description', 'unknown')}")
            return False
        return True

    def send_progress(self, badge_count, milestones: dict, target: int = 21) -> bool:
        """Send the daily progress summary."""
        from .rewards import format_progress, next_milestone

        progress = format_progress(badge_count, target)
        lines = [HEADER, ""]
        lines.append(f"Badge progress: {progress}")

        if badge_count is None:
            lines.append("Badge count could not be read from Builder Center today.")
        else:
            upcoming = next_milestone(badge_count)
            if upcoming is None:
                lines.append(f"All {target} badges reached. Check the official program page for next steps.")
            else:
                lines.append(f"Next milestone: {upcoming.badges} badges ({upcoming.badges - badge_count} to go)")
            lines.append("Milestones: " + ", ".join(
                f"{key} {'[done]' if value else '[todo]'}" for key, value in milestones.items()
            ))

        return self.send_message("\n".join(lines))
    def send_activity(
        self,
        activity: dict,
        summary: str = "",
        comment: str = "",
        why: str = "",
    ) -> bool:
        """Send one recommended activity with an optional draft comment."""
        lines = [HEADER, ""]
        lines.append(f"Recommended discussion:\n{activity.get('title', '(untitled)')}")
        if activity.get("url"):
            lines.append(f"URL:\n{activity['url']}")
        if summary:
            lines.append(f"What it covers:\n{summary}")
        if why:
            lines.append(f"Why it may be useful:\n{why}")
        if comment:
            lines.append(f"Suggested comment:\n{comment}")
        lines.append("")
        lines.append("\u26a0\ufe0f Review before posting. Nothing has been published.")
        return self.send_message("\n".join(lines))

    def send_activities(self, drafts: list[dict]) -> bool:
        """Send up to five recommended activities, one message each."""
        ok = True
        for draft in drafts[:5]:
            ok = self.send_activity(
                draft.get("activity", {}),
                summary=draft.get("summary", ""),
                comment=draft.get("comment", ""),
                why=draft.get("why", ""),
            ) and ok
        return ok

    def send_milestone(self, previous, current) -> bool:
        """Celebrate a change in the observed badge count."""
        from .rewards import newly_reached_milestones

        lines = ["\U0001f389 AWS Builder milestone update", ""]
        lines.append(f"Previous: {previous if previous is not None else 'unknown'} badges")
        lines.append(f"Current: {current if current is not None else 'unknown'} badges")

        reached = newly_reached_milestones(previous, current)
        for milestone in reached:
            lines.append("")
            lines.append(f"You reached the {milestone.badges}-badge milestone.")
            lines.append(f"Currently listed reward: {milestone.reward}")
        if reached:
            lines.append("")
            lines.append(
                "Rewards can change - verify current terms at "
                "https://aws.amazon.com/education/aws-student-rewards/"
            )
        return self.send_message("\n".join(lines))

    def send_auth_expired(self) -> bool:
        """Notify the user that the persisted AWS session needs regenerating."""
        return self.send_message(
            "\u26a0\ufe0f AWS Builder Agent\n\n"
            "Your AWS Builder Center session appears to have expired.\n\n"
            "Please regenerate the Playwright storage state locally and update:\n"
            "AWS_STORAGE_STATE_B64\n\n"
            "Commands:\n"
            "  python scripts/save_login_state.py\n"
            "  python scripts/encode_state.py"
        )

    def send_error(self, message: str) -> bool:
        """Send a sanitized technical error notification."""
        safe = self.redact(str(message)).replace(self.token or "***", "***")
        return self.send_message(
            "\u26a0\ufe0f AWS Builder Agent\n\n"
            f"Run failed or degraded:\n{safe[:1500]}"
        )


    def send_approval_request(self, proposal: Any) -> int | None:
        """Send a proposal with clear Approve / Reject instructions.

        Returns the Telegram message id, or None on failure. The message id is
        what the user replies to when approving, so the decision stays
        unambiguous.
        """
        if not self.is_configured():
            print("Telegram is not configured; skipping approval request")
            return None

        text = (
            "\U0001f514 Action needed\n\n"
            + proposal.describe()
            + "\n\nRead the article, then reply with:\n"
            f"  approve {proposal.id}   - do this\n"
            f"  reject  {proposal.id}   - skip it\n"
        )
        text = self.redact(text)
        try:
            result = self._api("sendMessage", {"chat_id": self.chat_id, "text": text})
        except Exception as exc:
            print(f"Could not send approval request: {type(exc).__name__}")
            return None
        return result.get("result", {}).get("message_id")

    def send_decision_ack(self, decision: str, proposal_id: str,
                          accepted: bool, title: str = "", detail: str = "") -> bool:
        """Acknowledge a reply so the user always gets an answer.

        Every reply the user sends gets exactly one response, including ones
        we cannot act on - silence would leave them unsure whether the agent
        saw the message at all.
        """
        if decision == "approve":
            head = "\U0001f4dd Reply received"
            if accepted:
                head = "\u2705 Approved"
            else:
                head = "\u26a0\ufe0f Could not approve"
        else:
            head = "\U0001f916 Skipped" if accepted else "\u26a0\ufe0f Could not skip"

        lines = [head, "", f"id: {proposal_id}"]
        if title:
            lines.append(f"item: {title}")
        if detail:
            lines.append("")
            lines.append(detail)
        return self.send_message("\n".join(lines))

    def send_execution_report(self, results: list[tuple[str, str]]) -> bool:
        """Report what the agent did after executing approvals."""
        if not results:
            return True
        lines = ["\U0001f4c4 Approved actions", ""]
        for proposal_id, outcome in results[:10]:
            icon = "\u2705" if outcome.startswith("OK") else "\u26a0\ufe0f"
            lines.append(f"{icon} {proposal_id}: {outcome}")
        return self.send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # Reading your replies back
    #
    # The bot asks you to reply "approve <id>" / "reject <id>". These
    # methods read those replies. Without them the messages queue up in
    # Telegram forever and nothing ever happens.
    # ------------------------------------------------------------------
    def get_updates(self, offset: int = 0, timeout: int = 0) -> list[dict[str, Any]]:
        """Fetch pending updates. ``offset`` acknowledges everything before it.

        Returns [] on any failure: a missing reply must never crash a run.
        """
        if not self.is_configured():
            return []
        payload = {"timeout": timeout, "offset": offset, "allowed_updates": '["message"]'}
        try:
            result = self._api("getUpdates", payload)
        except TelegramError as exc:
            print(f"Could not read Telegram updates: {exc}")
            return []
        updates = result.get("result")
        return updates if isinstance(updates, list) else []

    def acknowledge(self, last_update_id: int) -> None:
        """Mark updates as seen so they are not returned again."""
        if last_update_id <= 0:
            return
        self.get_updates(offset=last_update_id + 1, timeout=0)

    def parse_incoming_decisions(self, updates: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
        """Turn raw updates into ``(decision, proposal_id, update_id)`` triples.

        Only messages from the configured chat are considered, and the id must
        look like one we issued, so ordinary chat cannot approve an action.
        """
        from .actions import parse_decision

        results: list[tuple[str, str, int]] = []
        for update in updates or []:
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            chat = (message.get("chat") or {}).get("id")
            if str(chat) != str(self.chat_id):
                continue
            text = message.get("text") or ""
            parsed = parse_decision(text)
            if parsed is None:
                continue
            decision, proposal_id = parsed
            results.append((decision, proposal_id, int(update.get("update_id", 0))))
        return results


def _safe_body(exc: "error.HTTPError") -> str:
    """Read a short slice of a Telegram HTTP error body."""
    try:
        return exc.read().decode("utf-8", errors="replace")[:200].replace("\n", " ")
    except Exception:
        return ""