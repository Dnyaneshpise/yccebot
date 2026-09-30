"""AWS Student Rewards milestone tracking.

The reward terms referenced here reflect the currently known AWS Student
Rewards program at the time this agent was written. They are NOT guaranteed
and may change at any time, so they are always presented together with a link
to the official program page. The agent never assumes that a specific activity
always counts toward a badge - it inspects the live site whenever possible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Official / canonical links. Update these if AWS changes them.
OFFICIAL_REWARDS_URL = "https://aws.amazon.com/education/aws-student-rewards/"
BUILDER_URL = "https://builder.aws.com/"

TARGET_BADGES = 21

# Values above this are treated as an unreliable scrape rather than a real count.
MAX_SANE_BADGES = 1000

DISCLAIMER = (
    "Milestone rewards shown are the currently known AWS Student Rewards terms. "
    "They are NOT guaranteed and requirements can change - always verify at "
    f"{OFFICIAL_REWARDS_URL}"
)


@dataclass(frozen=True)
class Milestone:
    """A badge-count milestone and the reward currently associated with it."""

    badges: int
    reward: str


DEFAULT_MILESTONES: tuple[Milestone, ...] = (
    Milestone(badges=7, reward="$10 AWS credits"),
    Milestone(badges=14, reward="additional $20 AWS credits"),
    Milestone(badges=21, reward="$100 AWS Foundational Certification exam voucher"),
)

_BADGE_PATTERNS = (
    re.compile(r"^\s*(\d+)\s*/\s*\d+\s*$"),          # "13 / 21"
    re.compile(r"^\s*(\d+)\s*badges?\s*$", re.I),      # "13 badges"
    re.compile(r"^\s*badges?\s*[:\-]\s*(\d+)\s*$", re.I),  # "Badges: 13"
)


def parse_badge_count(text: str | None) -> int | None:
    """Parse a badge count from visible page text.

    Handles several formats ("13 / 21", "13 badges", "Badges: 13", "13"). Never
    guesses: anything that does not match a known format returns ``None``.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return None

    candidate = text.strip()
    if not candidate:
        return None

    # Reject negative values outright: "-3 / 21" must not parse as 3.
    if re.match(r"^-\s*\d", candidate) or re.search(r"-\s*\d+\s*/\s*\d+", candidate):
        return None

    for pattern in _BADGE_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return _clamp(int(match.group(1)))

    # Bare integer, e.g. the text of a counter element.
    if candidate.isdigit():
        return _clamp(int(candidate))

    # Embedded patterns inside a larger block of text.
    match = re.search(r"(\d+)\s*/\s*(\d+)", candidate)
    if match:
        return _clamp(int(match.group(1)))

    match = re.search(r"badges?\s*[:\-]\s*(\d+)", candidate, re.I)
    if match:
        return _clamp(int(match.group(1)))

    match = re.search(r"(\d+)\s+badges?\b", candidate, re.I)
    if match:
        return _clamp(int(match.group(1)))

    return None


def _clamp(value: int) -> int | None:
    """Reject obviously malformed values (negative or absurdly large)."""
    if value < 0 or value > MAX_SANE_BADGES:
        return None
    return value

def compute_milestones(
    badge_count: int | None,
    milestones: tuple[Milestone, ...] = DEFAULT_MILESTONES,
) -> dict[str, bool]:
    """Return a mapping of milestone target -> whether it has been reached.

    ``None`` (unknown badge count) yields an empty mapping so callers never
    report a milestone as reached when the count could not be determined.
    """
    if badge_count is None:
        return {}
    if not isinstance(badge_count, int) or isinstance(badge_count, bool):
        return {}
    return {str(m.badges): badge_count >= m.badges for m in milestones}


def next_milestone(
    badge_count: int | None,
    milestones: tuple[Milestone, ...] = DEFAULT_MILESTONES,
) -> Milestone | None:
    """Return the next unreached milestone, or ``None`` if all are reached."""
    if badge_count is None:
        return None
    for milestone in sorted(milestones, key=lambda m: m.badges):
        if badge_count < milestone.badges:
            return milestone
    return None


def badges_remaining(
    badge_count: int | None,
    milestones: tuple[Milestone, ...] = DEFAULT_MILESTONES,
) -> int | None:
    """How many badges are needed for the next milestone."""
    milestone = next_milestone(badge_count, milestones)
    if milestone is None or badge_count is None:
        return None
    return milestone.badges - badge_count


def newly_reached_milestones(
    previous: int | None,
    current: int | None,
    milestones: tuple[Milestone, ...] = DEFAULT_MILESTONES,
) -> list[Milestone]:
    """Milestones crossed when moving from ``previous`` to ``current``."""
    if current is None:
        return []
    baseline = previous if previous is not None else -1
    return [m for m in sorted(milestones, key=lambda m: m.badges) if baseline < m.badges <= current]


def describe_milestones(milestones: tuple[Milestone, ...] = DEFAULT_MILESTONES) -> str:
    """Human readable list of the currently known milestone rewards."""
    lines = [f"- {m.badges} badges: {m.reward}" for m in milestones]
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_progress(badge_count: int | None, target: int = TARGET_BADGES) -> str:
    """Format the progress line used in notifications."""
    if badge_count is None:
        return f"Progress: Unable to determine (target {target})"
    return f"{badge_count}/{target}"