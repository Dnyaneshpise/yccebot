"""Tests for Telegram redaction, AI error handling, and session decoding."""

from __future__ import annotations

import base64
import io
import json
from urllib import error

import pytest

from agent.ai import AIAssistant, AIError, _extract_content, _hint_for_status
from agent.main import write_storage_state
from agent.config import Config
from agent.telegram import TelegramNotifier


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def test_telegram_requires_configuration():
    assert TelegramNotifier("", "").is_configured() is False
    assert TelegramNotifier("123:ABC", "42").is_configured() is True


def test_telegram_redacts_token_from_text():
    notifier = TelegramNotifier("123456:SECRETTOKENVALUE", "42")
    message = "login failed for bot 123456:SECRETTOKENVALUE"
    redacted = notifier.redact(message)
    assert "SECRETTOKENVALUE" not in redacted
    assert "***REDACTED***" in redacted


def test_telegram_redacts_chat_id():
    notifier = TelegramNotifier("123456:SECRETTOKENVALUE", "99887766")
    assert "99887766" not in notifier.redact("chat 99887766 failed")


def test_telegram_never_sends_when_unconfigured(monkeypatch):
    """An unconfigured bot must not make any network call."""
    import agent.telegram as tg

    def explode(*args, **kwargs):  # pragma: no cover
        raise AssertionError("network call attempted while unconfigured")

    monkeypatch.setattr(tg.request, "urlopen", explode)
    assert TelegramNotifier("", "").send_message("hello") is False


def test_telegram_send_error_redacts(monkeypatch):
    sent: list[str] = []
    notifier = TelegramNotifier("123456:SECRETTOKENVALUE", "42")
    monkeypatch.setattr(notifier, "send_message", lambda text: sent.append(text) or True)

    notifier.send_error("boom 123456:SECRETTOKENVALUE")
    assert "SECRETTOKENVALUE" not in sent[0]


# ----------------------------------------------------------------------
# AI
# ----------------------------------------------------------------------
def test_ai_requires_key_and_model():
    assert AIAssistant("", "").is_configured() is False
    assert AIAssistant("key", "").is_configured() is False
    assert AIAssistant("key", "model").is_configured() is True


def test_ai_raises_when_unconfigured():
    with pytest.raises(AIError, match="not configured"):
        AIAssistant("", "").summarize_activity("title")


def test_extract_content_reads_standard_response():
    body = json.dumps({"choices": [{"message": {"content": "hello world"}}]})
    assert _extract_content(body, "m") == "hello world"


def test_extract_content_reads_list_content():
    body = json.dumps(
        {"choices": [{"message": {"content": [{"type": "text", "text": "part1"}, {"text": "part2"}]}}]}
    )
    assert _extract_content(body, "m") == "part1part2"


def test_extract_content_raises_on_error_payload():
    body = json.dumps({"error": {"message": "model not found"}})
    with pytest.raises(AIError, match="model not found"):
        _extract_content(body, "m")


def test_extract_content_raises_on_missing_choices():
    with pytest.raises(AIError, match="missing choices"):
        _extract_content(json.dumps({"unexpected": 1}), "m")


def test_extract_content_raises_on_empty_completion():
    body = json.dumps({"choices": [{"message": {"content": "   "}}]})
    with pytest.raises(AIError, match="empty completion"):
        _extract_content(body, "m")


def test_extract_content_raises_on_non_json():
    with pytest.raises(AIError, match="non-JSON"):
        _extract_content("<html>gateway error</html>", "m")


@pytest.mark.parametrize(
    ("code", "fragment"),
    [(401, "OPENROUTER_API_KEY"), (404, "OPENROUTER_MODEL"), (429, "rate limited")],
)
def test_status_hints_are_actionable(code, fragment):
    assert fragment in _hint_for_status(code)


def test_chat_sanitizes_http_error(monkeypatch):
    """API errors must be readable but must not leak the key."""
    body = io.BytesIO(json.dumps({"error": {"message": "insufficient credits"}}).encode())
    exc = error.HTTPError("u", 402, "Payment Required", {}, body)

    monkeypatch.setattr("agent.ai.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(exc))

    assistant = AIAssistant("sk-secret-key", "some/model")
    with pytest.raises(AIError) as info:
        assistant.draft_comment("title", "summary")

    message = str(info.value)
    assert "insufficient credits" in message
    assert "sk-secret-key" not in message


# ----------------------------------------------------------------------
# Session decoding
# ----------------------------------------------------------------------
def _valid_state() -> str:
    payload = {"cookies": [{"name": "session-id", "value": "abc", "domain": ".amazon.com"}], "origins": []}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_write_storage_state_creates_file(tmp_path, monkeypatch):
    target = tmp_path / "auth" / "storage_state.json"
    monkeypatch.setenv("AWS_STORAGE_STATE_B64", _valid_state())

    config = Config(storage_state_b64=_valid_state(), storage_state_path=str(target))
    written = write_storage_state(config)

    assert written == target
    assert json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["name"] == "session-id"


def test_write_storage_state_returns_none_when_unset():
    assert write_storage_state(Config(storage_state_b64="")) is None


def test_write_storage_state_rejects_bad_base64(tmp_path):
    from agent.builder import AuthExpiredError

    config = Config(storage_state_b64="!!!not-base64!!!", storage_state_path=str(tmp_path / "s.json"))
    with pytest.raises(AuthExpiredError, match="base64"):
        write_storage_state(config)


def test_write_storage_state_rejects_wrong_json_shape(tmp_path):
    from agent.builder import AuthExpiredError

    encoded = base64.b64encode(json.dumps({"hello": "world"}).encode()).decode()
    config = Config(storage_state_b64=encoded, storage_state_path=str(tmp_path / "s.json"))
    with pytest.raises(AuthExpiredError, match="cookies"):
        write_storage_state(config)