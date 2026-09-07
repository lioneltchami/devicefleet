"""Shared data models for devices, sessions, and actions."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class ProviderKind(str, Enum):
    """Backend that can execute actions on a phone."""

    ADB = "adb"
    STUB = "stub"
    CLOUD = "cloud"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    UNKNOWN = "unknown"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"


class DiscoveredDevice(BaseModel):
    """A device reported by a provider before it is registered."""

    provider: ProviderKind
    provider_ref: str
    display_name: str
    status: DeviceStatus = DeviceStatus.ONLINE
    metadata: dict[str, str] = Field(default_factory=dict)
    suggested_id: str | None = None
    suggested_tags: list[str] = Field(default_factory=list)


class DeviceRecord(BaseModel):
    """A phone known to the fleet registry."""

    id: str
    display_name: str
    provider: ProviderKind
    provider_ref: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    registered_at: datetime = Field(default_factory=utcnow)
    last_seen: datetime | None = None
    last_status: DeviceStatus = DeviceStatus.UNKNOWN
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("device id must not be empty")
        if any(ch.isspace() for ch in cleaned):
            raise ValueError("device id must not contain whitespace")
        return cleaned

    @field_validator("registered_at", "last_seen", mode="before")
    @classmethod
    def _utc_datetimes(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for tag in value:
            item = tag.strip().lower()
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result


class SessionRecord(BaseModel):
    """Exclusive lease that binds one agent to one device."""

    id: str
    device_id: str
    agent_label: str = "anonymous"
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    last_action_at: datetime | None = None
    released_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ActionName(str, Enum):
    SCREENSHOT = "screenshot"
    TAP = "tap"
    SWIPE = "swipe"
    TYPE = "type"
    KEY = "key"
    DUMP_UI = "dump_ui"
    INFO = "info"


class ActionRequest(BaseModel):
    """Normalized action payload used by the CLI, API, and transports."""

    name: ActionName
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    duration_ms: int = 300
    text: str | None = None
    key: str | None = None


class ActionResult(BaseModel):
    """Outcome of a helper executed against a session."""

    ok: bool
    action: ActionName
    session_id: str
    device_id: str
    message: str = ""
    artifact_path: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class CloudDeviceSpec(BaseModel):
    """Hint used when a cloud provider provisions a hosted phone."""

    platform: Literal["android", "ios"] = "android"
    model: str | None = None
    os_version: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class DeviceListItem(BaseModel):
    """Registry device plus live occupancy for listings."""

    device: DeviceRecord
    status: DeviceStatus
    session_id: str | None = None
    agent_label: str | None = None
