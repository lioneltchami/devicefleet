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

    def _parse(self, document: dict[str, object]) -> list[SessionRecord]:
        raw_items = document.get("sessions", [])
        if raw_items is None:
            return []
        if not isinstance(raw_items, list):
            raise ValueError("sessions.yaml must contain a list under 'sessions'")
        return [SessionRecord.model_validate(item) for item in raw_items]

    def _dump(self, sessions: list[SessionRecord]) -> dict[str, object]:
        return {"sessions": [session.model_dump(mode="json") for session in sessions]}

    def _read_all(self) -> list[SessionRecord]:
        return self._parse(self._store.load())

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
        """Create a new exclusive session. Check-and-save is atomic under the store lock."""
        if not device_id.strip():
            raise ValueError("device_id is required")

        def mutator(document: dict[str, object]) -> SessionRecord:
            sessions = self._parse(document)
            for item in sessions:
                if item.device_id == device_id and item.status == SessionStatus.ACTIVE:
                    raise DeviceBusyError(
                        f"device {device_id} is held by session {item.id} ({item.agent_label})"
                    )
            session = SessionRecord(
                id=_new_session_id(),
                device_id=device_id,
                agent_label=agent_label.strip() or "anonymous",
                metadata=metadata or {},
            )
            sessions.append(session)
            document.clear()
            document.update(self._dump(sessions))
            return session

        return self._store.update(mutator)

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

        def mutator(document: dict[str, object]) -> SessionRecord:
            sessions = self._parse(document)
            found: SessionRecord | None = None
            for item in sessions:
                if item.id == session_id:
                    found = item
                    break
            if found is None:
                raise SessionNotFoundError(f"session not found: {session_id}")
            if found.status == SessionStatus.RELEASED:
                return found
            updated = found.model_copy(
                update={
                    "status": SessionStatus.RELEASED,
                    "released_at": when or utcnow(),
                }
            )
            replaced = [item for item in sessions if item.id != session_id]
            replaced.append(updated)
            document.clear()
            document.update(self._dump(replaced))
            return updated

        return self._store.update(mutator)

    def touch(self, session_id: str, when: datetime | None = None) -> SessionRecord:
        """Record that an action ran on this session."""

        def mutator(document: dict[str, object]) -> SessionRecord:
            sessions = self._parse(document)
            found: SessionRecord | None = None
            for item in sessions:
                if item.id == session_id:
                    found = item
                    break
            if found is None:
                raise SessionNotFoundError(f"session not found: {session_id}")
            if found.status != SessionStatus.ACTIVE:
                raise SessionError(f"session {session_id} is not active")
            updated = found.model_copy(update={"last_action_at": when or utcnow()})
            replaced = [item for item in sessions if item.id != session_id]
            replaced.append(updated)
            document.clear()
            document.update(self._dump(replaced))
            return updated

        return self._store.update(mutator)


def _new_session_id() -> str:
    return "ses_" + secrets.token_hex(6)
