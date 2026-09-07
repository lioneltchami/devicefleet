from __future__ import annotations

from devicefleet.fleet import Fleet
from devicefleet.models import ActionName, ActionRequest


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
