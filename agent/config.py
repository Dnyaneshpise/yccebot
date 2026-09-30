"""Configuration for the AWS Builder Badge Agent.

Everything is environment-driven so the same code runs locally and in GitHub
Actions without edits. Secrets are read from the environment only - never from
files committed to the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool = True) -> bool:
    """Read a boolean from the environment."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Read an integer from the environment, ignoring invalid values."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Application configuration loaded from environment variables."""

    # --- OpenRouter ---
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemma-4-26b-a4b-it:free"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- AWS Builder Center ---
    builder_url: str = "https://builder.aws.com/"
    storage_state_b64: str = ""

    # --- Runtime / paths ---
    headless: bool = True
    progress_path: str = "data/progress.json"
    storage_state_path: str = "auth/storage_state.json"
    user_agent: str = ""

    # --- Behaviour tuning ---
    max_drafts: int = 3
    min_relevance: int = 4
    target_badges: int = 21
    dry_run: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_model=os.getenv(
                "OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free"
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            builder_url=os.getenv("BUILDER_URL", "https://builder.aws.com/"),
            storage_state_b64=os.getenv("AWS_STORAGE_STATE_B64", ""),
            headless=_env_bool("HEADLESS", True),
            progress_path=os.getenv("PROGRESS_PATH", "data/progress.json"),
            storage_state_path=os.getenv(
                "STORAGE_STATE_PATH", "auth/storage_state.json"
            ),
            user_agent=os.getenv("BROWSER_USER_AGENT", ""),
            max_drafts=_env_int("MAX_DRAFTS", 3),
            min_relevance=_env_int("MIN_RELEVANCE", 4),
            target_badges=_env_int("TARGET_BADGES", 21),
            dry_run=_env_bool("DRY_RUN", True),
        )

    def validate(self) -> list[str]:
        """Return the names of required-but-missing settings.

        An empty list means the agent can attempt a run. Missing Telegram or
        OpenRouter credentials degrade the run rather than blocking it, so only
        the AWS session is truly required for badge tracking.
        """
        missing: list[str] = []
        if not self.storage_state_b64:
            missing.append("AWS_STORAGE_STATE_B64")
        return missing

    def warnings(self) -> list[str]:
        """Return non-fatal configuration problems."""
        problems: list[str] = []
        if not self.openrouter_api_key:
            problems.append(
                "OPENROUTER_API_KEY is not set - activity drafting and summaries "
                "will be skipped"
            )
        if not self.telegram_bot_token or not self.telegram_chat_id:
            problems.append(
                "Telegram is not configured - notifications will only appear in logs"
            )
        return problems

    def resolve(self, relative: str) -> Path:
        """Resolve a possibly-relative config path against the project root."""
        path = Path(relative)
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def resolved_progress_path(self) -> Path:
        return self.resolve(self.progress_path)

    @property
    def resolved_storage_state_path(self) -> Path:
        return self.resolve(self.storage_state_path)


PROJECT_ROOT = Path(__file__).resolve().parent.parent