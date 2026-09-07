"""Exclusive session leases so agents do not collide on one phone."""

from __future__ import annotations

import secrets
from datetime import datetime

from devicefleet.models import SessionRecord, SessionStatus, utcnow
from devicefleet.store import YamlStore


class SessionError(RuntimeError):
    """Base error for session lifecycle problems."""


class SessionNotFoundError(SessionError):
    """No session exists for the given id."""


class DeviceBusyError(SessionError):
    """Another active session already holds this device."""


class SessionOwnershipError(SessionError):
    """Caller is not the agent that holds this lease."""


class SessionManager:
    """Create, attach, list, and release device sessions."""

    def __init__(self, store: YamlStore) -> None:
        self._store = store

    def _read_all(self) -> list[SessionRecord]:
        document = self._store.load()
        raw_items = document.get("sessions", [])
        if raw_items is None:
            return []
        if not isinstance(raw_items, list):
            raise ValueError("sessions.yaml must contain a list under 'sessions'")
        return [SessionRecord.model_validate(item) for item in raw_items]

    def _write_all(self, sessions: list[SessionRecord]) -> None:
        self._store.save(
            {"sessions": [session.model_dump(mode="json") for session in sessions]}
        )

    def list_sessions(self, active_only: bool = False) -> list[SessionRecord]:
        """Return sessions, newest first."""
        sessions = self._read_all()
        if active_only:
            sessions = [item for item in sessions if item.status == SessionStatus.ACTIVE]
        return sorted(sessions, key=lambda item: item.created_at, reverse=True)

    def get(self, session_id: str) -> SessionRecord:
        """Look up a session by id."""
        for session in self._read_all():
            if session.id == session_id:
                return session
        raise SessionNotFoundError(f"session not found: {session_id}")

    def active_for_device(self, device_id: str) -> SessionRecord | None:
        """Return the live lease on a device, if any."""
        for session in self._read_all():
            if session.device_id == device_id and session.status == SessionStatus.ACTIVE:
                return session
        return None

    def start(
        self,
        device_id: str,
        agent_label: str = "anonymous",
        metadata: dict[str, str] | None = None,
    ) -> SessionRecord:
        """Create a new exclusive session. Fails if the device is already leased."""
        if not device_id.strip():
            raise ValueError("device_id is required")
        busy = self.active_for_device(device_id)
        if busy is not None:
            raise DeviceBusyError(
                f"device {device_id} is held by session {busy.id} ({busy.agent_label})"
            )
        session = SessionRecord(
            id=_new_session_id(),
            device_id=device_id,
            agent_label=agent_label.strip() or "anonymous",
            metadata=metadata or {},
        )
        sessions = self._read_all()
        sessions.append(session)
        self._write_all(sessions)
        return session

    def attach(self, session_id: str, agent_label: str | None = None) -> SessionRecord:
        """Rejoin an existing active session. The agent label must match the owner."""
        session = self.get(session_id)
        if session.status != SessionStatus.ACTIVE:
            raise SessionError(f"session {session_id} is not active")
        caller = (agent_label or "").strip()
        if not caller:
            raise SessionOwnershipError(
                "agent_label is required to attach; a session id alone is not enough"
            )
        if caller != session.agent_label:
            raise SessionOwnershipError(
                f"session {session_id} belongs to {session.agent_label}, not {caller}"
            )
        return session

    def require_owner(self, session_id: str, agent_label: str | None) -> SessionRecord:
        """Return the session only if it is active and owned by this agent."""
        session = self.get(session_id)
        if session.status != SessionStatus.ACTIVE:
            raise SessionError(f"session {session_id} is not active")
        caller = (agent_label or "").strip()
        if not caller or caller != session.agent_label:
            raise SessionOwnershipError(
                f"session {session_id} is not owned by {caller or 'unknown agent'}"
            )
        return session

    def stop(self, session_id: str, when: datetime | None = None) -> SessionRecord:
        """Release a session so another agent can take the device."""
        session = self.get(session_id)
        if session.status == SessionStatus.RELEASED:
            return session
        updated = session.model_copy(
            update={
                "status": SessionStatus.RELEASED,
                "released_at": when or utcnow(),
            }
        )
        return self._replace(updated)

    def touch(self, session_id: str, when: datetime | None = None) -> SessionRecord:
        """Record that an action ran on this session."""
        session = self.get(session_id)
        if session.status != SessionStatus.ACTIVE:
            raise SessionError(f"session {session_id} is not active")
        updated = session.model_copy(update={"last_action_at": when or utcnow()})
        return self._replace(updated)

    def _replace(self, session: SessionRecord) -> SessionRecord:
        sessions = [item for item in self._read_all() if item.id != session.id]
        sessions.append(session)
        self._write_all(sessions)
        return session


def _new_session_id() -> str:
    return "ses_" + secrets.token_hex(6)
