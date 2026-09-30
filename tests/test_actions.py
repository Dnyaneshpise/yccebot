

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

    assert is_decision_for("approve AAA", "BBB") is None
    assert is_decision_for("approve AAA", "AAA") == (True, "AAA")


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