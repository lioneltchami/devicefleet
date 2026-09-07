from __future__ import annotations

from pathlib import Path

import pytest

from devicefleet.models import ProviderKind
from devicefleet.registry import DeviceNotFoundError, DeviceRegistry
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
