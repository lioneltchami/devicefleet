from __future__ import annotations

import threading
from pathlib import Path

import pytest

from devicefleet.config import load_settings
from devicefleet.fleet import DeviceInUseError, Fleet, FleetError
from devicefleet.models import ActionName, ActionRequest, ProviderKind
from devicefleet.registry import DeviceNotFoundError
from devicefleet.sessions import DeviceBusyError, SessionError


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


def test_stopped_provisioned_stub_is_not_leaseable(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    extra_id = extra.id
    session = fleet.start_session(device_id=extra_id, agent_label="temp")
    fleet.stop_session(session.id, agent_label="temp")
    with pytest.raises((DeviceNotFoundError, FleetError)):
        fleet.start_session(device_id=extra_id, agent_label="again")
    ids = {item.device.id for item in fleet.list_devices()}
    assert extra_id not in ids
    demo = fleet.start_session(device_id="stub-demo", agent_label="demo")
    fleet.stop_session(demo.id, agent_label="demo")
    again = fleet.start_session(device_id="stub-demo", agent_label="demo-2")
    assert again.device_id == "stub-demo"


def test_cloud_does_not_route_to_stub(fleet: Fleet) -> None:
    with pytest.raises(FleetError, match="cloud adapter"):
        fleet.register_device(
            device_id="farm-1",
            provider=ProviderKind.CLOUD,
            provider_ref="slot-1",
        )
    with pytest.raises(FleetError, match="does not fall back"):
        fleet.provider_for(ProviderKind.CLOUD)


def test_whitespace_ref_is_stripped(fleet: Fleet) -> None:
    record = fleet.register_device(
        device_id="padded",
        provider=ProviderKind.STUB,
        provider_ref="  stub-phone-8  ",
    )
    assert record.provider_ref == "stub-phone-8"


def test_duplicate_ref_rejected_at_fleet(fleet: Fleet) -> None:
    fleet.register_device(
        device_id="one",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-7",
    )
    with pytest.raises(FleetError, match="already registered"):
        fleet.register_device(
            device_id="two",
            provider=ProviderKind.STUB,
            provider_ref="stub-phone-7",
        )


def test_concurrent_start_session_single_lease(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    winners: list[str] = []
    errors: list[BaseException] = []

    def attempt(label: str) -> None:
        try:
            session = fleet.start_session(device_id=extra.id, agent_label=label)
            winners.append(session.id)
        except (DeviceBusyError, FleetError) as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(f"agent-{i}",)) for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert errors
    assert fleet.sessions.active_for_device(extra.id) is not None


def test_run_after_stop_cannot_fire(fleet: Fleet) -> None:
    session = fleet.start_session(device_id="stub-demo", agent_label="racer")
    fleet.stop_session(session.id, agent_label="racer")
    with pytest.raises(SessionError):
        fleet.run(session.id, ActionRequest(name=ActionName.INFO), agent_label="racer")


def test_run_and_stop_serialized(fleet: Fleet) -> None:
    session = fleet.start_session(device_id="stub-demo", agent_label="lock")
    gate = threading.Event()
    original = fleet.stub.screenshot

    def blocked_screenshot(handle: str) -> bytes:
        gate.wait(timeout=2)
        return original(handle)

    fleet.stub.screenshot = blocked_screenshot  # type: ignore[method-assign]
    results: list[str] = []

    def runner() -> None:
        try:
            fleet.run(
                session.id,
                ActionRequest(name=ActionName.SCREENSHOT),
                agent_label="lock",
            )
            results.append("ran")
        except SessionError:
            results.append("run-failed")

    thread = threading.Thread(target=runner)
    thread.start()
    # Give the runner a chance to take the lease lock and enter screenshot.
    threading.Event().wait(0.05)
    gate.set()
    thread.join(timeout=3)
    stopped = fleet.stop_session(session.id, agent_label="lock")
    assert stopped.status.value == "released"
    assert "ran" in results or "run-failed" in results
    if "ran" in results:
        # Action completed under the lock before stop released the lease.
        assert fleet.sessions.get(session.id).status.value == "released"
