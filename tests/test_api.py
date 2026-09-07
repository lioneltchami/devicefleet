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

    created = client.post(
        "/sessions",
        json={"device_id": "stub-demo", "agent_label": "api-test"},
    )
    assert created.status_code == 200
    session_id = created.json()["id"]

    shot = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.SCREENSHOT.value},
    )
    assert shot.status_code == 200
    assert shot.json()["ok"] is True

    tap = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": "tap", "x": 12, "y": 40},
    )
    assert tap.status_code == 200

    stopped = client.delete(f"/sessions/{session_id}")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "released"
