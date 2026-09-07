from __future__ import annotations

import threading
from pathlib import Path

import pytest

from devicefleet.models import SessionStatus
from devicefleet.sessions import (
    DeviceBusyError,
    SessionManager,
    SessionNotFoundError,
    SessionOwnershipError,
)
from devicefleet.store import YamlStore


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(YamlStore(tmp_path / "sessions.yaml"))


def test_exclusive_lease(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.start("pixel-lab", agent_label="agent-a")
    assert first.status is SessionStatus.ACTIVE
    with pytest.raises(DeviceBusyError):
        manager.start("pixel-lab", agent_label="agent-b")
    manager.stop(first.id)
    second = manager.start("pixel-lab", agent_label="agent-b")
    assert second.id != first.id
    assert manager.get(first.id).status is SessionStatus.RELEASED


def test_attach_and_missing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session = manager.start("stub-demo", agent_label="coder")
    assert session.secret.startswith("cap_")
    attached = manager.attach(
        session.id, agent_label="coder", session_secret=session.secret
    )
    assert attached.id == session.id
    with pytest.raises(SessionOwnershipError):
        manager.attach(session.id, agent_label="coder-2", session_secret=session.secret)
    with pytest.raises(SessionOwnershipError):
        manager.attach(session.id, agent_label="coder")
    with pytest.raises(SessionOwnershipError):
        manager.attach(session.id, agent_label="coder", session_secret="cap_wrong")
    with pytest.raises(SessionOwnershipError):
        manager.attach(session.id, agent_label="coder", session_secret="é")
    with pytest.raises(SessionNotFoundError):
        manager.get("ses_missing")


def test_two_devices_parallel(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    a = manager.start("phone-a")
    b = manager.start("phone-b")
    assert {a.device_id, b.device_id} == {"phone-a", "phone-b"}
    assert len(manager.list_sessions(active_only=True)) == 2


def test_concurrent_start_one_winner(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    winners: list[str] = []
    errors: list[BaseException] = []

    def attempt(label: str) -> None:
        try:
            session = manager.start("shared-phone", agent_label=label)
            winners.append(session.id)
        except DeviceBusyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(errors) == 9
    assert manager.active_for_device("shared-phone") is not None


def test_stop_returns_transitioned_flag(tmp_path: Path) -> None:
    """Regression: stop() must signal real transition vs. idempotent no-op."""
    manager = _manager(tmp_path)
    session = manager.start("phone-1", agent_label="owner")
    first = manager.stop(session.id)
    assert first.transitioned is True
    # Second call is a no-op
    second = manager.stop(session.id)
    assert second.transitioned is False
    assert second.session.id == first.session.id
    # Different device still works after a no-op stop
    third = manager.start("phone-2", agent_label="owner")
    assert third.id != session.id


def test_duplicate_session_ids_are_avoided(tmp_path: Path) -> None:
    """Regression: start() must not reuse an id from an existing session."""
    from devicefleet.sessions import _new_session_id

    manager = _manager(tmp_path)
    # Pre-seed an existing session id and force the next random id to collide
    forced = _new_session_id()
    # Insert a released session with a known id by reaching into the store
    from devicefleet.models import SessionRecord, SessionStatus

    seed = SessionRecord(
        id=forced,
        device_id="old-device",
        agent_label="old",
        status=SessionStatus.RELEASED,
        secret="cap_old",
    )
    store = manager._store
    document = store.load()
    document["sessions"] = [seed.model_dump(mode="json")]
    store.save(document)

    # Monkey-patch _new_session_id to collide once, then return a fresh id
    real = _new_session_id
    calls = {"n": 0}

    def maybe_collide() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return forced
        return real()

    import devicefleet.sessions as s_module

    s_module._new_session_id = maybe_collide
    try:
        session = manager.start("new-device", agent_label="new")
    finally:
        s_module._new_session_id = real
    assert session.id != forced
    # Both sessions must coexist
    assert manager.get(forced).device_id == "old-device"
    assert manager.get(session.id).device_id == "new-device"
