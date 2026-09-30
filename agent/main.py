"""Entry point for the AWS Builder Badge Agent.

One run does exactly this:

1. Decode the persisted AWS session (if provided) to ``auth/storage_state.json``.
2. Open AWS Builder Center with Playwright and confirm we are signed in.
3. Read the badge count, update ``data/progress.json``, notify Telegram.
4. Discover relevant public activities and produce review-only drafts.

This agent NEVER publishes, likes, upvotes, or comments on anything. Every
draft is delivered to Telegram for human review first.

Exit codes: ``0`` success, ``1`` unexpected error, ``2`` session/auth problem,
``3`` bad configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ai import AIAssistant
from .builder import AuthExpiredError, BuilderCenter
from .config import Config
from .discovery import find_opportunities
from .rewards import TARGET_BADGES, describe_milestones, newly_reached_milestones
from .storage import ProgressStorage
from .telegram import TelegramNotifier


def log(message: str) -> None:
    """Print a timestamped log line to stdout."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[agent] {stamp} {message}", flush=True)


# ----------------------------------------------------------------------
# Session handling
# ----------------------------------------------------------------------
def write_storage_state(config: Config) -> Path | None:
    """Decode ``AWS_STORAGE_STATE_B64`` into ``auth/storage_state.json``.

    Returns the path, or ``None`` when the variable is unset. The decoded JSON
    is validated before being written so a truncated secret fails loudly instead
    of producing a confusing logged-out run.
    """
    if not config.storage_state_b64:
        return None

    try:
        raw = base64.b64decode(config.storage_state_b64, validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise AuthExpiredError(
            "AWS_STORAGE_STATE_B64 is not valid base64-encoded JSON "
            f"({type(exc).__name__}). Re-run scripts/save_login_state.py "
            "then scripts/encode_state.py."
        ) from None

    if not isinstance(payload, dict) or "cookies" not in payload:
        raise AuthExpiredError(
            "Decoded AWS_STORAGE_STATE_B64 is not a Playwright storage state "
            "(expected a JSON object with a 'cookies' key)."
        )

    target = config.resolved_storage_state_path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o600)  # secrets: owner read/write only
    except OSError:
        pass
    target.write_text(json.dumps(payload), encoding="utf-8")
    log(f"Decoded AWS session to {target} ({len(payload['cookies'])} cookies)")
    return target


def build_components(
    config: Config,
) -> tuple[ProgressStorage, TelegramNotifier, AIAssistant]:
    """Instantiate the non-browser components."""
    storage = ProgressStorage(config.resolved_progress_path)
    telegram = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    ai = AIAssistant(api_key=config.openrouter_api_key, model=config.openrouter_model)
    return storage, telegram, ai
# ----------------------------------------------------------------------
# Run phases
# ----------------------------------------------------------------------
async def check_auth(builder: BuilderCenter, telegram: TelegramNotifier) -> int:
    """Confirm the session is usable. Returns 0 when OK, non-zero otherwise."""
    if not await builder.goto(builder.base_url):
        log("ERROR: could not load the Builder Center page")
        telegram.send_error("Could not load the AWS Builder Center page.")
        return 1

    if not builder.is_authenticated():
        log("ERROR: session is not authenticated")
        telegram.send_auth_expired()
        return 2

    log("Session is authenticated")
    return 0


async def track_badges(
    builder: BuilderCenter,
    storage: ProgressStorage,
    telegram: TelegramNotifier,
    state: dict[str, Any],
    previous: int | None,
) -> None:
    """Read the badge count, persist it, and notify."""
    badge_count = await builder.get_badge_count()
    if badge_count is None:
        log("WARNING: badge count could not be read; keeping the stored value")
    else:
        log(f"Current badge count: {badge_count}/{TARGET_BADGES}")

    state, changed = storage.record_badge_count(state, badge_count)
    if changed:
        log("Badge count updated in progress store")

    reached = newly_reached_milestones(previous, badge_count)
    if reached:
        for milestone in reached:
            log(f"Milestone reached: {milestone.badges} badges")
        telegram.send_milestone(previous, badge_count)
    elif previous is not None and badge_count is not None and previous != badge_count:
        telegram.send_message(
            "Badge count updated\n\n"
            f"Previous: {previous}\n"
            f"Current: {badge_count}\n\n"
            + describe_milestones()
        )

    telegram.send_progress(badge_count, state.get("milestones", {}), TARGET_BADGES)


async def discover_and_draft(
    config: Config,
    builder: BuilderCenter,
    storage: ProgressStorage,
    state: dict[str, Any],
    telegram: TelegramNotifier,
    ai: AIAssistant,
) -> int:
    """Discover activities and produce review-only drafts.

    Drafting failures never break badge tracking; the run continues without
    drafts and the reason is logged.
    """
    if not ai.is_configured():
        log("Skipping discovery: OpenRouter is not configured")
        return 0

    seen = {
        str(item.get("title", "")).strip().lower()
        for item in (state.get("activities") or [])[-60:]
        if isinstance(item, dict) and item.get("title")
    }

    try:
        drafts = await find_opportunities(
            builder,
            ai,
            max_items=config.max_drafts,
            min_score=config.min_relevance,
            seen_titles=seen,
        )
    except Exception as exc:
        log(f"WARNING: activity discovery failed - {type(exc).__name__}: {exc}")
        return 0

    if not drafts:
        log("No new relevant activities today")
        return 0

    for draft in drafts:
        activity = draft.get("activity", {})
        log(f"Draft ready: {activity.get('title', '(untitled)')}")
        storage.record_activity(state, activity)

    telegram.send_activities(drafts)
    log(f"{len(drafts)} draft(s) sent for human review - nothing was published")
    return len(drafts)
# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
async def run(config: Config, draft: bool | None = None) -> int:
    """Execute one full agent run and return a process exit code."""
    missing = config.validate()
    if missing:
        for name in missing:
            log(f"ERROR: missing required environment variable {name}")
        log("See .env.example and the README deployment section.")
        return 3

    for warning in config.warnings():
        log(f"WARNING: {warning}")

    try:
        write_storage_state(config)
    except AuthExpiredError as exc:
        log(f"ERROR: {exc}")
        return 2

    if not config.resolved_storage_state_path.exists():
        log(
            f"ERROR: no AWS session found at {config.resolved_storage_state_path}. "
            "Generate one locally with scripts/save_login_state.py."
        )
        return 2

    storage, telegram, ai = build_components(config)
    state = storage.load()
    previous = state.get("badges")
    log("Previous badge count: " + (str(previous) if previous is not None else "unknown"))

    builder = BuilderCenter(
        base_url=config.builder_url,
        headless=config.headless,
        user_agent=config.user_agent or None,
    )

    exit_code = 0
    try:
        await builder.initialize(str(config.resolved_storage_state_path))

        auth_code = await check_auth(builder, telegram)
        if auth_code != 0:
            return auth_code

        await track_badges(builder, storage, telegram, state, previous)

        should_draft = config.dry_run is False if draft is None else draft
        if should_draft:
            await discover_and_draft(config, builder, storage, state, telegram, ai)
        else:
            log("Drafting disabled for this run (badge tracking only)")

    except AuthExpiredError as exc:
        log(f"ERROR: {exc}")
        telegram.send_auth_expired()
        exit_code = 2
    except Exception as exc:
        log(f"ERROR: unexpected failure - {type(exc).__name__}: {exc}")
        telegram.send_error(f"Unexpected failure - {type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        try:
            storage.save(state)
        except OSError as exc:
            log(f"WARNING: could not save progress file - {exc}")
        await builder.close()

    log(f"Run finished with exit code {exit_code}")
    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m agent.main",
        description=(
            "Track AWS Builder Center badge progress and draft content for "
            "human review. Never publishes anything."
        ),
    )
    parser.add_argument(
        "--draft",
        dest="draft",
        action="store_true",
        default=None,
        help="Force activity discovery and draft generation this run.",
    )
    parser.add_argument(
        "--no-draft",
        dest="draft",
        action="store_false",
        help="Track badge progress only; skip discovery and drafting.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Print configuration status and exit without touching AWS.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    config = Config.from_env()

    if args.check_config:
        log(f"OpenRouter model : {config.openrouter_model or '(unset)'}")
        log(f"Builder URL      : {config.builder_url}")
        log(f"Progress file    : {config.resolved_progress_path}")
        log(f"Session file     : {config.resolved_storage_state_path}")
        log(f"Drafting enabled : {not config.dry_run}")
        missing = config.validate()
        if missing:
            log("Missing required: " + ", ".join(missing))
            return 3
        for warning in config.warnings():
            log(f"WARNING: {warning}")
        log("Configuration looks usable")
        return 0

    try:
        return asyncio.run(run(config, draft=args.draft))
    except KeyboardInterrupt:
        log("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())