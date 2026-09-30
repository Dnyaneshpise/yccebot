"""Tests for progress storage, discovery ranking, and config handling."""

from __future__ import annotations

import json

from agent.config import Config
from agent.discovery import is_relevant, normalize_title, rank_activities, relevance_score
from agent.storage import ProgressStorage


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------
def test_load_missing_file_returns_defaults(tmp_path):
    store = ProgressStorage(tmp_path / "nested" / "progress.json")
    state = store.load()
    assert state["badges"] is None
    assert state["milestones"] == {"7": False, "14": False, "21": False}


def test_load_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert ProgressStorage(path).load()["badges"] is None


def test_load_wrong_shape_is_normalised(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert ProgressStorage(path).load()["badges"] is None


def test_load_coerces_bad_field_types(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text(
        json.dumps({"badges": "13", "activities": "nope", "history": 5, "milestones": "x"}),
        encoding="utf-8",
    )
    state = ProgressStorage(path).load()
    assert state["badges"] is None
    assert state["activities"] == []
    assert state["history"] == []
    assert state["milestones"] == {"7": False, "14": False, "21": False}


def test_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "sub" / "progress.json"
    store = ProgressStorage(path)
    state = store.default_state()
    state["badges"] = 9
    store.save(state)

    assert json.loads(path.read_text(encoding="utf-8"))["badges"] == 9
    assert store.load()["badges"] == 9
    assert not list(path.parent.glob("*.tmp"))


def test_record_badge_count_sets_milestones(tmp_path):
    store = ProgressStorage(tmp_path / "progress.json")
    state = store.default_state()
    state, changed = store.record_badge_count(state, 15)

    assert changed is True
    assert state["badges"] == 15
    assert state["milestones"] == {"7": True, "14": True, "21": False}
    assert state["last_run"].endswith("Z")
    assert state["history"][-1]["badges"] == 15


def test_record_badge_count_no_change_adds_no_history(tmp_path):
    store = ProgressStorage(tmp_path / "progress.json")
    state = store.default_state()
    state, _ = store.record_badge_count(state, 15)
    history_len = len(state["history"])

    state, changed = store.record_badge_count(state, 15)
    assert changed is False
    assert len(state["history"]) == history_len


def test_unknown_count_preserves_stored_value(tmp_path):
    """A failed scrape must never wipe previously good data."""
    store = ProgressStorage(tmp_path / "progress.json")
    state = store.default_state()
    state, _ = store.record_badge_count(state, 12)

    state, changed = store.record_badge_count(state, None)
    assert state["badges"] == 12
    assert changed is False
    assert state["last_run"] is not None


def test_record_activity_deduplicates_by_url(tmp_path):
    store = ProgressStorage(tmp_path / "progress.json")
    state = store.default_state()
    entry = {"title": "Post A", "url": "https://builder.aws.com/content/1"}

    store.record_activity(state, entry)
    store.record_activity(state, dict(entry))
    assert len(state["activities"]) == 1


def test_record_activity_dedupes_ignoring_field_order(tmp_path):
    store = ProgressStorage(tmp_path / "progress.json")
    state = store.default_state()
    store.record_activity(state, {"title": "Post A", "url": "https://x/1", "content": "a"})
    store.record_activity(state, {"content": "a", "url": "https://x/1", "title": "Post A"})
    assert len(state["activities"]) == 1


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------
def test_relevance_prefers_technical_content():
    technical = relevance_score("Understanding Lambda concurrency and cold starts", "")
    assert technical > 0
    assert technical > relevance_score("My weekly photo diary", "")


def test_engagement_bait_is_penalised():
    bait = relevance_score("AWS lambda tips - like and follow me for more!", "")
    genuine = relevance_score("AWS lambda concurrency control explained", "")
    assert bait < genuine


def test_rank_removes_seen_titles_and_sorts_descending():
    activities = [
        {"title": "AWS Lambda deep dive on concurrency", "url": "u1"},
        {"title": "My cat photo diary", "url": "u2"},
        {"title": "Serverless cost optimization with Well-Architected", "url": "u4"},
    ]
    ranked = rank_activities(activities, seen_titles={"aws lambda deep dive on concurrency"})

    titles = [a["title"] for a in ranked]
    assert "AWS Lambda deep dive on concurrency" not in titles
    assert "My cat photo diary" not in titles
    assert len(titles) == 1
    assert ranked[0]["relevance"] >= 4


def test_rank_deduplicates_repeated_titles_within_one_batch():
    """Two entries with the same title must collapse to a single result."""
    activities = [
        {"title": "Understanding AWS Lambda concurrency", "url": "u1"},
        {"title": "Understanding AWS Lambda concurrency", "url": "u2"},
    ]
    assert len(rank_activities(activities)) == 1


def test_rank_sorts_by_descending_relevance():
    activities = [
        {"title": "AWS Lambda", "url": "u1"},
        {"title": "AWS Lambda concurrency and cold start deep dive with benchmarks", "url": "u2"},
    ]
    ranked = rank_activities(activities)
    assert len(ranked) == 2
    assert ranked[0]["relevance"] > ranked[1]["relevance"]


def test_rank_respects_min_score():
    """min_score is the caller's knob; there is no hidden floor."""
    weak = [{"title": "lambda", "url": "u1"}]
    assert rank_activities(weak, min_score=2) == []
    assert len(rank_activities(weak, min_score=1)) == 1


def test_rank_returns_empty_for_all_noise():
    assert rank_activities([{"title": "hello world", "url": "u"}]) == []


def test_is_relevant_cheap_prefilter():
    assert is_relevant("Understanding AWS Lambda concurrency and cold starts") is True
    assert is_relevant("My cat photo diary") is False


def test_normalize_title():
    assert normalize_title("  AWS   Lambda  Tips ") == "aws lambda tips"
    assert normalize_title(None) == ""


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
def test_config_validate_requires_session(monkeypatch):
    monkeypatch.delenv("AWS_STORAGE_STATE_B64", raising=False)
    monkeypatch.delenv("BROWSER_CDP_ENDPOINT", raising=False)
    assert "AWS_STORAGE_STATE_B64" in Config.from_env().validate()


def test_config_session_optional_with_cdp(monkeypatch):
    """Attaching to a real browser supplies the session, so no state file."""
    monkeypatch.delenv("AWS_STORAGE_STATE_B64", raising=False)
    monkeypatch.setenv("BROWSER_CDP_ENDPOINT", "127.0.0.1:9222")
    assert Config.from_env().validate() == []


def test_config_validate_passes_with_session(monkeypatch):
    monkeypatch.delenv("BROWSER_CDP_ENDPOINT", raising=False)
    monkeypatch.setenv("AWS_STORAGE_STATE_B64", "e30=")
    assert Config.from_env().validate() == []


def test_config_warnings_are_non_fatal(monkeypatch):
    monkeypatch.delenv("AWS_STORAGE_STATE_B64", raising=False)
    monkeypatch.delenv("BROWSER_CDP_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    config = Config.from_env()
    # Telegram and OpenRouter missing => warnings, but not blocking errors.
    assert len(config.warnings()) == 2
    assert config.validate() == ["AWS_STORAGE_STATE_B64"]


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("MAX_DRAFTS", "5")
    monkeypatch.setenv("MIN_RELEVANCE", "9")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HEADLESS", "false")
    config = Config.from_env()
    assert config.max_drafts == 5
    assert config.min_relevance == 9
    assert config.dry_run is False
    assert config.headless is False


def test_config_ignores_invalid_int(monkeypatch):
    monkeypatch.setenv("MAX_DRAFTS", "not-a-number")
    assert Config.from_env().max_drafts == 3


def test_no_hardcoded_secrets_in_config_defaults():
    config = Config()
    assert config.openrouter_api_key == ""
    assert config.telegram_bot_token == ""
    assert config.storage_state_b64 == ""