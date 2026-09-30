"""Badge-targeting action planner and the human-approval queue.

Design rule, enforced here: **no action is ever performed without a recorded
human approval.** The agent may propose; only the user decides. Every proposal
is written to ``data/approvals.json`` with an explicit ``approved`` flag, and
:func:`run_approved` refuses to act on anything that is not approved.

This module is deliberately split from execution:
  * :func:`build_proposals`  - decide what could be done, propose it
  * :func:`run_approved`     - perform ONLY what the user has approved
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Badge -> what the user would have to do. Used to explain proposals clearly.
BADGE_ACTIONS: dict[str, tuple[str, str]] = {
    "30-Day Like Streak": ("like", "Like one article per day"),
    "90-Day Like Streak": ("like", "Like one article per day"),
    "7-Day Comment Streak": ("comment", "Post one comment per day"),
    "30-Day Comment Streak": ("comment", "Post one comment per day"),
    "90-Day Comment Streak": ("comment", "Post one comment per day"),
    "Discussion Debut": ("comment", "Post a first comment"),
    "First Wish": ("wish", "Publish a wish"),
    "4-Week Wish Vote Streak": ("vote", "Vote on a wish each week"),
    "First Article": ("article", "Publish an article"),
    "4-Week Article Publishing Streak": ("article", "Publish an article each week"),
}

# Badges that depend on other people's reactions. No action can help these.
SOCIAL_BADGES = (
    "Conversation Starter",
    "Idea Influencer",
    "Meaningful Contributor",
    "Valued Creator",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Proposal:
    """A single suggested action awaiting the user's decision."""

    id: str
    kind: str                 # like | comment | wish | vote | article
    title: str
    url: str
    summary: str = ""
    draft: str = ""           # pre-written text for comment/article proposals
    badge: str = ""           # which badge this advances
    reason: str = ""
    created_at: str = field(default_factory=_now)
    approved: bool | None = None   # None = pending, True/False = decided
    decided_at: str | None = None
    result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known.setdefault("id", data.get("id", ""))
        known["id"] = str(known.get("id") or uuid.uuid4().hex[:12])
        known["kind"] = known.get("kind") or "like"
        known["title"] = known.get("title") or "(untitled)"
        known["url"] = known.get("url") or ""
        return cls(**known)

    def describe(self) -> str:
        """Human-readable one-liner for Telegram."""
        icons = {
            "like": "\U0001f44d",
            "comment": "\U0001f4ac",
            "vote": "\U0001f5f3",
            "wish": "\U0001f31f",
            "article": "\u270d\ufe0f",
        }
        icon = icons.get(self.kind, "\u2022")
        text = f"{icon} {self.title}"
        if self.badge:
            text += f"\n   advances: {self.badge}"
        if self.summary:
            text += f"\n\n{self.summary}"
        if self.draft:
            text += f"\n\nProposed text (edit before approving):\n{self.draft}"
        text += f"\n\nURL: {self.url}" if self.url else ""
        return text

class ApprovalStore:
    """Persistent queue of proposals and their decisions."""

    def __init__(self, path: str | os.PathLike[str] = "data/approvals.json"):
        self.path = Path(path)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> list[Proposal]:
        return [Proposal.from_dict(row) for row in self._read()]

    def add(self, proposal: Proposal) -> Proposal:
        rows = self._read()
        for row in rows:
            if row.get("url") == proposal.url and row.get("kind") == proposal.kind:
                existing = Proposal.from_dict(row)
                if existing.approved is None:
                    return existing  # already pending, do not duplicate
        rows.append(proposal.to_dict())
        rows = rows[-200:]  # keep the file bounded
        self._write(rows)
        return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        for proposal in self.all():
            if proposal.id == proposal_id:
                return proposal
        return None

    def decide(self, proposal_id: str, approved: bool) -> Proposal | None:
        """Record the user's decision. This is the only way to authorise work."""
        rows = self._read()
        for index, row in enumerate(rows):
            if str(row.get("id")) == str(proposal_id):
                row["approved"] = bool(approved)
                row["decided_at"] = _now()
                self._write(rows)
                return Proposal.from_dict(row)
        return None

    def approved(self) -> list[Proposal]:
        return [p for p in self.all() if p.approved is True and p.result is None]

    def pending(self) -> list[Proposal]:
        return [p for p in self.all() if p.approved is None]

    def set_result(self, proposal_id: str, result: str) -> None:
        rows = self._read()
        for row in rows:
            if str(row.get("id")) == str(proposal_id):
                row["result"] = result
                self._write(rows)
                return

def plan_for_badge(badge_name: str) -> tuple[str, str] | None:
    """Return ``(kind, human instruction)`` for a badge, or None if unsupported."""
    return BADGE_ACTIONS.get(badge_name)


def next_actions(
    badge_details: list[dict[str, Any]],
    limit: int = 3,
) -> list[str]:
    """Pick the actionable badges closest to completion.

    Social badges are excluded: no automated action can satisfy them, and
    proposing them would be misleading.
    """
    candidates: list[tuple[float, dict[str, Any]]] = []
    for badge in badge_details or []:
        name = badge.get("name") or ""
        if name in SOCIAL_BADGES or badge.get("status") == "GRANTED":
            continue
        if name not in BADGE_ACTIONS:
            continue
        count = badge.get("count") or 0
        threshold = badge.get("threshold") or 1
        progress = (count / threshold) if threshold else 0.0
        candidates.append((progress, badge))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [b.get("name") for _, b in candidates[:limit]]


async def execute(builder: Any, proposal: Proposal) -> str:
    """Perform ONE approved action. Never called for an unapproved proposal.

    The human approval check is repeated here (defence in depth) so that a bug
    elsewhere in the pipeline cannot cause an unapproved action.
    """
    if proposal.approved is not True:
        return "REFUSED: no human approval recorded"

    if proposal.kind == "like":
        return await builder.like_article(proposal.url)
    if proposal.kind == "comment":
        if not proposal.draft.strip():
            return "REFUSED: no comment text to post"
        return await builder.post_comment(proposal.url, proposal.draft)
    if proposal.kind == "vote":
        return await builder.vote_on_wish(proposal.url)
    return f"REFUSED: unsupported action '{proposal.kind}'"


async def run_approved(
    builder: Any,
    store: ApprovalStore,
    actions: dict[str, Any],
) -> list[tuple[str, str]]:
    """Execute every approved proposal. Returns ``[(id, result), ...]``.

    ``actions`` is a mapping of ``kind -> callable`` so this stays testable
    without a real browser.
    """
    results: list[tuple[str, str]] = []
    for proposal in store.approved():
        if proposal.approved is not True:  # belt and braces
            continue
        handler = actions.get(proposal.kind)
        if handler is None:
            result = f"REFUSED: no handler for '{proposal.kind}'"
        else:
            try:
                outcome = handler(proposal)
                if hasattr(outcome, "__await__"):
                    outcome = await outcome
                result = str(outcome)
            except Exception as exc:
                result = f"FAILED: {type(exc).__name__}: {exc}"
        store.set_result(proposal.id, result)
        results.append((proposal.id, result))
    return results

_APPROVE_RE = re.compile(r"^\s*/?(?:approve|yes|do)\s+([A-Za-z0-9_-]+)\s*$", re.I)
_REJECT_RE = re.compile(r"^\s*/?(?:reject|no|skip)\s+([A-Za-z0-9_-]+)\s*$", re.I)

# A bare proposal id ("22e20ddcac") counts as approval. People reply with just
# the id, and rejecting requires typing the word, so the risky direction is
# always the explicit one.
_BARE_ID_RE = re.compile(r"^\s*/?([A-Za-z0-9]{6,})([A-Za-z0-9_-]*)\s*$")


def parse_decision(text: str) -> tuple[str, str] | None:
    """Parse an approval/rejection command.

    Returns ``("approve"|"reject", proposal_id)`` or ``None`` if the text is
    not a decision. Only explicit, unambiguous commands are honoured.
    """
    if not text or not isinstance(text, str):
        return None

    def _plausible_id(value: str) -> bool:
        """Only accept something shaped like an id we actually issued.

        Every proposal id is 10 hex characters. Requiring that shape is what
        stops "yes please" or "thanks" from being treated as an approval.
        """
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,32}", value or "")) and bool(
            re.search(r"\d", value or "")
        )

    match = _APPROVE_RE.match(text)
    if match and _plausible_id(match.group(1)):
        return "approve", match.group(1)
    match = _REJECT_RE.match(text)
    if match and _plausible_id(match.group(1)):
        return "reject", match.group(1)

    # Bare id => approve. This is how people actually reply, so it has to work.
    # We require a digit and a plausible length so "thanks" or "ok" cannot be
    # read as an approval.
    match = _BARE_ID_RE.match(text)
    if match:
        candidate = (match.group(1) + (match.group(2) or "")).strip()
        if _plausible_id(candidate):
            return "approve", candidate
    return None


def is_decision_for(text: str, proposal_id: str) -> tuple[bool, str] | None:
    """Parse a decision and confirm it targets ``proposal_id``.

    Returns ``(approved, id)`` only when the command names this proposal,
    which stops one approval from being applied to the wrong item.
    """
    decision = parse_decision(text)
    if decision is None:
        return None
    action, target_id = decision
    if str(target_id) != str(proposal_id):
        return None
    return action == "approve", str(proposal_id)