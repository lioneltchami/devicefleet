from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devicefleet.models import DeviceRecord, DeviceStatus, ProviderKind
from devicefleet.registry import DeviceNotFoundError, DeviceRegistry, DuplicateDeviceError
from devicefleet.store import YamlStore


def _registry(tmp_path: Path) -> DeviceRegistry:
    return DeviceRegistry(YamlStore(tmp_path / "devices.yaml"))


def test_register_and_get(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register(
        device_id="pixel-lab",
        provider=ProviderKind.ADB,
        provider_ref="R58TEST",
        display_name="Pixel",
        tags=["Lab", "android", "lab"],
    )
    assert record.id == "pixel-lab"
    assert record.tags == ["lab", "android"]
    loaded = registry.get("pixel-lab")
    assert loaded.provider_ref == "R58TEST"


def test_persist_reload(tmp_path: Path) -> None:
    first = _registry(tmp_path)
    first.register(
        device_id="emu-1",
        provider=ProviderKind.ADB,
        provider_ref="emulator-5554",
        tags=["emu"],
    )
    second = _registry(tmp_path)
    assert second.get("emu-1").provider_ref == "emulator-5554"


def test_find_by_tags(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register("a", ProviderKind.STUB, "s1", tags=["demo", "android"])
    registry.register("b", ProviderKind.ADB, "s2", tags=["lab", "android"])
    registry.register("c", ProviderKind.ADB, "s3", tags=["lab", "tablet"])
    matches = registry.find(tags=["lab", "android"])
    assert [item.id for item in matches] == ["b"]


def test_remove_missing(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(DeviceNotFoundError):
        registry.remove("nope")


def test_rejects_whitespace_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ValueError):
        registry.register("bad id", ProviderKind.STUB, "x")


def test_rejects_duplicate_provider_ref(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register("phone-a", ProviderKind.ADB, "  SERIAL-1  ")
    assert registry.get("phone-a").provider_ref == "SERIAL-1"
    with pytest.raises(DuplicateDeviceError, match="already registered as phone-a"):
        registry.register("phone-b", ProviderKind.ADB, "SERIAL-1")
    registry.register("phone-a", ProviderKind.ADB, "SERIAL-1", tags=["lab"])
    assert registry.get("phone-a").tags == ["lab"]


def test_reregister_none_keeps_empty_clears(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        "phone-a",
        ProviderKind.STUB,
        "h1",
        tags=["lab", "android"],
        metadata={"rack": "1"},
        notes="keep me",
    )
    kept = registry.register("phone-a", ProviderKind.STUB, "h1")
    assert kept.tags == ["lab", "android"]
    assert kept.metadata == {"rack": "1"}
    assert kept.notes == "keep me"

    cleared = registry.register(
        "phone-a",
        ProviderKind.STUB,
        "h1",
        tags=[],
        metadata={},
        notes="",
    )
    assert cleared.tags == []
    assert cleared.metadata == {}
    assert cleared.notes == ""


def test_naive_registered_at_becomes_utc() -> None:
    record = DeviceRecord(
        id="naive-1",
        display_name="Naive",
        provider=ProviderKind.STUB,
        provider_ref="h1",
        registered_at=datetime(2024, 1, 15, 12, 0, 0),
    )
    assert record.registered_at.tzinfo is not None
    assert record.registered_at.utcoffset() == timezone.utc.utcoffset(record.registered_at)
    later = DeviceRecord(
        id="naive-2",
        display_name="Later",
        provider=ProviderKind.STUB,
        provider_ref="h2",
        registered_at=datetime(2024, 1, 16, 12, 0, 0),
    )
    assert [record.id, later.id] == [
        item.id for item in sorted([later, record], key=lambda item: item.registered_at)
    ]


def test_reregister_keeps_display_name(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        "phone-a",
        ProviderKind.STUB,
        "h1",
        display_name="Pretty Name",
    )
    kept = registry.register("phone-a", ProviderKind.STUB, "h1")
    assert kept.display_name == "Pretty Name"
    renamed = registry.register(
        "phone-a", ProviderKind.STUB, "h1", display_name="New Name"
    )
    assert renamed.display_name == "New Name"


def test_register_strips_id_before_lookup(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register("phone", ProviderKind.STUB, "h1", display_name="Original")
    updated = registry.register(" phone ", ProviderKind.STUB, "h1", tags=["lab"])
    assert updated.id == "phone"
    assert updated.display_name == "Original"
    assert updated.tags == ["lab"]
    assert len(registry.list_devices()) == 1


def test_concurrent_upsert_keeps_all_devices(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    def writer(index: int) -> None:
        registry.upsert(
            DeviceRecord(
                id=f"dev-{index}",
                display_name=f"Dev {index}",
                provider=ProviderKind.STUB,
                provider_ref=f"h{index}",
            )
        )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = {item.id for item in registry.list_devices()}
    assert ids == {f"dev-{i}" for i in range(16)}


def test_get_by_ref(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register("custom", ProviderKind.STUB, "stub-phone-8")
    found = registry.get_by_ref(ProviderKind.STUB, "stub-phone-8")
    assert found is not None
    assert found.id == "custom"
    assert registry.get_by_ref(ProviderKind.ADB, "stub-phone-8") is None


def test_set_status_does_not_clobber_concurrent_tags(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register("phone", ProviderKind.STUB, "h1", tags=["keep"])

    def status_loop() -> None:
        for _ in range(80):
            registry.set_status("phone", DeviceStatus.ONLINE)
            registry.set_status("phone", DeviceStatus.OFFLINE)

    def tag_writer() -> None:
        for index in range(80):
            registry.register("phone", ProviderKind.STUB, "h1", tags=[f"t{index}"])

    threads = [threading.Thread(target=status_loop), threading.Thread(target=tag_writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    loaded = registry.get("phone")
    assert loaded.tags
    assert loaded.tags[0] == "keep" or loaded.tags[0].startswith("t")
    assert loaded.provider_ref == "h1"


def test_ensure_stub_demo_does_not_overwrite_existing(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        "stub-demo",
        ProviderKind.ADB,
        "SERIAL-1",
        display_name="Not Stub",
    )
    kept = registry.ensure_stub_demo()
    assert kept.provider is ProviderKind.ADB
    assert kept.provider_ref == "SERIAL-1"
    assert kept.display_name == "Not Stub"
