"""Write executors: the one swappable piece of the agent.

Everything else - scheduling, badge tracking, discovery, drafting,
Telegram, state - runs anywhere, including free cloud. The only part that
needs a trusted browser is *acting* on the community: liking, commenting
and voting.

That split lives here. A caller asks an executor to carry out an approved
action; it does not care which executor it got.

Implementations
---------------
BrowserExecutor
    Performs the action through a real browser session (an attached Brave
    via CDP, or a launched one). This is the only kind that has worked
    against Builder Center so far.

DeferredExecutor
    Performs nothing. It records the approved action and reports it as
    deferred. This is the default on cloud runners, where no genuine
    session exists; the approval stays queued and is executed later by
    whatever executor is available.

Swapping in a future executor means implementing ``WriteExecutor`` and
selecting it in config. No other module changes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

DEFERRED = "DEFERRED"


@runtime_checkable
class WriteExecutor(Protocol):
    """Performs approved community actions.

    Implementations must never act on anything the user has not approved;
    the approval gate lives in :mod:`agent.actions` and is checked before
    any executor is called.
    """

    name: str

    async def like(self, url: str) -> str:
        """Like the content at ``url``."""

    async def comment(self, url: str, text: str) -> str:
        """Post ``text`` as a comment on ``url``."""

    async def vote(self, url: str) -> str:
        """Vote on the wish at ``url``."""

    async def close(self) -> None:
        """Release any resources held."""


class BrowserExecutor:
    """Carries out actions in a real, signed-in browser session."""

    name = "browser"

    def __init__(self, builder: Any) -> None:
        self.builder = builder

    async def like(self, url: str) -> str:
        return await self.builder.like_article(url)

    async def comment(self, url: str, text: str) -> str:
        return await self.builder.post_comment(url, text)

    async def vote(self, url: str) -> str:
        return await self.builder.vote_on_wish(url)

    async def close(self) -> None:
        # The caller owns the browser lifecycle; nothing to release here.
        return None


class DeferredExecutor:
    """Records the intent without performing it.

    Used where no genuine session exists (CI runners). Keeping the
    approval recorded means nothing is lost: the action is still pending
    and a later run with a real executor can pick it up.
    """

    name = "deferred"

    def __init__(self, reason: str = "no trusted browser session available") -> None:
        self.reason = reason
        self.deferred: list[dict[str, Any]] = []

    def _record(self, kind: str, url: str, detail: str = "") -> str:
        self.deferred.append({"kind": kind, "url": url, "detail": detail})
        return f"{DEFERRED}: {self.reason}"

    async def like(self, url: str) -> str:
        return self._record("like", url)

    async def comment(self, url: str, text: str) -> str:
        return self._record("comment", url, f"{len(text or '')} chars")

    async def vote(self, url: str) -> str:
        return self._record("vote", url)

    async def close(self) -> None:
        return None


def build_executor(builder: Any, writes_enabled: bool) -> WriteExecutor:
    """Choose an executor.

    ``writes_enabled`` should be true only where a genuine signed-in
    browser is actually reachable. On a cloud runner it is false, and the
    agent still does everything else.
    """
    if writes_enabled and builder is not None:
        return BrowserExecutor(builder)
    return DeferredExecutor()