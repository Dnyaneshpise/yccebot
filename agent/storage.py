"""Persistent progress storage (JSON on disk)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rewards import compute_milestones

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 1,
    "badges": None,
    "last_run": None,
    "milestones": {"7": False, "14": False, "21": False},
    "activities": [],
    "history": [],
}


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ProgressStorage:
    """Loads and saves agent progress in a JSON file.

    The store is deliberately forgiving: a missing or corrupt file is replaced
    by a fresh default state instead of crashing the whole run.
    """

    def __init__(self, path: str | os.PathLike[str] = "data/progress.json"):
        self.path = Path(path)

    def default_state(self) -> dict[str, Any]:
        """Return a deep-ish copy of the default state."""
        state = json.loads(json.dumps(DEFAULT_STATE))
        state["milestones"] = {k: bool(v) for k, v in DEFAULT_STATE["milestones"].items()}
        state["activities"] = []
        state["history"] = []
        return state

    def load(self) -> dict[str, Any]:
        """Load state from disk, falling back to defaults on any error."""
        if not self.path.exists():
            return self.default_state()

        try:
            raw = self.path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            return self.default_state()

        if not isinstance(data, dict):
            return self.default_state()

        return self._normalize(data)

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Merge stored data over defaults and coerce malformed fields."""
        state = self.default_state()
        state.update(data)

        badges = state.get("badges")
        if not isinstance(badges, int) or isinstance(badges, bool) or badges < 0:
            state["badges"] = None

        for key in ("activities", "history"):
            if not isinstance(state.get(key), list):
                state[key] = []

        milestones = state.get("milestones")
        if not isinstance(milestones, dict):
            milestones = {}
        state["milestones"] = {k: bool(milestones.get(k, False)) for k in ("7", "14", "21")}

        return state

    def save(self, state: dict[str, Any]) -> None:
        """Write state to disk atomically (write temp file, then replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=False)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)

    def record_badge_count(
        self, state: dict[str, Any], badge_count: int | None
    ) -> tuple[dict[str, Any], bool]:
        """Apply a freshly observed badge count.

        Returns ``(state, changed)`` where ``changed`` is True when the stored
        count actually moved. Unknown counts (``None``) leave the stored value
        untouched so we never overwrite good data with a failed scrape.
        """
        state["last_run"] = utcnow_iso()

        if badge_count is None:
            return state, False

        previous = state.get("badges")
        state["badges"] = badge_count
        state["milestones"] = compute_milestones(badge_count)

        changed = previous != badge_count
        if changed:
            history = state.setdefault("history", [])
            history.append({"timestamp": state["last_run"], "badges": badge_count})
        return state, changed

    def record_activity(self, state: dict[str, Any], activity: dict[str, Any]) -> None:
        """Append a discovered activity to the (bounded) activity log."""
        activities = state.setdefault("activities", [])
        key = activity.get("url") or activity.get("title")
        for existing in activities:
            if existing.get("url") and existing.get("url") == activity.get("url"):
                return
            if key is None:
                return
        activities.append(activity)
        del activities[:-200]