"""Tests for badge parsing and milestone tracking."""

from __future__ import annotations

import pytest

from agent.rewards import (
    DEFAULT_MILESTONES,
    DISCLAIMER,
    MAX_SANE_BADGES,
    OFFICIAL_REWARDS_URL,
    TARGET_BADGES,
    badges_remaining,
    compute_milestones,
    describe_milestones,
    format_progress,
    newly_reached_milestones,
    next_milestone,
    parse_badge_count,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13 / 21", 13),
        ("13/21", 13),
        ("  7 / 21  ", 7),
        ("13 badges", 13),
        ("1 badge", 1),
        ("Badges: 13", 13),
        ("badges - 5", 5),
        ("13", 13),
        ("You have earned 14 badges so far", 14),
        ("Progress: 21/21", 21),
    ],
)
def test_parse_badge_count_recognises_known_formats(text, expected):
    assert parse_badge_count(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "no numbers here",
        "Sign in to AWS Builder Center",
        "-3 / 21",
        f"{MAX_SANE_BADGES + 1} / 21",
        12345,
    ],
)
def test_parse_badge_count_never_guesses(text):
    """Unparseable or implausible input must return None, never a guess."""
    assert parse_badge_count(text) is None


def test_parse_badge_count_rejects_absurd_values():
    assert parse_badge_count("5000 badges") is None
    assert parse_badge_count("5000") is None


def test_milestones_for_middle_progress():
    result = compute_milestones(10)
    assert result == {"7": True, "14": False, "21": False}


def test_milestones_all_reached():
    assert compute_milestones(21) == {"7": True, "14": True, "21": True}


def test_milestones_none_for_unknown_count():
    """An unknown badge count must never report a milestone as reached."""
    assert compute_milestones(None) == {}


def test_milestones_reject_bool_and_non_int():
    assert compute_milestones(True) == {}
    assert compute_milestones("13") == {}


def test_next_milestone():
    assert next_milestone(3).badges == 7
    assert next_milestone(7).badges == 14
    assert next_milestone(20).badges == 21


def test_next_milestone_when_complete_or_unknown():
    assert next_milestone(21) is None
    assert next_milestone(None) is None


def test_badges_remaining():
    assert badges_remaining(3) == 4
    assert badges_remaining(20) == 1
    assert badges_remaining(21) is None
    assert badges_remaining(None) is None


def test_newly_reached_milestones():
    reached = newly_reached_milestones(6, 15)
    assert [m.badges for m in reached] == [7, 14]


def test_newly_reached_on_first_ever_run():
    """With no baseline, every milestone up to the current count is 'new'."""
    reached = newly_reached_milestones(None, 8)
    assert [m.badges for m in reached] == [7]


def test_newly_reached_empty_without_change():
    assert newly_reached_milestones(10, 10) == []
    assert newly_reached_milestones(10, None) == []


def test_describe_milestones_includes_official_link():
    text = describe_milestones()
    for milestone in DEFAULT_MILESTONES:
        assert str(milestone.badges) in text
    assert OFFICIAL_REWARDS_URL in text
    assert "NOT guaranteed" in text or DISCLAIMER in text


def test_format_progress():
    assert format_progress(13) == "13/21"
    assert "Unable to determine" in format_progress(None)


def test_target_badges_matches_final_milestone():
    assert TARGET_BADGES == max(m.badges for m in DEFAULT_MILESTONES)