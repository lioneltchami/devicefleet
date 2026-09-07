from __future__ import annotations

from pathlib import Path

import pytest

from datetime import datetime, timezone

from devicefleet.models import DeviceRecord, ProviderKind
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
