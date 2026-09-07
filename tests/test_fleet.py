from __future__ import annotations

from pathlib import Path

import pytest

from devicefleet.config import load_settings
from devicefleet.fleet import DeviceInUseError, Fleet
from devicefleet.models import ActionName, ActionRequest, ProviderKind


def test_stub_session_helpers_without_hardware(fleet: Fleet) -> None:
    session = fleet.start_session(device_id="stub-demo", agent_label="pytest")
    shot = fleet.run(session.id, ActionRequest(name=ActionName.SCREENSHOT))
    assert shot.ok
    assert shot.artifact_path
    assert shot.artifact_path.endswith("screenshot.png")

    tap = fleet.run(session.id, ActionRequest(name=ActionName.TAP, x=540, y=960))
    assert tap.ok
    typed = fleet.run(session.id, ActionRequest(name=ActionName.TYPE, text="fleet"))
    assert typed.ok
    dump = fleet.run(session.id, ActionRequest(name=ActionName.DUMP_UI))
    assert "<hierarchy" in str(dump.payload.get("xml"))

    stopped = fleet.stop_session(session.id)
    assert stopped.status.value == "released"


def test_busy_device_blocks_second_session(fleet: Fleet) -> None:
    fleet.start_session(device_id="stub-demo", agent_label="one")
    extra = fleet.provision_stub()
    other = fleet.start_session(device_id=extra.id, agent_label="two")
    assert other.device_id != "stub-demo"
    listed = fleet.list_devices()
    busy = {item.device.id: item.status.value for item in listed}
    assert busy["stub-demo"] == "busy"


def test_current_session_is_per_agent(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    alice = fleet.start_session(device_id="stub-demo", agent_label="alice")
    bob = fleet.start_session(device_id=extra.id, agent_label="bob")
    assert fleet.current_session_id("alice") == alice.id
    assert fleet.current_session_id("bob") == bob.id
    assert fleet.current_session_id("alice") != fleet.current_session_id("bob")


def test_remove_busy_device_is_refused(fleet: Fleet) -> None:
    fleet.start_session(device_id="stub-demo", agent_label="keeper")
    with pytest.raises(DeviceInUseError):
        fleet.remove_device("stub-demo")


def test_unattached_adb_device_lists_offline(fleet: Fleet) -> None:
    fleet.register_device(
        device_id="ghost-pixel",
        provider=ProviderKind.ADB,
        provider_ref="SERIAL-MISSING",
        display_name="Ghost",
    )
    listed = {item.device.id: item.status.value for item in fleet.list_devices()}
    assert listed["ghost-pixel"] == "offline"
    assert listed["stub-demo"] == "online"


def test_stop_releases_when_registry_row_is_gone(fleet: Fleet) -> None:
    session = fleet.start_session(device_id="stub-demo", agent_label="ghost")
    fleet.registry.remove("stub-demo")
    stopped = fleet.stop_session(session.id, agent_label="ghost")
    assert stopped.status.value == "released"


def test_stub_registry_rehydrates_and_provision_avoids_collision(
    fleet_home: Path,
) -> None:
    first = Fleet(load_settings(fleet_home))
    first.register_device(
        device_id="stub-custom",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-9",
        display_name="Custom Stub",
    )
    second = Fleet(load_settings(fleet_home))
    assert second.stub.health("stub-phone-9") is True
    extra = second.provision_stub()
    assert extra.provider_ref != "stub-phone-9"
    assert extra.provider_ref != "stub-phone-1"
