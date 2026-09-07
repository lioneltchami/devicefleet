from __future__ import annotations

from fastapi.testclient import TestClient

from devicefleet.api.app import create_app
from devicefleet.fleet import Fleet
from devicefleet.models import ActionName


def test_health_and_session_action(fleet: Fleet) -> None:
    client = TestClient(create_app(fleet))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    headers = {"X-Devicefleet-Agent": "api-test"}
    created = client.post(
        "/sessions",
        json={"device_id": "stub-demo", "agent_label": "api-test"},
        headers=headers,
    )
    assert created.status_code == 200
    session_id = created.json()["id"]

    shot = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.SCREENSHOT.value},
        headers=headers,
    )
    assert shot.status_code == 200
    assert shot.json()["ok"] is True

    tap = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": "tap", "x": 12, "y": 40},
        headers=headers,
    )
    assert tap.status_code == 200

    missing = client.post(
        "/sessions/ses_missing/actions",
        json={"name": "tap", "x": 1, "y": 1},
        headers=headers,
    )
    assert missing.status_code == 404

    stopped = client.delete(f"/sessions/{session_id}", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "released"


def test_register_and_busy_delete(fleet: Fleet) -> None:
    client = TestClient(create_app(fleet))
    created = client.post(
        "/devices",
        json={
            "device_id": "lab-stub",
            "provider": "stub",
            "provider_ref": "stub-phone-4",
            "display_name": "Lab Stub",
        },
    )
    assert created.status_code == 200
    assert created.json()["id"] == "lab-stub"
    assert fleet.stub.health("stub-phone-4") is True

    client.post(
        "/sessions",
        json={"device_id": "lab-stub", "agent_label": "api-test"},
        headers={"X-Devicefleet-Agent": "api-test"},
    )
    busy = client.delete("/devices/lab-stub")
    assert busy.status_code == 409


def test_action_on_missing_device_is_404(fleet: Fleet) -> None:
    client = TestClient(create_app(fleet))
    headers = {"X-Devicefleet-Agent": "api-test"}
    created = client.post(
        "/sessions",
        json={"device_id": "stub-demo", "agent_label": "api-test"},
        headers=headers,
    )
    session_id = created.json()["id"]
    fleet.registry.remove("stub-demo")
    action = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": "info"},
        headers=headers,
    )
    assert action.status_code == 404
    stopped = client.delete(f"/sessions/{session_id}", headers=headers)
    assert stopped.status_code == 200
