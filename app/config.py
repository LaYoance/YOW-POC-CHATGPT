"""
Application Configuration & Settings using Pydantic Settings v2 with fallback.
"""

import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import pydantic_settings
    from pydantic_settings import BaseSettings as _BaseSettings, SettingsConfigDict as _SettingsConfigDict
    from pydantic import Field
    HAS_PYDANTIC_SETTINGS = True
except ImportError:
    HAS_PYDANTIC_SETTINGS = False
    from app.models.base import BaseModel as _BaseSettings, Field, ConfigDict as _SettingsConfigDict


class Settings(_BaseSettings):
    """Global configuration settings for ChatGPT Integration POC."""

    APP_NAME: str = "YOW-ChatGPT-Integration-POC"
    ENV: str = "development"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Local Storage & Security Isolation (R1, A7)
    BASE_DIR: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    SESSION_DATA_DIR: Path = Path(".session_data")
    DB_FILENAME: str = "poc.db"

    # SQLite Pragmas
    SQLITE_BUSY_TIMEOUT_MS: int = 5000
    SQLITE_SYNCHRONOUS: str = "NORMAL"

    # Transport & Session Timeouts (R4)
    DEFAULT_TRANSPORT_MODE: str = "BROWSER_FETCH"
    RECONNECT_TIMEOUT_SECONDS: int = 300
    UPSTREAM_BASE_URL: str = "https://chatgpt.com"
    BROWSER_HEADLESS: bool = True

    if HAS_PYDANTIC_SETTINGS:
        model_config = _SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )
    else:
        def __init__(self, **kwargs: Any):
            super().__init__(**kwargs)
            # Load environment overrides if present
            for field in [
                "APP_NAME", "ENV", "DEBUG", "HOST", "PORT", "SESSION_DATA_DIR",
                "DB_FILENAME", "SQLITE_BUSY_TIMEOUT_MS", "SQLITE_SYNCHRONOUS",
                "DEFAULT_TRANSPORT_MODE", "RECONNECT_TIMEOUT_SECONDS",
                "UPSTREAM_BASE_URL", "BROWSER_HEADLESS"
            ]:
                if field in os.environ:
                    val = os.environ[field]
                    if field in ("PORT", "SQLITE_BUSY_TIMEOUT_MS", "RECONNECT_TIMEOUT_SECONDS"):
                        setattr(self, field, int(val))
                    elif field in ("DEBUG", "BROWSER_HEADLESS"):
                        setattr(self, field, val.lower() in ("1", "true", "yes"))
                    elif field == "SESSION_DATA_DIR":
                        setattr(self, field, Path(val))
                    else:
                        setattr(self, field, val)

    @property
    def full_session_data_dir(self) -> Path:
        """Absolute path to isolated session directory."""
        if self.SESSION_DATA_DIR.is_absolute():
            return self.SESSION_DATA_DIR
        return self.BASE_DIR / self.SESSION_DATA_DIR

    @property
    def db_path(self) -> Path:
        """Absolute path to SQLite database."""
        return self.full_session_data_dir / self.DB_FILENAME

    def ensure_isolated_storage(self) -> None:
        """Enforces 0700 permissions on session data directory and 0600 on db file."""
        target_dir = self.full_session_data_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(target_dir, 0o700)
            except OSError:
                pass

            db_file = self.db_path
            if db_file.exists():
                try:
                    os.chmod(db_file, 0o600)
                except OSError:
                    pass

            wal_file = Path(f"{db_file}-wal")
            shm_file = Path(f"{db_file}-shm")
            if wal_file.exists():
                try:
                    os.chmod(wal_file, 0o600)
                except OSError:
                    pass
            if shm_file.exists():
                try:
                    os.chmod(shm_file, 0o600)
                except OSError:
                    pass

    def verify_git_isolation(self, repo_root: Optional[Path] = None) -> bool:
        """Verifies that .gitignore strictly excludes .session_data and *.db."""
        root = repo_root or self.BASE_DIR
        gitignore = root / ".gitignore"
        required_patterns = [r"\.session_data", r"\*\.db", r"\*\.sqlite"]
        if not gitignore.exists():
            return False
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
        for pat in required_patterns:
            if not any(re.search(pat, line) for line in lines):
                return False
        return True


settings = Settings()
