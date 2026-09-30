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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .actions import ApprovalStore
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

    if not await builder.is_authenticated():
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
    detail = await builder.get_badge_progress()
    badge_count = detail["granted"] if detail else None
    total = detail["total"] if detail else None

    if badge_count is None:
        log("WARNING: badge count could not be read; keeping the stored value")
    else:
        log(f"Current badge count: {badge_count}/{total or TARGET_BADGES} badges granted")
        state["badge_total"] = total
        state["badge_categories"] = detail.get("categories", {})
        # NOTE: "badges" is already the integer count - the per-badge detail
        # list must use a different key or _normalize() would coerce it away.
        state["badge_details"] = detail.get("badges", [])
        outstanding = [b for b in detail.get("badges", []) if b.get("status") != "GRANTED"]
        for badge in outstanding[:5]:
            log(
                f"  outstanding: {badge['name']} "
                f"{badge.get('count')}/{badge.get('threshold')}"
            )

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

    telegram.send_progress(
        badge_count,
        state.get("milestones", {}),
        total or TARGET_BADGES,
    )


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
            "Track AWS Builder Center badge progress and propose actions for "
            "your review. Nothing is ever published without explicit approval."
        ),
        epilog=(
            "Approval flow:\n"
            "  --propose        pick articles for the badges closest to completion\n"
            "  --accept ID      mark a proposal approved\n"
            "  --reject ID      mark a proposal rejected\n"
            "  --execute        perform ONLY the actions you approved\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--draft", dest="draft", action="store_true", default=None,
                        help="Force draft generation this run.")
    parser.add_argument("--no-draft", dest="draft", action="store_false",
                        help="Track badge progress only.")
    parser.add_argument("--check-config", action="store_true",
                        help="Print configuration status and exit.")
    parser.add_argument("--propose", action="store_true",
                        help="Suggest actions for the badges closest to completion.")
    parser.add_argument("--execute", action="store_true",
                        help="Perform only the actions you already approved.")
    parser.add_argument("--accept", metavar="ID", default=None,
                        help="Approve a proposal by id.")
    parser.add_argument("--reject", metavar="ID", default=None,
                        help="Reject a proposal by id.")
    return parser.parse_args(argv)

async def propose_actions(
    builder: BuilderCenter,
    telegram: TelegramNotifier,
    ai: AIAssistant,
    state: dict[str, Any],
    store: ApprovalStore,
    limit: int = 3,
) -> list[Any]:
    """Create proposals for the badges closest to completion and ask the user.

    Proposes only - performs nothing. Each proposal is sent to Telegram with
    the article summary so the user can read and decide.
    """
    from agent.actions import Proposal, next_actions, plan_for_badge

    targets = next_actions(state.get("badge_details", []), limit=limit)
    if not targets:
        log("No actionable badges found")
        return []

    drafts = await find_opportunities(builder, ai, max_items=limit * 2, min_score=1)
    activities = [d.get("activity", {}) for d in drafts]
    if not activities:
        try:
            activities = await builder.discover_activities(limit=10)
        except Exception as exc:
            log(f"Activity discovery failed: {type(exc).__name__}")
            activities = []

    created: list[Any] = []
    for index, badge_name in enumerate(targets):
        plan = plan_for_badge(badge_name)
        if plan is None:
            continue
        kind, _instruction = plan

        activity = activities[index] if index < len(activities) else {}
        draft = drafts[index] if index < len(drafts) else {}
        url = activity.get("url", "")
        if not url:
            continue

        proposal = Proposal(
            id=uuid.uuid4().hex[:10],
            kind=kind,
            title=activity.get("title", "(untitled)"),
            url=url,
            summary=draft.get("summary", ""),
            draft=draft.get("comment", "") if kind in ("comment", "article") else "",
            badge=badge_name,
            reason="Closest badge to completion",
        )
        stored = store.add(proposal)
        telegram.send_approval_request(stored)
        log(f"Proposed {kind} for '{stored.title}' (id={stored.id}) -> {badge_name}")
        created.append(stored)

    return created


async def run_approved_actions(
    builder: BuilderCenter,
    telegram: TelegramNotifier,
    store: ApprovalStore,
) -> list[tuple[str, str]]:
    """Execute only what the user has explicitly approved."""
    from agent.actions import run_approved

    pending = store.approved()
    if not pending:
        log("No approved actions waiting")
        return []

    log(f"Executing {len(pending)} approved action(s)")
    handlers = {
        "like": lambda p: builder.like_article(p.url),
        "comment": lambda p: builder.post_comment(p.url, p.draft),
        "vote": lambda p: builder.vote_on_wish(p.url),
    }
    results = await run_approved(builder, store, handlers)
    for proposal_id, outcome in results:
        log(f"  {proposal_id}: {outcome}")
    telegram.send_execution_report(results)
    return results

def approvals_path(config: Config) -> Path:
    """Where the approval queue lives."""
    return config.resolve("data/approvals.json")


# ----------------------------------------------------------------------
# Approval-mode CLI
# ----------------------------------------------------------------------
async def approval_mode(config: Config, action: str) -> int:
    """``--propose`` asks for approval; ``--execute`` performs what you approved."""
    from agent.actions import ApprovalStore

    if config.validate():
        log("ERROR: missing AWS session; run scripts/save_login_state.py first")
        return 3

    write_storage_state(config)
    storage, telegram, ai = build_components(config)
    state = storage.load()
    store = ApprovalStore(approvals_path(config))

    builder = BuilderCenter(
        base_url=config.builder_url,
        headless=config.headless,
        user_agent=config.user_agent or None,
    )
    try:
        await builder.initialize(str(config.resolved_storage_state_path))
        auth_code = await check_auth(builder, telegram)
        if auth_code != 0:
            return auth_code

        if action == "propose":
            await track_badges(builder, storage, telegram, state, state.get("badges"))
            created = await propose_actions(builder, telegram, ai, state, store)
            log(f"{len(created)} proposal(s) sent. Reply 'approve <id>' or 'reject <id>'.")
            print("\nPending proposal ids:")
            for proposal in store.pending():
                print(f"   {proposal.id}  {proposal.kind:<8} {proposal.title[:52]}")
            print("\nTo accept:  python -m agent.main --accept <id>")
            print("To refuse:  python -m agent.main --reject <id>")
            print("To perform approved actions:  python -m agent.main --execute")
            return 0

        results = await run_approved_actions(builder, telegram, store)
        log(f"Executed {len(results)} approved action(s)")
        return 0
    except AuthExpiredError as exc:
        log(f"ERROR: {exc}")
        telegram.send_auth_expired()
        return 2
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        telegram.send_error(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        storage.save(state)
        await builder.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    config = Config.from_env()

    if args.accept or args.reject:
        from agent.actions import ApprovalStore, parse_decision

        store = ApprovalStore(approvals_path(config))
        text = f"approve {args.accept}" if args.accept else f"reject {args.reject}"
        decision = parse_decision(text)
        if decision is None:
            log("ERROR: could not parse that decision")
            return 1
        action, proposal_id = decision
        proposal = store.decide(proposal_id, action == "approve")
        if proposal is None:
            log(f"ERROR: no proposal with id '{proposal_id}'")
            log("Run  python -m agent.main --propose  first")
            return 1
        log(f"{'APPROVED' if action == 'approve' else 'REJECTED'}: {proposal.title}")
        log("Run  python -m agent.main --execute  to perform approved actions")
        return 0

    if args.propose or args.execute:
        action = "propose" if args.propose else "execute"
        return asyncio.run(approval_mode(config, action))

    if args.check_config:
        log(f"OpenRouter model : {config.openrouter_model or '(unset)'}")
        log(f"Builder URL      : {config.builder_url}")
        log(f"Progress file    : {config.resolved_progress_path}")
        log(f"Session file     : {config.resolved_storage_state_path}")
        log(f"Approvals file   : {approvals_path(config)}")
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
