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
    attached = manager.attach(session.id, agent_label="coder")
    assert attached.id == session.id
    with pytest.raises(SessionOwnershipError):
        manager.attach(session.id, agent_label="coder-2")
    with pytest.raises(SessionOwnershipError):
        manager.attach(session.id, agent_label=None)
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
