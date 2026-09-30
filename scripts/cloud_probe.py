"""One-shot diagnostic: can a cloud-launched browser perform a write?

Answers the question empirically instead of by assumption. Run in CI, it:

  1. launches Chromium with the stored session
  2. reports whether the page looks signed in
  3. tries to read badge progress
  4. attempts exactly ONE like on one article
  5. re-reads isLiked from the API to see if it persisted

Writes a JSON result to data/cloud_probe_result.json and posts a summary
to Telegram. No stealth, no proxies, no CAPTCHA solving - just a plain
headless browser doing one ordinary click.

    python scripts/cloud_probe.py <article_url>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RESULT_PATH = PROJECT_ROOT / "data" / "cloud_probe_result.json"


def record(**fields) -> dict:
    result = {
        "environment": "github-actions" if os.getenv("GITHUB_ACTIONS") else "local",
        "run_id": os.getenv("GITHUB_RUN_ID", "local"),
    }
    result.update(fields)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def probe(article_url: str) -> dict:
    from agent.builder import BuilderCenter
    from agent.telegram import TelegramNotifier

    out: dict = {"article": article_url}

    b = BuilderCenter(headless=True)
    try:
        state = os.getenv("AWS_STORAGE_STATE_B64", "")
        if not state:
            return record(**out, verdict="no-session", detail="AWS_STORAGE_STATE_B64 missing")

        from agent.main import write_storage_state
        from agent.config import Config

        cfg = Config.from_env()
        write_storage_state(cfg)
        state_file = str(cfg.resolved_storage_state_path)

        await b.initialize(state_file)

        if not await b.goto("https://builder.aws.com/"):
            return record(**out, verdict="nav-failed", detail="could not load home")

        out["is_authenticated"] = await b.is_authenticated()

        # Does the page think we are signed in? This is the tell.
        out["nav_signout"] = await b.page.evaluate(
            """() => [...document.querySelectorAll('a,button')]
                 .filter(e => /sign out|log out/i.test(e.innerText||'')).length"""
        )
        out["nav_signin"] = await b.page.evaluate(
            """() => [...document.querySelectorAll('a,button')]
                 .filter(e => /^(sign in|log in)$/i.test((e.innerText||'').trim())).length"""
        )

        detail = await b.get_badge_progress()
        out["badge_read"] = f"{detail['granted']}/{detail['total']}" if detail else None

        # ---- the single write under test ----
        if not article_url:
            out["verdict"] = "no-url"
            out["detail"] = "no article url supplied"
            return record(**out)

        cid = article_url.rstrip("/").split("/content/")[-1].split("/")[0]

        async def is_liked() -> object:
            r = await b.page.request.get(
                f"https://api.builder.aws.com/cs/v2/articles?articleId=%2Fcontent%2F{cid}",
                headers={"referer": "https://builder.aws.com/", "accept": "application/json"},
            )
            if r.status != 200:
                return f"HTTP {r.status}"
            data = await r.json()

            def find(node):
                if isinstance(node, dict):
                    if "isLiked" in node:
                        return node.get("isLiked")
                    for v in node.values():
                        g = find(v)
                        if g is not None:
                            return g
                elif isinstance(node, list):
                    for x in node[:3]:
                        g = find(x)
                        if g is not None:
                            return g
                return None

            return find(data)

        out["isLiked_before"] = await is_liked()
        out["like_result"] = await b.like_article(article_url)
        out["isLiked_after"] = await is_liked()

        persisted = out["isLiked_after"] is True
        out["write_persisted"] = persisted
        out["verdict"] = "WRITE-OK" if persisted else "WRITE-BLOCKED"

        notifier = TelegramNotifier(
            os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
        )
        if notifier.is_configured():
            notifier.send_message(
                "Cloud probe result\n\n"
                f"verdict: {out['verdict']}\n"
                f"isLiked before: {out['isLiked_before']}\n"
                f"isLiked after : {out['isLiked_after']}\n"
                f"badge read   : {out.get('badge_read')}\n"
                f"sign-out nav : {out.get('nav_signout')}"
            )
        return record(**out)

    except Exception as exc:
        return record(**out, verdict="error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        await b.close()


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("PROBE_ARTICLE_URL", "")
    result = asyncio.run(probe(url))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())