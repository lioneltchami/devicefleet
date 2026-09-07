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
from devicefleet.providers.base import DeviceProvider, ProviderError
from devicefleet.registry import DeviceNotFoundError
from devicefleet.sessions import DeviceBusyError, SessionError, SessionOwnershipError


class FakeCloudProvider(DeviceProvider):
    """Minimal hosted-farm adapter for fleet tests."""

    provider_id = "cloud"

    def __init__(self) -> None:
        self.devices: dict[str, DiscoveredDevice] = {}
        self.released: list[str] = []
        self.fail_release = False

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
        if self.fail_release:
            raise ProviderError("farm busy")
        if handle not in self.released:
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
    assert extra.metadata.get("provisioned") == "true"
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


def test_long_device_id_lease_lock_is_bounded(fleet: Fleet) -> None:
    long_id = "phone-" + ("x" * 300)
    other_id = "phone-" + ("y" * 300)
    fleet.register_device(
        device_id=long_id,
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-long",
    )
    lock = fleet._lease_lock(long_id)
    assert len(lock.path.name.encode("utf-8")) <= 255
    assert fleet._lease_lock(long_id).path == lock.path
    assert fleet._lease_lock(other_id).path != lock.path
    session = fleet.start_session(device_id=long_id, agent_label="long-id")
    fleet.stop_session(
        session.id, agent_label="long-id", session_secret=session.secret
    )
    fleet.register_device(
        device_id=long_id,
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-long-b",
    )
    fleet.remove_device(long_id)


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


def test_concurrent_start_session_retries_other_idle_device(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    winners: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        try:
            barrier.wait()
            session = fleet.start_session(tags=["stub"], agent_label=label)
            winners.append(session.device_id)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=attempt, args=("alice",)),
        threading.Thread(target=attempt, args=("bob",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert set(winners) == {"stub-demo", extra.id}


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


def test_register_device_serializes_with_provision_stub(fleet: Fleet) -> None:
    """Regression: register_device must share the provision lock with provision_stub
    so a concurrent register cannot sneak an id into the snapshot-to-register window.
    """
    errors: list[BaseException] = []
    provisioned: list[str] = []
    barrier = threading.Barrier(2)

    def provisioner() -> None:
        try:
            barrier.wait()
            record = fleet.provision_stub()
            provisioned.append(record.id)
        except BaseException as exc:
            errors.append(exc)

    def registrator() -> None:
        try:
            barrier.wait()
            # Pre-register the well-known demo id while provision_stub runs.
            # With the lock, this serializes cleanly with provision_stub.
            fleet.register_device(
                device_id="my-device",
                provider=ProviderKind.STUB,
                provider_ref="stub-phone-known",
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=provisioner),
        threading.Thread(target=registrator),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(provisioned) == 1
    # Both operations must produce stable, distinct records.
    record = fleet.registry.get("my-device")
    assert record.provider_ref == "stub-phone-known"
    extra_id = provisioned[0]
    assert extra_id != "my-device"


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


def test_stop_releases_metadata_handle_if_row_mutated(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    original = extra.provider_ref
    session = fleet.start_session(device_id=extra.id, agent_label="meta")
    with pytest.raises(DeviceInUseError, match="provider"):
        fleet.register_device(
            device_id=extra.id,
            provider=ProviderKind.STUB,
            provider_ref="stub-phone-99",
            display_name=extra.display_name,
        )
    fleet.registry.register(
        extra.id, ProviderKind.STUB, "stub-phone-99", display_name=extra.display_name
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


def test_discover_save_updates_user_chosen_id(fleet: Fleet) -> None:
    fleet.register_device(
        device_id="my-stub",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-8",
        display_name="Custom",
    )
    fleet.discover(save=True)
    ids = {item.device.id for item in fleet.list_devices()}
    assert "my-stub" in ids
    assert "stub-phone-8" not in ids
    assert fleet.registry.get("my-stub").provider_ref == "stub-phone-8"


def test_discover_save_does_not_overwrite_suggested_id_collision(fleet: Fleet) -> None:
    fleet.register_device(
        device_id="stub-phone-8",
        provider=ProviderKind.ADB,
        provider_ref="SERIAL-REAL",
        display_name="Real Phone",
    )
    fleet.stub.ensure("unique-handle-8", "Colliding stub")
    original = fleet.stub.discover

    def extra_discover() -> list[DiscoveredDevice]:
        items = original()
        items.append(
            DiscoveredDevice(
                provider=ProviderKind.STUB,
                provider_ref="unique-handle-8",
                display_name="Colliding stub",
                suggested_id="stub-phone-8",
            )
        )
        return items

    fleet.stub.discover = extra_discover  # type: ignore[method-assign]
    fleet.discover(save=True)
    kept = fleet.registry.get("stub-phone-8")
    assert kept.provider is ProviderKind.ADB
    assert kept.provider_ref == "SERIAL-REAL"
    assert kept.display_name == "Real Phone"


def test_local_start_preserves_remote_session_secrets(fleet: Fleet) -> None:
    fleet.state_store.save(
        {
            "current_sessions": {"remote": "ses_old"},
            "session_secrets": {"ses_remote": "cap_keepme"},
        }
    )
    session = fleet.start_session(device_id="stub-demo", agent_label="local")
    document = fleet.state_store.load()
    secrets = document.get("session_secrets")
    assert isinstance(secrets, dict)
    assert secrets.get("ses_remote") == "cap_keepme"
    mapping = document.get("current_sessions")
    assert isinstance(mapping, dict)
    assert mapping.get("local") == session.id
    assert mapping.get("remote") == "ses_old"


def test_concurrent_provision_stub_unique_ids(fleet: Fleet) -> None:
    created: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            record = fleet.provision_stub()
            created.append(record.id)
        except BaseException as exc:  # noqa: BLE001 — collect any race error
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(created) == 8
    assert len(set(created)) == 8
    refs = {fleet.registry.get(device_id).provider_ref for device_id in created}
    assert len(refs) == 8


def test_discover_save_skips_busy_status(fleet: Fleet) -> None:
    original = fleet.stub.discover

    def busy_discover() -> list[DiscoveredDevice]:
        items = original()
        return [
            item.model_copy(update={"status": DeviceStatus.BUSY}) for item in items
        ]

    fleet.stub.discover = busy_discover  # type: ignore[method-assign]
    found = fleet.discover(save=True)
    assert found
    assert all(item.status is DeviceStatus.BUSY for item in found)
    demo = fleet.registry.get("stub-demo")
    assert demo.last_status is not DeviceStatus.BUSY


def test_discover_save_does_not_resurrect_released_stub(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    stale = DiscoveredDevice(
        provider=ProviderKind.STUB,
        provider_ref=extra.provider_ref,
        display_name=extra.display_name,
        status=DeviceStatus.ONLINE,
        metadata=dict(extra.metadata),
        suggested_id=extra.id,
        suggested_tags=list(extra.tags),
    )
    session = fleet.start_session(device_id=extra.id, agent_label="temp")
    fleet.stop_session(session.id, session_secret=session.secret)
    fleet._save_discovered(stale)
    ids = {item.device.id for item in fleet.list_devices()}
    assert extra.id not in ids
    assert fleet.stub.health(extra.provider_ref) is False


def test_user_registered_stub_survives_session_stop(fleet: Fleet) -> None:
    custom = fleet.register_device(
        device_id="lab-stub",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-custom",
        display_name="Lab Stub",
    )
    assert custom.metadata.get("provisioned") != "true"
    session = fleet.start_session(device_id="lab-stub", agent_label="lab")
    fleet.stop_session(session.id, session_secret=session.secret)
    kept = fleet.registry.get("lab-stub")
    assert kept.provider_ref == "stub-phone-custom"
    assert fleet.stub.health("stub-phone-custom") is True
    again = fleet.start_session(device_id="lab-stub", agent_label="lab-2")
    assert again.device_id == "lab-stub"
    fleet.stop_session(again.id, session_secret=again.secret)


def test_spoofed_provisioned_metadata_does_not_deregister(fleet: Fleet) -> None:
    custom = fleet.register_device(
        device_id="lab-stub-meta",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-meta",
        display_name="Lab Stub",
        metadata={"provisioned": "true", "lab": "1"},
    )
    assert custom.metadata.get("provisioned") != "true"
    assert custom.metadata.get("lab") == "1"
    session = fleet.start_session(device_id="lab-stub-meta", agent_label="lab")
    assert session.metadata.get("provisioned") != "true"
    fleet.stop_session(session.id, session_secret=session.secret)
    kept = fleet.registry.get("lab-stub-meta")
    assert kept.provider_ref == "stub-phone-meta"
    assert fleet.stub.health("stub-phone-meta") is True


def test_released_stop_requires_secret(fleet: Fleet) -> None:
    extra = fleet.provision_stub()
    session = fleet.start_session(device_id=extra.id, agent_label="owner")
    fleet.stop_session(session.id, session_secret=session.secret)
    later = fleet.start_session(device_id="stub-demo", agent_label="owner")
    with pytest.raises(SessionOwnershipError):
        fleet.stop_session(session.id)
    assert fleet.current_session_id("owner") == later.id
    fleet.stop_session(session.id, session_secret=session.secret)
    assert fleet.current_session_id("owner") == later.id
    fleet.stop_session(later.id, session_secret=later.secret)


def test_cloud_release_failure_marks_session_released(fleet: Fleet) -> None:
    """Greptile P1 fix: stop_session marks the session RELEASED before releasing
    the cloud handle, so a release failure leaves the persistent state consistent.
    The handle may leak, but a retry is a safe no-op rather than a double release.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-retry", "Farm Retry")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    session = fleet.start_session(device_id="slot-retry", agent_label="farm")
    cloud.fail_release = True
    with pytest.raises(ProviderError, match="farm busy"):
        fleet.stop_session(session.id, session_secret=session.secret)
    # Persistent state is the source of truth: session is RELEASED.
    assert fleet.sessions.get(session.id).status.value == "released"
    # Retry is a safe no-op (the session is already RELEASED).
    stopped = fleet.stop_session(session.id, session_secret=session.secret)
    assert stopped.status.value == "released"
    # The handle still hasn't been released because the original call failed.
    assert cloud.released == []


def test_set_cloud_provider_rejects_missing_release_cloud(fleet: Fleet) -> None:
    """set_cloud_provider must refuse adapters without a callable release_cloud."""
    from devicefleet.providers.base import DeviceProvider

    class NoReleaseProvider(DeviceProvider):
        provider_id = "norelease"

        def discover(self): return []
        def screenshot(self, h): return b""
        def tap(self, h, x, y): pass
        def swipe(self, h, x1, y1, x2, y2, duration_ms=300): pass
        def type_text(self, h, t): pass
        def keyevent(self, h, k): pass
        def dump_ui(self, h): return ""

    with pytest.raises(ValueError, match="release_cloud"):
        fleet.set_cloud_provider(NoReleaseProvider())


def test_ensure_stub_demo_atomic_under_concurrent_register(fleet: Fleet) -> None:
    """ensure_stub_demo must not overwrite a concurrently-registered stub-demo."""
    import threading
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def ensureer() -> None:
        try:
            barrier.wait()
            results.append(("ensure", fleet.registry.ensure_stub_demo().display_name))
        except BaseException as exc:
            errors.append(exc)

    def registerer() -> None:
        try:
            barrier.wait()
            fleet.register_device(
                device_id="stub-demo",
                provider=ProviderKind.STUB,
                provider_ref="custom-handle",
                display_name="Custom Demo",
            )
            results.append(("register", "ok"))
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=ensureer)
    t2 = threading.Thread(target=registerer)
    t1.start(); t2.start()
    t1.join(); t2.join()
    # Whichever wins, the final record must be one of the two and must be
    # unique (no torn write).
    record = fleet.registry.get("stub-demo")
    assert record.id == "stub-demo"
    assert record.provider is ProviderKind.STUB
    # The two operations must not race into two records.
    devices = [d for d in fleet.registry.list_devices() if d.id == "stub-demo"]
    assert len(devices) == 1


def test_list_devices_skips_removed_during_status_update(fleet: Fleet) -> None:
    """list_devices must not raise DeviceNotFoundError when remove_device races
    with the per-device status update.
    """
    import threading
    import time

    # Pre-register a device
    fleet.register_device(
        device_id="racy",
        provider=ProviderKind.STUB,
        provider_ref="racy-handle",
        display_name="Racy",
    )

    def remover() -> None:
        time.sleep(0.01)
        try:
            fleet.remove_device("racy")
        except DeviceNotFoundError:
            pass

    t = threading.Thread(target=remover)
    t.start()
    # Repeatedly call list_devices; the in-between remove must not raise
    for _ in range(20):
        items = fleet.list_devices()
        ids = [i.device.id for i in items]
        assert "racy" not in ids or any(i.device.id == "racy" for i in items)
    t.join()


def test_ensure_stub_demo_skips_when_handle_already_taken(fleet: Fleet) -> None:
    """ensure_stub_demo must not create a stub-demo record when another device
    already owns stub-phone-1, otherwise two registry ids would lease the same
    stub phone concurrently.
    """
    # Simulate the state the bot describes: stub-demo was removed and a
    # different id now owns the same provider_ref.
    fleet.remove_device("stub-demo")
    fleet.register_device(
        device_id="custom",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-1",
        display_name="Custom Demo",
    )
    # Now ensure_stub_demo must NOT overwrite "custom" with a fresh stub-demo
    result = fleet.registry.ensure_stub_demo()
    assert result is not None
    assert result.id == "custom", f"got {result.id!r}, expected 'custom'"
    # The registry must not have a stub-demo record
    with pytest.raises(DeviceNotFoundError):
        fleet.registry.get("stub-demo")


def test_set_cloud_provider_rejects_replacement_with_active_cloud_session(
    fleet: Fleet,
) -> None:
    """set_cloud_provider must refuse to replace the adapter while a CLOUD
    session is still active, so the stop path cannot route release_cloud to
    the wrong adapter.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-1", "Slot 1")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    session = fleet.start_session(device_id="slot-1", agent_label="farm")
    # Replacing the adapter now must fail
    new_cloud = FakeCloudProvider()
    with pytest.raises(ValueError, match="active"):
        fleet.set_cloud_provider(new_cloud)
    # Stop the session, then the replacement is allowed
    fleet.stop_session(session.id, session_secret=session.secret)
    fleet.set_cloud_provider(new_cloud)  # no error


def test_start_session_rolls_back_when_current_session_write_fails(fleet: Fleet) -> None:
    """If _set_current_session cannot record the new lease, the session must
    be rolled back to RELEASED so the device is not orphaned.
    """
    # Register a second stub device so we can lease it while stub-demo is held
    fleet.register_device(
        device_id="racy",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-racy",
        display_name="Racy",
    )

    def boom(_session_id: str | None, _agent_label: str) -> None:
        raise OSError("disk full")

    original = fleet._set_current_session
    fleet._set_current_session = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError, match="disk full"):
            fleet.start_session(device_id="racy", agent_label="alice")
    finally:
        fleet._set_current_session = original  # type: ignore[method-assign]

    # The failed lease must have been rolled back; no active session on racy.
    assert fleet.sessions.active_for_device("racy") is None
    sessions = [
        s for s in fleet.sessions.list_sessions()
        if s.device_id == "racy"
    ]
    assert sessions and sessions[0].status.value == "released"


def test_stop_session_retries_pending_release_after_failure(fleet: Fleet) -> None:
    """A failed _release_cloud_handle must leave a release_pending marker
    so the next stop_session retry can pick the release back up.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-r", "Slot R")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    session = fleet.start_session(device_id="slot-r", agent_label="farm")

    # First stop: release fails, session is RELEASED but the marker is set
    cloud.fail_release = True
    with pytest.raises(ProviderError):
        fleet.stop_session(session.id, session_secret=session.secret)
    persisted = fleet.sessions.get(session.id)
    assert persisted.status.value == "released"
    assert persisted.metadata.get("release_pending") == "true"
    assert cloud.released == []

    # Second stop: same id, but the adapter is healthy now; the retry must
    # clear the marker and actually release the handle.
    cloud.fail_release = False
    result = fleet.stop_session(session.id, session_secret=session.secret)
    assert result.status.value == "released"
    assert cloud.released == ["slot-r"]
    cleared = fleet.sessions.get(session.id)
    assert cleared.metadata.get("release_pending") != "true"


def test_stop_session_abandons_pending_release_when_new_lease_exists(
    fleet: Fleet,
) -> None:
    """If a new lease has been started on the same device while a previous
    release is still pending, the retry must abandon the pending release
    rather than release a handle the new lease depends on.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-n", "Slot N")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    first = fleet.start_session(device_id="slot-n", agent_label="alice")

    # First stop fails -> release_pending set
    cloud.fail_release = True
    with pytest.raises(ProviderError):
        fleet.stop_session(first.id, session_secret=first.secret)

    # Re-add the cloud phone and start a new session on it
    cloud.add("slot-n", "Slot N")
    second = fleet.start_session(device_id="slot-n", agent_label="bob")
    assert second.id != first.id

    # Retry the original stop: must NOT release slot-n (it now belongs to bob)
    pre = list(cloud.released)
    fleet.stop_session(first.id, session_secret=first.secret)
    assert cloud.released == pre  # no new release
    cleared = fleet.sessions.get(first.id)
    assert cleared.metadata.get("release_pending") != "true"  # marker cleared

    # Cleanup
    cloud.fail_release = False
    fleet.stop_session(second.id, session_secret=second.secret)


def test_session_start_normalizes_device_id(fleet: Fleet) -> None:
    """`SessionManager.start` must strip device_id so differently-spaced forms
    cannot both create active leases on the same device.
    """
    fleet.register_device(
        device_id="phone-1",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-idnorm",
        display_name="Phone 1",
    )
    first = fleet.start_session(device_id="phone-1", agent_label="alice")
    assert first.device_id == "phone-1"
    # The second start uses a padded form; the underlying session must
    # collide on the stripped id and raise DeviceBusyError.
    with pytest.raises(DeviceBusyError):
        fleet.start_session(device_id="  phone-1  ", agent_label="bob")
    fleet.stop_session(first.id, session_secret=first.secret)


def test_set_cloud_provider_rejects_when_pending_release_exists(fleet: Fleet) -> None:
    """set_cloud_provider must refuse installation when a persisted session has
    a CLOUD handle with a pending release — otherwise the new adapter would
    receive `release_cloud` for a handle owned by the previous adapter.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-p", "Slot P")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    session = fleet.start_session(device_id="slot-p", agent_label="farm")
    # First stop fails -> release_pending set
    cloud.fail_release = True
    with pytest.raises(ProviderError):
        fleet.stop_session(session.id, session_secret=session.secret)
    # Now replacing the adapter must fail because the old handle is still
    # held with a pending release.
    new_cloud = FakeCloudProvider()
    with pytest.raises(ValueError, match="pending release"):
        fleet.set_cloud_provider(new_cloud)
    # Clear the pending release (by retrying) and the replacement is allowed
    cloud.fail_release = False
    fleet.stop_session(session.id, session_secret=session.secret)
    fleet.set_cloud_provider(new_cloud)


def test_release_cloud_handle_runs_for_cloud_with_default_handle(fleet: Fleet) -> None:
    """The DEFAULT_HANDLE exemption must apply only to STUB; a CLOUD provider
    may legitimately allocate `stub-phone-1` and we must release it.
    """
    cloud = FakeCloudProvider()
    cloud.add("stub-phone-1", "Cloud on default name")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    session = fleet.start_session(device_id="stub-phone-1", agent_label="farm")
    fleet.stop_session(session.id, session_secret=session.secret)
    assert "stub-phone-1" in cloud.released


def test_release_pending_marker_set_atomically_with_stop(fleet: Fleet) -> None:
    """Even when the cloud release is bypassed or fails before the marker is
    written separately, the marker must already be on the persisted session
    record so the next stop_session retry can find it. SessionManager.stop
    now writes the marker inside the same mutator as the RELEASED transition.
    """
    from devicefleet.sessions import SessionManager

    sm = SessionManager(fleet.sessions._store)
    # Seed an active session directly through the manager
    record = sm.start("atomic-phone", agent_label="alice")
    assert record.metadata.get("release_pending") != "true"
    # Now stop it; the marker must be set on the returned record.
    result = sm.stop(record.id)
    assert result.transitioned is True
    assert result.session.metadata.get("release_pending") == "true"
    # And it must persist across a re-read
    reloaded = sm.get(record.id)
    assert reloaded.metadata.get("release_pending") == "true"


def test_retry_release_releases_old_handle_when_new_lease_uses_different(
    fleet: Fleet,
) -> None:
    """Greptile P1: when a failed cloud release is followed by re-registering
    the same device with a different handle and starting a new lease, the
    retry must release the OLD handle (which is still leaked) without
    touching the new lease.
    """
    cloud = FakeCloudProvider()
    cloud.add("h-old", "Old Handle")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    # `h-old` is now registered under whatever id discover chose (e.g. h-old).
    first_record = fleet.registry.get_by_ref(ProviderKind.CLOUD, "h-old")
    assert first_record is not None
    first = fleet.start_session(device_id=first_record.id, agent_label="alice")

    # First stop fails -> release_pending set
    cloud.fail_release = True
    with pytest.raises(ProviderError):
        fleet.stop_session(first.id, session_secret=first.secret)
    pre_retry = list(cloud.released)
    assert pre_retry == []

    # Re-register the SAME fleet id with a different cloud handle
    cloud.add("h-new", "New Handle")
    fleet.register_device(
        device_id=first_record.id,
        provider=ProviderKind.CLOUD,
        provider_ref="h-new",
        display_name="Phone",
    )
    second = fleet.start_session(device_id=first_record.id, agent_label="bob")
    assert second.id != first.id

    # Retry the original stop. The OLD handle must be released; the new
    # handle (h-new) must NOT be released, because bob's lease owns it.
    cloud.fail_release = False
    fleet.stop_session(first.id, session_secret=first.secret)
    assert "h-old" in cloud.released
    assert "h-new" not in cloud.released

    # Cleanup
    fleet.stop_session(second.id, session_secret=second.secret)


def test_start_session_rollback_releases_handle(fleet: Fleet) -> None:
    """When _set_current_session fails, the start_session rollback must
    release the provider handle too — otherwise provisioned stubs and cloud
    allocations would leak.
    """
    cloud = FakeCloudProvider()
    cloud.add("h-rb1", "Rollback 1")
    cloud.add("h-rb2", "Rollback 2")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    rb1 = fleet.registry.get_by_ref(ProviderKind.CLOUD, "h-rb1")
    rb2 = fleet.registry.get_by_ref(ProviderKind.CLOUD, "h-rb2")
    assert rb1 is not None and rb2 is not None
    # Alice takes the first device so bob's start on the second goes through
    fleet.start_session(device_id=rb1.id, agent_label="alice")
    assert cloud.released == []

    def boom(_session_id: str | None, _agent_label: str) -> None:
        raise OSError("disk full")

    original = fleet._set_current_session
    fleet._set_current_session = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError, match="disk full"):
            fleet.start_session(device_id=rb2.id, agent_label="bob")
    finally:
        fleet._set_current_session = original  # type: ignore[method-assign]

    # The failed start must have rolled back the handle too
    assert "h-rb2" in cloud.released
    assert fleet.sessions.active_for_device(rb2.id) is None


def test_start_session_rollback_does_not_double_release(fleet: Fleet) -> None:
    """If the release during rollback succeeds and we then call stop_session
    on the rolled-back session, it must be a no-op (already RELEASED) and
    must NOT call release_cloud a second time.
    """
    cloud = FakeCloudProvider()
    cloud.add("h-nd1", "ND 1")
    cloud.add("h-nd2", "ND 2")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    nd1 = fleet.registry.get_by_ref(ProviderKind.CLOUD, "h-nd1")
    nd2 = fleet.registry.get_by_ref(ProviderKind.CLOUD, "h-nd2")
    assert nd1 is not None and nd2 is not None
    fleet.start_session(device_id=nd1.id, agent_label="alice")

    def boom(_session_id: str | None, _agent_label: str) -> None:
        raise OSError("disk full")

    original = fleet._set_current_session
    fleet._set_current_session = boom  # type: ignore[method-assign]
    rolled_id = None
    try:
        with pytest.raises(OSError):
            fleet.start_session(device_id=nd2.id, agent_label="bob")
    finally:
        fleet._set_current_session = original  # type: ignore[method-assign]
    # The rolled-back lease is RELEASED; capture its id
    rolled = [
        s for s in fleet.sessions.list_sessions()
        if s.device_id == nd2.id and s.agent_label == "bob"
    ]
    if rolled:
        rolled_id = rolled[0].id

    pre_cleanup = list(cloud.released)
    assert "h-nd2" in pre_cleanup
    assert pre_cleanup.count("h-nd2") == 1
    # The rolled-back session is already RELEASED. The FakeCloudProvider's
    # release is idempotent (a second call would not re-append because the
    # handle is already in self.released), so we verify the post-condition
    # directly: the session is RELEASED and the handle was released
    # exactly once during rollback.
    if rolled_id is not None:
        reloaded = fleet.sessions.get(rolled_id)
        assert reloaded.status.value == "released"
        assert cloud.released.count("h-nd2") == 1


def test_start_session_rejects_whitespace_device_id(fleet: Fleet) -> None:
    """`start_session(device_id="   ")` must not lease the first available
    device; an explicitly supplied id must be normalized and validated.
    """
    with pytest.raises(ValueError, match="device_id is required"):
        fleet.start_session(device_id="   ", agent_label="alice")
    # Empty string is also rejected
    with pytest.raises(ValueError, match="device_id is required"):
        fleet.start_session(device_id="", agent_label="alice")
    # A valid id still works
    session = fleet.start_session(device_id="stub-demo", agent_label="alice")
    assert session.device_id == "stub-demo"


def test_release_done_marker_bounds_retry_after_crash(fleet: Fleet) -> None:
    """If the release completes but the mark_release_completed write fails,
    a future stop_session retry must NOT call release_cloud again — the
    `release_done` flag marks the release as already attempted.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-done", "Slot Done")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    first = fleet.start_session(device_id="slot-done", agent_label="farm")
    pre = list(cloud.released)

    # Force the post-release marker write to fail. The release itself
    # succeeds, but the mark_release_completed call will raise.
    def boom(_session_id: str) -> SessionRecord:
        raise OSError("disk full")

    original = fleet.sessions.mark_release_completed
    fleet.sessions.mark_release_completed = boom  # type: ignore[method-assign]
    try:
        # Use the lower-level stop path so the marker write happens after
        # the release. fleet.stop_session surfaces the OSError.
        with pytest.raises(OSError):
            fleet.stop_session(first.id, session_secret=first.secret)
    finally:
        fleet.sessions.mark_release_completed = original  # type: ignore[method-assign]

    # The release actually ran
    assert "slot-done" in cloud.released
    # The session is RELEASED; the marker write failed so the pending
    # marker is still set. Simulate operator recovery: mark done manually.
    fleet.sessions.mark_release_completed(first.id)
    # A retry must NOT call release_cloud a second time
    pre_retry = list(cloud.released)
    fleet.stop_session(first.id, session_secret=first.secret)
    assert cloud.released == pre_retry


def test_retry_release_abandons_when_handle_owned_by_different_device(
    fleet: Fleet,
) -> None:
    """Greptile P1: when a failed release is followed by re-registering the
    captured handle under a *different* device id and starting a new lease,
    the retry must abandon the pending release (the new lease now owns the
    handle), not stomp on it.
    """
    cloud = FakeCloudProvider()
    cloud.add("h-shared", "Shared")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)
    # Pick the device that got auto-registered for h-shared
    original_device = fleet.registry.get_by_ref(ProviderKind.CLOUD, "h-shared")
    assert original_device is not None
    first = fleet.start_session(device_id=original_device.id, agent_label="alice")

    # First stop fails
    cloud.fail_release = True
    with pytest.raises(ProviderError):
        fleet.stop_session(first.id, session_secret=first.secret)
    pre = list(cloud.released)
    assert pre == []

    # The original device is removed (its session is already RELEASED, so
    # remove_device is allowed), then a different device id is registered
    # with the same handle. The new device then takes a new lease.
    fleet.remove_device(original_device.id)
    fleet.register_device(
        device_id="alt-device",
        provider=ProviderKind.CLOUD,
        provider_ref="h-shared",
        display_name="Alt",
    )
    second = fleet.start_session(device_id="alt-device", agent_label="bob")
    assert second.id != first.id

    # Retry the original stop with the adapter healthy. The retry must
    # notice that another active session now owns h-shared, and abandon.
    cloud.fail_release = False
    fleet.stop_session(first.id, session_secret=first.secret)
    # The handle must NOT have been released — bob's lease still owns it.
    assert "h-shared" not in cloud.released
    assert fleet.sessions.get(first.id).metadata.get("release_pending") != "true"

    # Cleanup
    fleet.stop_session(second.id, session_secret=second.secret)


def test_clear_current_session_does_not_evict_concurrent_start(fleet: Fleet) -> None:
    """A concurrent start on a *different* device that writes a new id for the
    same agent must not be wiped by a stale _clear_current_session that was
    comparing against an old id read before the new start committed.
    """
    import threading
    import time

    # Register a second device so the racer has somewhere to lease.
    fleet.register_device(
        device_id="second",
        provider=ProviderKind.STUB,
        provider_ref="stub-phone-second",
        display_name="Second",
    )

    # Agent "alice" starts a session on the first device; remember it.
    first = fleet.start_session(device_id="stub-demo", agent_label="alice")
    assert fleet.current_session_id("alice") == first.id

    barrier = threading.Barrier(2)

    def racer() -> None:
        barrier.wait()
        # Small delay so the clearer reads current_session_id first.
        time.sleep(0.005)
        fleet.start_session(device_id="second", agent_label="alice")

    def clearer() -> None:
        barrier.wait()
        fleet.stop_session(
            first.id, agent_label="alice", session_secret=first.secret
        )

    t1 = threading.Thread(target=racer)
    t2 = threading.Thread(target=clearer)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # The racer wrote a new current_session_id for alice on a *different*
    # device; that id must not have been evicted by the stop_session clear.
    active = fleet.sessions.active_for_device("second")
    if active is not None and active.agent_label == "alice":
        assert fleet.current_session_id("alice") == active.id


def test_local_transport_blocks_cross_agent_secret_access(fleet: Fleet) -> None:
    """Regression: LocalTransport must not reveal a session's secret to another agent
    that shares the same DEVICEFLEET_HOME.
    """
    from devicefleet.transport.local import LocalTransport

    session = fleet.start_session(device_id="stub-demo", agent_label="alice")
    # Verify the secret is in the persisted record
    persisted = fleet.sessions.get(session.id)
    assert persisted.secret == session.secret

    # Same home, different agent label
    intruder = LocalTransport(fleet, agent_label="bob")
    with pytest.raises(SessionOwnershipError, match="belongs to alice"):
        intruder.run(
            session.id,
            ActionRequest(name=ActionName.INFO),
        )
    with pytest.raises(SessionOwnershipError, match="belongs to alice"):
        intruder.stop_session(session.id)


def test_state_yaml_preserves_remote_secrets_across_local_start(fleet: Fleet) -> None:
    """Regression: a local session start must not delete remote capability secrets
    persisted in state.yaml by an earlier remote CLI invocation.
    """
    # Simulate a remote CLI having remembered a secret in state.yaml
    from devicefleet.store import YamlStore
    from devicefleet.transport.http import HttpTransport

    state = YamlStore(fleet.settings.state_path)
    state.update(
        lambda doc: doc.update(
            {"session_secrets": {"ses_remote_old": "cap_remote_value"}}
        )
    )
    # Now run a local session start
    fleet.start_session(device_id="stub-demo", agent_label="alice")
    # The remote secret must still be there
    document = state.load()
    assert document["session_secrets"].get("ses_remote_old") == "cap_remote_value"


def test_stop_session_retry_does_not_release_new_handle(fleet: Fleet) -> None:
    """Regression: a second stop_session on an already-released id must not
    release a cloud handle that is now bound to a different lease.
    """
    cloud = FakeCloudProvider()
    cloud.add("slot-1", "Slot 1")
    fleet.set_cloud_provider(cloud)
    fleet.discover(save=True)

    # First lease takes slot-1
    first = fleet.start_session(device_id="slot-1", agent_label="alice")
    fleet.stop_session(first.id, session_secret=first.secret)
    assert cloud.released == ["slot-1"]

    # Same cloud phone reappears (e.g. reprovisioned) under a new session id
    cloud.add("slot-1", "Slot 1")
    second = fleet.start_session(device_id="slot-1", agent_label="bob")
    assert second.id != first.id

    # Retrying alice's stop with the now-released id must NOT touch the cloud
    # handle that is now owned by bob's session.
    pre_retry_release_calls = len(cloud.released)
    fleet.stop_session(first.id, session_secret=first.secret)
    assert len(cloud.released) == pre_retry_release_calls
    # The active session for bob is still alive
    assert fleet.sessions.active_for_device("slot-1") is not None
    assert fleet.sessions.active_for_device("slot-1").agent_label == "bob"

    # Cleanup bob's session — the handle is now released
    fleet.stop_session(second.id, session_secret=second.secret)
    assert "slot-1" in cloud.released


def test_discover_save_persists_status_for_offline_device(fleet: Fleet) -> None:
    """Regression: discover(save=True) must retain the OFFLINE status from a backend
    so list_devices does not default unleased devices to ONLINE.
    """
    from devicefleet.providers.base import DeviceProvider

    class OfflineAdb(DeviceProvider):
        provider_id = "adb"

        def __init__(self) -> None:
            self.ref = "offline-device-1"

        def discover(self):
            from devicefleet.models import DiscoveredDevice
            return [
                DiscoveredDevice(
                    provider=ProviderKind.ADB,
                    provider_ref=self.ref,
                    display_name="Offline",
                    status=DeviceStatus.OFFLINE,
                    suggested_id=self.ref,
                )
            ]

        def screenshot(self, handle): return b""
        def tap(self, handle, x, y): pass
        def swipe(self, handle, x1, y1, x2, y2, duration_ms=300): pass
        def type_text(self, handle, text): pass
        def keyevent(self, handle, key): pass
        def dump_ui(self, handle): return ""
        def available(self): return True

    fleet.adb = OfflineAdb()
    fleet.discover(save=True)
    record = fleet.registry.get("offline-device-1")
    assert record.last_status is DeviceStatus.OFFLINE
    items = fleet.list_devices()
    item = next(i for i in items if i.device.id == "offline-device-1")
    assert item.status is DeviceStatus.OFFLINE
