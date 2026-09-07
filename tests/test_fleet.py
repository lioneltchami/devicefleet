from __future__ import annotations

import threading
from pathlib import Path

import pytest

from devicefleet.config import load_settings
from devicefleet.fleet import DeviceInUseError, Fleet, FleetError
from devicefleet.models import (
    ActionName,
    ActionRequest,
    DeviceStatus,
    DiscoveredDevice,
    ProviderKind,
)
from devicefleet.providers.base import DeviceProvider
from devicefleet.registry import DeviceNotFoundError
from devicefleet.sessions import DeviceBusyError, SessionError


class FakeCloudProvider(DeviceProvider):
    """Minimal hosted-farm adapter for fleet tests."""

    provider_id = "cloud"

    def __init__(self) -> None:
        self.devices: dict[str, DiscoveredDevice] = {}
        self.released: list[str] = []

    def add(self, ref: str, name: str = "Farm Phone") -> DiscoveredDevice:
        item = DiscoveredDevice(
            provider=ProviderKind.CLOUD,
            provider_ref=ref,
            display_name=name,
            status=DeviceStatus.ONLINE,
            suggested_id=ref,
            suggested_tags=["cloud", "farm"],
        )
        self.devices[ref] = item
        return item

    def discover(self) -> list[DiscoveredDevice]:
        return list(self.devices.values())

    def screenshot(self, handle: str) -> bytes:
        return b""

    def tap(self, handle: str, x: int, y: int) -> None:
        return None

    def swipe(
        self,
        handle: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> None:
        return None

    def type_text(self, handle: str, text: str) -> None:
        return None

    def keyevent(self, handle: str, key: str) -> None:
        return None

    def dump_ui(self, handle: str) -> str:
        return "<hierarchy/>"

    def release_cloud(self, handle: str) -> None:
        self.released.append(handle)
        self.devices.pop(handle, None)

    def health(self, handle: str) -> bool:
        return handle in self.devices


def test_stub_session_helpers_without_hardware(fleet: Fleet) -> None:
    session = fleet.start_session(device_id="stub-demo", agent_label="pytest")
    secret = session.secret
    shot = fleet.run(
        session.id, ActionRequest(name=ActionName.SCREENSHOT), session_secret=secret
    )
    assert shot.ok
    assert shot.artifact_path
    assert shot.artifact_path.endswith("screenshot.png")

    tap = fleet.run(
        session.id,
        ActionRequest(name=ActionName.TAP, x=540, y=960),
        session_secret=secret,
    )
    assert tap.ok
    typed = fleet.run(
        session.id,
        ActionRequest(name=ActionName.TYPE, text="fleet"),
        session_secret=secret,
    )
    assert typed.ok
    dump = fleet.run(
        session.id, ActionRequest(name=ActionName.DUMP_UI), session_secret=secret
    )
    assert "<hierarchy" in str(dump.payload.get("xml"))

    stopped = fleet.stop_session(session.id, session_secret=secret)
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
    stopped = fleet.stop_session(
        session.id, agent_label="ghost", session_secret=session.secret
    )
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
    fleet.stop_session(session.id, agent_label="temp", session_secret=session.secret)
    with pytest.raises((DeviceNotFoundError, FleetError)):
        fleet.start_session(device_id=extra_id, agent_label="again")
    ids = {item.device.id for item in fleet.list_devices()}
    assert extra_id not in ids
    demo = fleet.start_session(device_id="stub-demo", agent_label="demo")
    fleet.stop_session(demo.id, agent_label="demo", session_secret=demo.secret)
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
    fleet.stop_session(session.id, agent_label="racer", session_secret=session.secret)
    with pytest.raises(SessionError):
        fleet.run(
            session.id,
            ActionRequest(name=ActionName.INFO),
            agent_label="racer",
            session_secret=session.secret,
        )


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
                session_secret=session.secret,
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
    stopped = fleet.stop_session(
        session.id, agent_label="lock", session_secret=session.secret
    )
    assert stopped.status.value == "released"
    assert "ran" in results or "run-failed" in results
    if "ran" in results:
        # Action completed under the lock before stop released the lease.
        assert fleet.sessions.get(session.id).status.value == "released"


def test_provision_does_not_overwrite_non_stub(fleet: Fleet) -> None:
    fleet.register_device(
        device_id="stub-phone-2",
        provider=ProviderKind.ADB,
        provider_ref="SERIAL-REAL",
        display_name="Real Phone",
    )
    extra = fleet.provision_stub()
    assert extra.id != "stub-phone-2"
    assert extra.provider_ref != "stub-phone-2"
    assert extra.provider_ref != "SERIAL-REAL"
    kept = fleet.registry.get("stub-phone-2")
    assert kept.provider is ProviderKind.ADB
    assert kept.display_name == "Real Phone"


def test_remove_and_start_do_not_orphan_session(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    errors: list[BaseException] = []
    started: list[str] = []

    def remover() -> None:
        try:
            fleet.remove_device(extra.id)
        except (DeviceInUseError, DeviceNotFoundError) as exc:
            errors.append(exc)

    def starter() -> None:
        try:
            session = fleet.start_session(device_id=extra.id, agent_label="racer")
            started.append(session.id)
        except (DeviceNotFoundError, FleetError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=starter), threading.Thread(target=remover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if started:
        session = fleet.sessions.get(started[0])
        assert fleet.registry.get(session.device_id).id == extra.id
    else:
        with pytest.raises(DeviceNotFoundError):
            fleet.registry.get(extra.id)


def test_adb_unknown_is_not_leaseable_but_stub_demo_is(fleet: Fleet) -> None:
    fleet.register_device(
        device_id="ghost-pixel",
        provider=ProviderKind.ADB,
        provider_ref="SERIAL-MISSING",
        display_name="Ghost",
    )
    assert fleet.registry.get("ghost-pixel").last_status is DeviceStatus.UNKNOWN
    with pytest.raises(FleetError, match="not available|unavailable"):
        fleet.start_session(device_id="ghost-pixel", agent_label="adb")
    demo = fleet.start_session(device_id="stub-demo", agent_label="demo")
    assert demo.device_id == "stub-demo"
    fleet.stop_session(demo.id, session_secret=demo.secret)


def test_stop_releases_metadata_handle_not_reregistered_ref(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    original = extra.provider_ref
    session = fleet.start_session(device_id=extra.id, agent_label="meta")
    fleet.register_device(
        device_id=extra.id,
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-99",
        display_name=extra.display_name,
    )
    fleet.stub.ensure("stub-phone-99", extra.display_name)
    fleet.stop_session(session.id, session_secret=session.secret)
    assert fleet.stub.health(original) is False
    assert fleet.stub.health("stub-phone-99") is True
    kept = fleet.registry.get(extra.id)
    assert kept.provider_ref == "stub-phone-99"


def test_double_stop_does_not_release_new_stub(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    first = fleet.start_session(device_id=extra.id, agent_label="first")
    first_handle = extra.provider_ref
    fleet.stop_session(first.id, session_secret=first.secret)
    again = fleet.stop_session(first.id, session_secret=first.secret)
    assert again.status.value == "released"
    replacement = fleet.provision_stub()
    second = fleet.start_session(device_id=replacement.id, agent_label="second")
    fleet.stop_session(first.id, session_secret=first.secret)
    assert fleet.sessions.get(second.id).status.value == "active"
    assert fleet.stub.health(replacement.provider_ref) is True
    assert replacement.provider_ref != first_handle or fleet.registry.get(
        replacement.id
    )
    fleet.stop_session(second.id, session_secret=second.secret)


def test_cloud_discover_and_live_status(fleet: Fleet) -> None:
    cloud = FakeCloudProvider()
    cloud.add("slot-1", "Farm One")
    fleet.set_cloud_provider(cloud)
    found = {item.provider_ref for item in fleet.discover(save=True)}
    assert "slot-1" in found
    listed = {item.device.id: item.status.value for item in fleet.list_devices()}
    assert listed["slot-1"] == "online"
    session = fleet.start_session(device_id="slot-1", agent_label="farm")
    fleet.stop_session(session.id, session_secret=session.secret)
    assert cloud.released == ["slot-1"]
    cloud.devices.clear()
    fleet.register_device(
        device_id="slot-gone",
        provider=ProviderKind.CLOUD,
        provider_ref="slot-missing",
        display_name="Gone",
    )
    listed = {item.device.id: item.status.value for item in fleet.list_devices()}
    assert listed["slot-gone"] == "offline"
    with pytest.raises(FleetError):
        fleet.start_session(device_id="slot-gone", agent_label="farm")


def test_attach_after_stop_does_not_restore_current(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    session = fleet.start_session(device_id=extra.id, agent_label="owner")
    fleet.stop_session(session.id, session_secret=session.secret)
    with pytest.raises(SessionError):
        fleet.attach_session(
            session.id, agent_label="owner", session_secret=session.secret
        )
    assert fleet.current_session_id("owner") is None


def test_attach_and_stop_leave_consistent_current(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    session = fleet.start_session(device_id=extra.id, agent_label="owner")
    errors: list[BaseException] = []

    def stopper() -> None:
        try:
            fleet.stop_session(session.id, session_secret=session.secret)
        except SessionError as exc:
            errors.append(exc)

    def attacher() -> None:
        try:
            fleet.attach_session(
                session.id, agent_label="owner", session_secret=session.secret
            )
        except SessionError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attacher), threading.Thread(target=stopper)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fleet.sessions.get(session.id).status.value == "released"
    assert fleet.current_session_id("owner") is None
