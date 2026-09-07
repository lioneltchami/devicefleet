from __future__ import annotations

import threading
from pathlib import Path

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
