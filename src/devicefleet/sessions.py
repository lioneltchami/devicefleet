"""Exclusive session leases so agents do not collide on one phone."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
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


@dataclass(frozen=True)
class StopResult:
    """Outcome of `SessionManager.stop`: the session and whether it transitioned.

    Callers that must release a cloud handle should only do so when
    `transitioned` is True; an idempotent re-stop must not release a handle
    that may now belong to a different lease.
    """

    session: SessionRecord
    transitioned: bool


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
        # Normalize device_id so different spacings (e.g. "phone-1" vs
        # " phone-1 ") do not create two concurrent active leases on the
        # same physical device.
        device_id = device_id.strip()
        if not device_id:
            raise ValueError("device_id is required")

        def mutator(document: dict[str, object]) -> SessionRecord:
            sessions = self._parse(document)
            for item in sessions:
                if item.device_id == device_id and item.status == SessionStatus.ACTIVE:
                    raise DeviceBusyError(
                        f"device {device_id} is held by session {item.id} ({item.agent_label})"
                    )
            # Regenerate until the new id does not collide with an existing
            # session; otherwise `stop()` would terminate every record with the
            # matching id, removing a different device's active lease.
            existing_ids = {item.id for item in sessions}
            new_id = _new_session_id()
            while new_id in existing_ids:
                new_id = _new_session_id()
            session = SessionRecord(
                id=new_id,
                device_id=device_id,
                agent_label=agent_label.strip() or "anonymous",
                metadata=metadata or {},
                secret=_new_session_secret(),
            )
            sessions.append(session)
            document.clear()
            document.update(self._dump(sessions))
            return session

        return self._store.update(mutator)

    def attach(
        self,
        session_id: str,
        agent_label: str | None = None,
        session_secret: str | None = None,
    ) -> SessionRecord:
        """Rejoin an existing active session. Requires the capability secret."""
        session = self.require_secret(session_id, session_secret, agent_label)
        return session

    def require_secret(
        self,
        session_id: str,
        session_secret: str | None,
        agent_label: str | None = None,
    ) -> SessionRecord:
        """Return the session only if it is active, the secret matches, and
        an optional agent label matches the owner."""
        session = self.get(session_id)
        if session.status != SessionStatus.ACTIVE:
            raise SessionError(f"session {session_id} is not active")
        self.authorize(session, session_secret, agent_label)
        return session

    def authorize(
        self,
        session: SessionRecord,
        session_secret: str | None,
        agent_label: str | None = None,
    ) -> None:
        """Check the capability secret (and optional owner) on any status."""
        _check_secret(session, session_secret)
        caller = (agent_label or "").strip()
        if caller and caller != session.agent_label:
            raise SessionOwnershipError(
                f"session {session.id} belongs to {session.agent_label}, not {caller}"
            )

    def require_owner(self, session_id: str, agent_label: str | None) -> SessionRecord:
        """Return the session only if it is active and owned by this agent.

        Prefer `require_secret` for HTTP: agent_label is caller-controlled.
        """
        session = self.get(session_id)
        if session.status != SessionStatus.ACTIVE:
            raise SessionError(f"session {session_id} is not active")
        caller = (agent_label or "").strip()
        if not caller or caller != session.agent_label:
            raise SessionOwnershipError(
                f"session {session_id} is not owned by {caller or 'unknown agent'}"
            )
        return session

    def stop(
        self,
        session_id: str,
        when: datetime | None = None,
        release_pending: bool = True,
    ) -> StopResult:
        """Release a session so another agent can take the device.

        Returns the session and a `transitioned` flag; the flag is False on an
        idempotent re-stop so callers do not release resources they did not
        actually acquire this call.

        `release_pending` is set in the same atomic mutator as the RELEASED
        transition, so a follow-up failure on the cloud release can never
        leave a "RELEASED + no marker" state that would skip the next retry.
        """

        def mutator(document: dict[str, object]) -> tuple[SessionRecord, bool]:
            sessions = self._parse(document)
            found: SessionRecord | None = None
            for item in sessions:
                if item.id == session_id:
                    found = item
                    break
            if found is None:
                raise SessionNotFoundError(f"session not found: {session_id}")
            if found.status == SessionStatus.RELEASED:
                return found, False
            metadata = dict(found.metadata)
            if release_pending:
                metadata["release_pending"] = "true"
            else:
                metadata.pop("release_pending", None)
            updated = found.model_copy(
                update={
                    "status": SessionStatus.RELEASED,
                    "released_at": when or utcnow(),
                    "metadata": metadata,
                }
            )
            replaced = [item for item in sessions if item.id != session_id]
            replaced.append(updated)
            document.clear()
            document.update(self._dump(replaced))
            return updated, True

        session, transitioned = self._store.update(mutator)
        return StopResult(session=session, transitioned=transitioned)

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

    def set_release_pending(self, session_id: str, pending: bool) -> SessionRecord:
        """Mark or clear a pending cloud-handle release for a session.

        When `pending` is True, the session is in RELEASED status but the
        hosted handle has not yet been released; subsequent stop_session
        retries should re-attempt the release. When False, the release is
        complete (or was never needed).
        """
        def mutator(document: dict[str, object]) -> SessionRecord:
            sessions = self._parse(document)
            found: SessionRecord | None = None
            for item in sessions:
                if item.id == session_id:
                    found = item
                    break
            if found is None:
                raise SessionNotFoundError(f"session not found: {session_id}")
            metadata = dict(found.metadata)
            if pending:
                metadata["release_pending"] = "true"
            else:
                metadata.pop("release_pending", None)
            updated = found.model_copy(update={"metadata": metadata})
            replaced = [item for item in sessions if item.id != session_id]
            replaced.append(updated)
            document.clear()
            document.update(self._dump(replaced))
            return updated

        return self._store.update(mutator)


def _new_session_id() -> str:
    return "ses_" + secrets.token_hex(6)


def _new_session_secret() -> str:
    return "cap_" + secrets.token_urlsafe(24)


def _check_secret(session: SessionRecord, presented: str | None) -> None:
    expected = session.secret or ""
    got = (presented or "").strip()
    if not expected or not got:
        raise SessionOwnershipError(
            "session secret is required; it is returned at start/attach "
            "and sent as X-Devicefleet-Session"
        )
    try:
        matched = hmac.compare_digest(expected.encode("utf-8"), got.encode("utf-8"))
    except (TypeError, UnicodeError) as exc:
        raise SessionOwnershipError("invalid session secret") from exc
    if not matched:
        raise SessionOwnershipError("invalid session secret")
