from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from devicefleet.store import YamlStore


def test_concurrent_saves_do_not_raise(tmp_path: Path) -> None:
    store = YamlStore(tmp_path / "state.yaml")
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            store.save({"current_sessions": {f"agent-{index}": f"ses_{index}"}})
        except Exception as exc:  # noqa: BLE001 — collect any race error
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    document = store.load()
    assert isinstance(document.get("current_sessions"), dict)


def test_update_merges_under_lock(tmp_path: Path) -> None:
    store = YamlStore(tmp_path / "state.yaml")
    store.save({"current_sessions": {}})

    def set_agent(agent: str) -> None:
        def mutator(document: dict[str, object]) -> None:
            mapping = document.get("current_sessions")
            merged = dict(mapping) if isinstance(mapping, dict) else {}
            merged[agent] = f"ses_{agent}"
            document["current_sessions"] = merged

        store.update(mutator)

    threads = [
        threading.Thread(target=set_agent, args=(f"a{i}",)) for i in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    mapping = store.load()["current_sessions"]
    assert isinstance(mapping, dict)
    assert len(mapping) == 12


def test_save_fsyncs_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = YamlStore(tmp_path / "state.yaml")
    opened: list[tuple[str, int]] = []
    real_open = os.open
    real_fsync = os.fsync

    def tracking_open(path: str | bytes | os.PathLike[str], flags: int, *args: int) -> int:
        opened.append((str(path), flags))
        return real_open(path, flags, *args)

    fsynced = 0

    def tracking_fsync(fd: int) -> None:
        nonlocal fsynced
        fsynced += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "open", tracking_open)  # type: ignore[attr-defined]
    monkeypatch.setattr(os, "fsync", tracking_fsync)  # type: ignore[attr-defined]
    store.save({"ok": True})
    parent = str(tmp_path)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    assert any(
        path == parent and (flags & directory_flag or directory_flag == 0)
        for path, flags in opened
    )
    assert fsynced >= 2
    assert store.load()["ok"] is True
