"""Settings loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import REPO_ROOT

# Hosted Bot API limits (https://core.telegram.org/bots/api#sending-files)
CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024
# Local Bot API server limit
LOCAL_FILE_LIMIT = 2000 * 1024 * 1024

MAX_OUTPUT_SECONDS = 180.0
OUTPUT_W, OUTPUT_H, OUTPUT_FPS = 1080, 1920, 30
PREVIEW_W, PREVIEW_H = 720, 1280


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value and value.strip() else default
    except ValueError:
        return default


@dataclass
class Settings:
    bot_token: str = ""
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    agent_backend: str = "claude"
    agent_model: str = ""
    agent_timeout_s: int = 1800
    agent_max_turns: int = 60
    lean_mode: bool = True
    self_eval_cap: int = 10
    elevenlabs_api_key: str = ""
    jobs_dir: Path = REPO_ROOT / "jobs"
    cleanup_after_hours: int = 24
    telegram_api_base_url: str = ""
    telegram_api_data_dir: Path = REPO_ROOT / "tg-bot-api-data"

    @property
    def local_bot_api(self) -> bool:
        return bool(self.telegram_api_base_url)

    @property
    def download_limit(self) -> int:
        return LOCAL_FILE_LIMIT if self.local_bot_api else CLOUD_DOWNLOAD_LIMIT

    @property
    def upload_limit(self) -> int:
        return LOCAL_FILE_LIMIT if self.local_bot_api else CLOUD_UPLOAD_LIMIT

    @property
    def db_path(self) -> Path:
        return self.jobs_dir / "reelbot.sqlite3"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or REPO_ROOT / ".env")
        e = os.environ.get
        ids = frozenset(
            int(x) for x in (e("ALLOWED_USER_IDS") or "").replace(" ", "").split(",") if x.isdigit()
        )

        def _path(name: str, default: Path) -> Path:
            raw = e(name)
            p = Path(raw) if raw else default
            return p if p.is_absolute() else (REPO_ROOT / p).resolve()

        backend = (e("AGENT_BACKEND") or "claude").strip().lower()
        if backend not in {"claude", "codex"}:
            raise ValueError(f"AGENT_BACKEND must be 'claude' or 'codex', got {backend!r}")
        return cls(
            bot_token=(e("TELEGRAM_BOT_TOKEN") or "").strip(),
            allowed_user_ids=ids,
            agent_backend=backend,
            agent_model=(e("AGENT_MODEL") or "").strip(),
            agent_timeout_s=_int(e("AGENT_TIMEOUT_S"), 1800),
            agent_max_turns=_int(e("AGENT_MAX_TURNS"), 60),
            lean_mode=_bool(e("LEAN_MODE"), True),
            self_eval_cap=_int(e("SELF_EVAL_CAP"), 10),
            elevenlabs_api_key=(e("ELEVENLABS_API_KEY") or "").strip(),
            jobs_dir=_path("JOBS_DIR", REPO_ROOT / "jobs"),
            cleanup_after_hours=_int(e("CLEANUP_AFTER_HOURS"), 24),
            telegram_api_base_url=(e("TELEGRAM_API_BASE_URL") or "").strip().rstrip("/"),
            telegram_api_data_dir=_path("TELEGRAM_API_DATA_DIR", REPO_ROOT / "tg-bot-api-data"),
        )

    @property
    def effective_self_eval_cap(self) -> int:
        """LEAN_MODE hard-caps screenshot checks at 10."""
        return min(self.self_eval_cap, 10) if self.lean_mode else self.self_eval_cap
