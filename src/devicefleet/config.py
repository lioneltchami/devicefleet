"""Fleet configuration loaded from env vars and an optional YAML file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def default_home() -> Path:
    """Directory that holds registry, sessions, and screenshots."""
    return Path.home() / ".devicefleet"


class Settings(BaseSettings):
    """Runtime settings. All fields can be set via DEVICEFLEET_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="DEVICEFLEET_",
        env_file=".env",
        extra="ignore",
    )

    home: Path = Field(default_factory=default_home)
    remote_url: str | None = None
    default_provider: str = "adb"
    adb_bin: str = "adb"
    adb_timeout_s: float = 30.0
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None
    agent: str = "anonymous"
    current_session: str | None = None

    @property
    def devices_path(self) -> Path:
        return self.home / "devices.yaml"

    @property
    def sessions_path(self) -> Path:
        return self.home / "sessions.yaml"

    @property
    def state_path(self) -> Path:
        return self.home / "state.yaml"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def stub_state_path(self) -> Path:
        return self.home / "stub-state.yaml"

    def ensure_dirs(self) -> None:
        """Create the data directory tree if it does not exist."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)


def load_settings(home: Path | None = None) -> Settings:
    """Load settings, optionally overriding the data directory."""
    settings = Settings() if home is None else Settings(home=home)
    settings.ensure_dirs()
    return settings
