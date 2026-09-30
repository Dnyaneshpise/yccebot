

# ----------------------------------------------------------------------
# Approval queue - the safety guarantee
# ----------------------------------------------------------------------
import pytest
def _proposal(kind="like", url="https://builder.aws.com/content/x", approved=None):
    from agent.actions import Proposal

    return Proposal(
        id="abc123", kind=kind, title="Test Article", url=url,
        draft="text" if kind == "comment" else "", badge="30-Day Like Streak",
        approved=approved,
    )


def test_proposal_defaults_to_pending():
    assert _proposal().approved is None


def test_store_roundtrip(tmp_path):
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    assert len(store.pending()) == 1
    assert store.approved() == []


def test_store_does_not_duplicate_pending_same_url(tmp_path):
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.add(_proposal())
    assert len(store.pending()) == 1


def test_decide_records_approval(tmp_path):
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.decide("abc123", True)
    approved = store.approved()
    assert len(approved) == 1
    assert approved[0].decided_at is not None


def test_decide_records_rejection(tmp_path):
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.decide("abc123", False)
    assert store.approved() == []
    assert store.pending() == []


def test_decide_unknown_id_is_noop(tmp_path):
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    assert store.decide("nope", True) is None


def test_store_corrupt_file_is_safe(tmp_path):
    from agent.actions import ApprovalStore

    path = tmp_path / "approvals.json"
    path.write_text("{broken", encoding="utf-8")
    assert ApprovalStore(path).all() == []


def test_result_marks_action_as_done(tmp_path):
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.decide("abc123", True)
    store.set_result("abc123", "OK: liked")
    assert store.approved() == []          # no longer pending execution


async def test_run_approved_refuses_unapproved():
    """THE critical guarantee: pending proposals must never execute."""
    from agent.actions import run_approved

    called = []

    async def handler(p):
        called.append(p.id)
        return "OK: did it"

    results = await run_approved(None, _FakeStore([]), {"like": handler})
    assert called == []
    assert results == []


async def test_run_approved_executes_only_when_approved(tmp_path):
    from agent.actions import ApprovalStore, run_approved

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.decide("abc123", True)

    called = []

    async def handler(p):
        called.append(p.id)
        return "OK: liked"

    results = await run_approved(None, store, {"like": handler})
    assert called == ["abc123"]
    assert results == [("abc123", "OK: liked")]


async def test_run_approved_handles_rejected(tmp_path):
    from agent.actions import ApprovalStore, run_approved

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.decide("abc123", False)

    called = []
    results = await run_approved(None, store, {"like": lambda p: called.append(p.id) or "OK"})
    assert called == [] and results == []


async def test_run_approved_records_failure_without_crashing(tmp_path):
    from agent.actions import ApprovalStore, run_approved

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal())
    store.decide("abc123", True)

    async def boom(p):
        raise RuntimeError("network down")

    results = await run_approved(None, store, {"like": boom})
    assert "FAILED" in results[0][1]
    assert "network down" in results[0][1]


async def test_run_approved_refuses_missing_handler(tmp_path):
    from agent.actions import ApprovalStore, run_approved

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(_proposal(kind="comment", url="https://x/2"))
    store.decide("abc123", True)

    results = await run_approved(None, store, {"like": lambda p: "OK"})
    assert "REFUSED" in results[0][1]


async def test_execute_refuses_without_approval():
    """execute() double-checks approval even if a caller skips the store."""
    from agent.actions import execute

    result = await execute(None, _proposal(approved=None))
    assert result.startswith("REFUSED")


async def test_execute_refuses_comment_with_no_text():
    from agent.actions import execute

    p = _proposal(kind="comment", approved=True)
    p.draft = "   "
    result = await execute(None, p)
    assert result.startswith("REFUSED")


async def test_execute_refuses_unknown_kind():
    from agent.actions import execute

    result = await execute(None, _proposal(kind="teleport", approved=True))
    assert result.startswith("REFUSED")


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def approved(self):
        return list(self._rows)

# ----------------------------------------------------------------------
# Approval command parsing
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["approve abc123", "/approve abc123", "APPROVE abc123", " yes abc123 ", "do abc123"],
)
def test_parse_approve(text):
    from agent.actions import parse_decision

    assert parse_decision(text) == ("approve", "abc123")


@pytest.mark.parametrize(
    "text",
    ["reject abc123", "/reject abc123", "no abc123", "skip abc123"],
)
def test_parse_reject(text):
    from agent.actions import parse_decision

    assert parse_decision(text) == ("reject", "abc123")


@pytest.mark.parametrize("text", ["", "hello", "approve", "maybe abc123", "approve!!", None])
def test_parse_ignores_ambiguous(text):
    from agent.actions import parse_decision

    assert parse_decision(text) is None


def test_is_decision_for_rejects_wrong_id():
    """An approval for proposal A must never apply to proposal B."""
    from agent.actions import is_decision_for

    assert is_decision_for("approve aaa111", "bbb222") is None
    assert is_decision_for("approve aaa111", "aaa111") == (True, "aaa111")


# ----------------------------------------------------------------------
# Badge targeting
# ----------------------------------------------------------------------
def test_next_actions_ranks_closest_first():
    from agent.actions import next_actions

    badges = [
        {"name": "30-Day Like Streak", "status": "IN_PROGRESS", "count": 10, "threshold": 30},
        {"name": "7-Day Comment Streak", "status": "NOT_STARTED", "count": 0, "threshold": 7},
    ]
    assert next_actions(badges) == ["30-Day Like Streak", "7-Day Comment Streak"]


def test_next_actions_excludes_granted_and_social():
    from agent.actions import next_actions

    badges = [
        {"name": "30-Day Like Streak", "status": "GRANTED", "count": 30, "threshold": 30},
        {"name": "Valued Creator", "status": "IN_PROGRESS", "count": 2, "threshold": 5},
        {"name": "First Wish", "status": "NOT_STARTED", "count": 0, "threshold": 1},
    ]
    assert next_actions(badges) == ["First Wish"]


def test_plan_for_badge_unknown():
    from agent.actions import plan_for_badge

    assert plan_for_badge("Nonexistent Badge") is None
    assert plan_for_badge("First Article")[0] == "article"


def test_proposal_describe_contains_decision_fields():
    from agent.actions import Proposal

    p = Proposal(id="zz9", kind="like", title="Cool post",
                 url="https://builder.aws.com/content/z", badge="30-Day Like Streak")
    text = p.describe()
    assert "Cool post" in text
    assert "30-Day Like Streak" in text
    assert "https://builder.aws.com/content/z" in text

# ----------------------------------------------------------------------
# Interface guarantees - these caught a real regression where a
# refactor silently deleted the whole engagement block.
# ----------------------------------------------------------------------
def test_builder_exposes_required_methods():
    from agent.builder import BuilderCenter

    for name in (
        "initialize", "close", "goto", "is_authenticated",
        "get_badge_count", "get_badge_progress", "fetch_badge_progress",
        "discover_activities", "scrape_activity_content",
        "like_article", "post_comment", "vote_on_wish",
        "_dismiss_consent", "_find_action_control", "is_real_article",
    ):
        assert hasattr(BuilderCenter, name), f"BuilderCenter.{name} is missing"


def test_execute_handlers_all_exist():
    """Every handler main.py registers must be a real method."""
    import inspect
    from agent.builder import BuilderCenter

    for kind in ("like", "comment", "vote"):
        method = {"like": "like_article", "comment": "post_comment", "vote": "vote_on_wish"}[kind]
        assert inspect.iscoroutinefunction(getattr(BuilderCenter, method))


def test_vote_on_wish_refuses_rather_than_guessing():
    """Unverified selectors must not be clicked at."""
    import asyncio
    from agent.builder import BuilderCenter

    b = BuilderCenter()
    result = asyncio.run(b.vote_on_wish("https://builder.aws.com/wishes/1"))
    assert result.startswith("UNSUPPORTED")


async def test_like_article_without_page_fails_safely():
    from agent.builder import BuilderCenter

    b = BuilderCenter()
    assert "FAILED" in await b.like_article("https://builder.aws.com/content/x")


async def test_post_comment_empty_text_refused():
    from agent.builder import BuilderCenter

    b = BuilderCenter()
    assert "REFUSED" in await b.post_comment("https://builder.aws.com/content/x", "   ")

def test_is_real_article_accepts_relative_and_absolute():
    """Builder Center hrefs are often relative - both forms must pass."""
    from agent.builder import BuilderCenter

    b = BuilderCenter()
    assert b.is_real_article({"url": "/content/3JNtXeZax1v5f73FkGvm6EBoQrz/some-title"})
    assert b.is_real_article(
        {"url": "https://builder.aws.com/content/3JNtXeZax1v5f73FkGvm6EBoQrz/some-title"}
    )
    assert b.is_real_article({"url": "/discussion/abc123"})


def test_is_real_article_rejects_chrome():
    from agent.builder import BuilderCenter

    b = BuilderCenter()
    for bad in (
        {"url": "/build/workshops", "title": "Workshops"},
        {"url": "/community", "title": "Community"},
        {"url": "/wishlist"},
        {"url": "javascript:void(0)"},
        {"url": "#"},
        {"url": ""},
    ):
        assert b.is_real_article(bad) is False, bad

# ----------------------------------------------------------------------
# Badge API permission handling
# ----------------------------------------------------------------------
def test_is_permission_denied_detects_iam_deny():
    from agent.builder import _is_permission_denied

    body = ("User is not authorized to access this resource with an explicit "
            "deny in an identity-based policy")
    assert _is_permission_denied(403, body) is True
    assert _is_permission_denied(401, body) is True


def test_is_permission_denied_ignores_token_errors():
    """A stale token is worth retrying; an identity deny is not."""
    from agent.builder import _is_permission_denied

    assert _is_permission_denied(401, "[Unauthorized] Unauthorized.") is False
    assert _is_permission_denied(403, "Missing Authentication Token") is False
    assert _is_permission_denied(404, "not found") is False
    assert _is_permission_denied(429, "rate limited") is False
    assert _is_permission_denied(200, "") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("You have 5 of 21 badges", {"granted": 5, "total": 21}),
        ("5 of 21 badges earned", {"granted": 5, "total": 21}),
        ("Badges: 7 of 21", {"granted": 7, "total": 21}),
        ("3/21 badges complete", {"granted": 3, "total": 21}),
    ],
)
def test_parse_badge_text(text, expected):
    from agent.builder import BuilderCenter

    parsed = BuilderCenter._parse_badge_text(text)
    assert parsed is not None
    assert parsed["granted"] == expected["granted"]
    assert parsed["total"] == expected["total"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Sign in to AWS Builder Center",
        "This article has 2474 likes and 1782 comments",   # a bare number
        "21 of 5 badges",                                 # granted > total
        "no numbers at all here",
    ],
)
def test_parse_badge_text_refuses_ambiguous(text):
    """Never infer a badge count from unrelated numbers."""
    from agent.builder import BuilderCenter

    assert BuilderCenter._parse_badge_text(text) is None


def test_permission_denied_starts_false():
    from agent.builder import BuilderCenter

    assert BuilderCenter()._permission_denied is False

# ----------------------------------------------------------------------
# Reading approval replies back from Telegram
# ----------------------------------------------------------------------
def test_parse_decision_accepts_bare_id():
    """Users reply with just the id, so that must work."""
    from agent.actions import parse_decision

    assert parse_decision("22e20ddcac") == ("approve", "22e20ddcac")


@pytest.mark.parametrize("text", ["thanks", "ok", "hello", "/start", "sure", "1", "yes please"])
def test_parse_decision_ignores_non_ids(text):
    """Ordinary chat must never be read as an approval."""
    from agent.actions import parse_decision

    assert parse_decision(text) is None


def _update(text, chat="1554312544", uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_parse_incoming_decisions():
    from agent.telegram import TelegramNotifier

    t = TelegramNotifier("123:ABC", "1554312544")
    updates = [
        _update("/start", uid=1),
        _update("22e20ddcac", uid=2),
        _update("reject abc123", uid=3),
    ]
    got = t.parse_incoming_decisions(updates)
    assert [(d, p) for d, p, _ in got] == [("approve", "22e20ddcac"), ("reject", "abc123")]


def test_parse_incoming_ignores_other_chats():
    """Someone else in another chat must not be able to approve actions."""
    from agent.telegram import TelegramNotifier

    t = TelegramNotifier("123:ABC", "1554312544")
    assert t.parse_incoming_decisions([_update("22e20ddcac", chat="999")]) == []


def test_parse_incoming_handles_empty():
    from agent.telegram import TelegramNotifier

    t = TelegramNotifier("123:ABC", "1554312544")
    assert t.parse_incoming_decisions([]) == []
    assert t.parse_incoming_decisions([{"update_id": 1}]) == []


def test_get_updates_unconfigured_is_safe():
    from agent.telegram import TelegramNotifier

    assert TelegramNotifier("", "").get_updates() == []


async def test_drain_inbox_records_and_executes(monkeypatch, tmp_path):
    """A Telegram reply must become a recorded approval, then an action."""
    import agent.main as m
    from agent.actions import ApprovalStore, Proposal

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(Proposal(id="abc123", kind="like", title="T", url="https://x/1"))

    class FakeTelegram:
        def __init__(self):
            self.acked = None

        def get_updates(self, *a, **k):
            return [_update("abc123", uid=7)]

        def acknowledge(self, last):
            self.acked = last

        def parse_incoming_decisions(self, updates):
            return [("approve", "abc123", 7)]

        def send_message(self, *a, **k):
            return True

        def send_decision_ack(self, *a, **k):
            self.acks = getattr(self, "acks", [])
            self.acks.append(a[0])
            return True

        def send_execution_report(self, *a, **k):
            return True

    tg = FakeTelegram()
    done = []

    class FakeBuilder:
        async def like_article(self, url):
            done.append(url)
            return "OK: liked"

    result = await m.drain_telegram_inbox(None, FakeBuilder(), tg, store)
    assert result == 1
    assert done == ["https://x/1"]
    assert tg.acked == 7
    assert store.get("abc123").result == "OK: liked"


async def test_drain_inbox_ignores_unknown_id(tmp_path):
    """A reply for a proposal we do not have must be dropped, not acted on."""
    import agent.main as m
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")

    class FakeTelegram:
        def get_updates(self, *a, **k):
            return [_update("deadbeef", uid=3)]

        def acknowledge(self, last):
            pass

        def parse_incoming_decisions(self, updates):
            return [("approve", "deadbeef", 3)]

        def send_message(self, *a, **k):
            return True

        def send_decision_ack(self, *a, **k):
            return True

    called = []

    class FakeBuilder:
        async def like_article(self, url):
            called.append(url)
            return "OK"

    assert await m.drain_telegram_inbox(None, FakeBuilder(), FakeTelegram(), store) == 0
    assert called == []


async def test_drain_inbox_rejects_perform_nothing(tmp_path):
    import agent.main as m
    from agent.actions import ApprovalStore, Proposal

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(Proposal(id="abc123", kind="like", title="T", url="https://x/1"))

    class FakeTelegram:
        def get_updates(self, *a, **k):
            return [_update("reject abc123", uid=4)]

        def acknowledge(self, last):
            pass

        def parse_incoming_decisions(self, updates):
            return [("reject", "abc123", 4)]

        def send_decision_ack(self, *a, **k):
            return True

    called = []

    class FakeBuilder:
        async def like_article(self, url):
            called.append(url)
            return "OK"

    assert await m.drain_telegram_inbox(None, FakeBuilder(), FakeTelegram(), store) == 0
    assert called == []

def test_like_article_reports_failure_not_false_success():
    """A silently-discarded click must never be reported as a success."""
    import asyncio
    from agent.builder import BuilderCenter

    b = BuilderCenter()
    result = asyncio.run(b.like_article("https://builder.aws.com/content/abc123/x"))
    # no page attached, so nothing can be liked
    assert result.startswith("FAILED")
    assert "OK: liked" not in result

# ----------------------------------------------------------------------
# Proposals must be actionable before they are offered
# ----------------------------------------------------------------------
def test_empty_draft_proposal_is_refused_at_execution(tmp_path):
    """An approved comment with no text must never be posted."""
    import asyncio

    from agent.actions import ApprovalStore, Proposal, run_approved

    store = ApprovalStore(tmp_path / "approvals.json")
    p = Proposal(id="e1", kind="comment", title="T", url="https://x/1",
                 draft="", approved=True)
    store._write([p.to_dict()])

    posted = []

    class FakeBuilder:
        async def post_comment(self, url, text):
            posted.append((url, text))
            return "OK: comment posted"

    async def handler(proposal):
        return await FakeBuilder().post_comment(proposal.url, proposal.draft)

    results = asyncio.run(run_approved(None, store, {"comment": handler}))
    assert "REFUSED" in results[0][1]
    assert posted == []


def test_blank_draft_is_rejected_when_building_proposals(monkeypatch, tmp_path):
    """A comment proposal is skipped when the model produced no text."""
    import asyncio

    import agent.main as m
    from agent.actions import ApprovalStore

    store = ApprovalStore(tmp_path / "approvals.json")
    sent = []

    class FakeTelegram:
        def send_approval_request(self, proposal):
            sent.append(proposal)
            return 1

    class FakeBuilder:
        page = None

    class FakeAI:
        def is_configured(self):
            return True

    async def fake_find(*a, **k):
        return [{"activity": {"title": "Some Article", "url": "https://x/1"}, "comment": ""}]

    monkeypatch.setattr(m, "find_opportunities", fake_find)

    state = {"badge_details": [
        {"name": "Discussion Debut", "status": "NOT_STARTED", "count": 0, "threshold": 1}
    ]}
    created = asyncio.run(
        m.propose_actions(FakeBuilder(), FakeTelegram(), FakeAI(), state, store, limit=1)
    )
    assert created == []
    assert sent == []

@pytest.mark.parametrize(
    ("kind", "draft", "url", "expected"),
    [
        ("comment", "", "https://x/1", "no comment text"),
        ("comment", "   ", "https://x/1", "no comment text"),
        ("article", "", "https://x/1", "no comment text"),
        ("like", "", "", "no url"),
        ("teleport", "", "https://x/1", "unsupported"),
    ],
)
def test_validate_for_execution_refuses(kind, draft, url, expected):
    from agent.actions import Proposal, validate_for_execution

    p = Proposal(id="v1", kind=kind, title="T", url=url, draft=draft, approved=True)
    reason = validate_for_execution(p)
    assert reason is not None and expected in reason


def test_validate_allows_a_real_like():
    from agent.actions import Proposal, validate_for_execution

    p = Proposal(id="v2", kind="like", title="T", url="https://x/1", approved=True)
    assert validate_for_execution(p) is None


def test_validate_requires_approval():
    from agent.actions import Proposal, validate_for_execution

    p = Proposal(id="v3", kind="like", title="T", url="https://x/1", approved=None)
    assert "no human approval" in (validate_for_execution(p) or "")