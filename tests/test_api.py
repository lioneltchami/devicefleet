from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from devicefleet.api.app import create_app
from devicefleet.fleet import Fleet
from devicefleet.models import ActionName, ActionRequest
from devicefleet.transport.http import HttpTransport


def _owned(headers: dict[str, str], created: dict) -> dict[str, str]:
    return {**headers, "X-Devicefleet-Session": created["secret"]}


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
    owned = _owned(headers, created.json())

    shot = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.SCREENSHOT.value},
        headers=owned,
    )
    assert shot.status_code == 200
    assert shot.json()["ok"] is True

    tap = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": "tap", "x": 12, "y": 40},
        headers=owned,
    )
    assert tap.status_code == 200

    missing = client.post(
        "/sessions/ses_missing/actions",
        json={"name": "tap", "x": 1, "y": 1},
        headers=owned,
    )
    assert missing.status_code == 404

    listed = client.get("/sessions", headers=headers)
    assert listed.status_code == 200
    assert "secret" not in listed.json()[0]

    stopped = client.delete(f"/sessions/{session_id}", headers=owned)
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
    owned = _owned(headers, created.json())
    fleet.registry.remove("stub-demo")
    action = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": "info"},
        headers=owned,
    )
    assert action.status_code == 404
    stopped = client.delete(f"/sessions/{session_id}", headers=owned)
    assert stopped.status_code == 200


def test_artifact_download_and_http_transport(fleet: Fleet, tmp_path: Path) -> None:
    client = TestClient(create_app(fleet))
    headers = {"X-Devicefleet-Agent": "api-test"}
    created = client.post(
        "/sessions",
        json={"device_id": "stub-demo", "agent_label": "api-test"},
        headers=headers,
    )
    session_id = created.json()["id"]
    owned = _owned(headers, created.json())
    shot = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.SCREENSHOT.value},
        headers=owned,
    )
    assert shot.status_code == 200
    name = Path(shot.json()["artifact_path"]).name

    stolen = client.get(
        f"/sessions/{session_id}/artifacts/{name}",
        headers={"X-Devicefleet-Agent": "thief"},
    )
    assert stolen.status_code == 403

    downloaded = client.get(
        f"/sessions/{session_id}/artifacts/{name}",
        headers=owned,
    )
    assert downloaded.status_code == 200
    assert downloaded.content[:8] == b"\x89PNG\r\n\x1a\n"

    traversal = client.get(
        f"/sessions/{session_id}/artifacts/..%2Fdevices.yaml",
        headers=owned,
    )
    assert traversal.status_code in {400, 404}

    dest = tmp_path / "client-artifacts"
    dest.mkdir()
    transport = HttpTransport("http://test", artifacts_dir=dest, agent_label="api-test")

    def fake_request(method: str, path: str, json=None, params=None, session_id=None):  # type: ignore[no-untyped-def]
        del method, params, session_id
        if path.endswith("/actions"):
            return shot.json()
        raise AssertionError(path)

    def fake_bytes(method: str, path: str, session_id: str | None = None) -> bytes:
        del method
        assert path == f"/sessions/{session_id}/artifacts/{name}"
        return downloaded.content

    transport._request = fake_request  # type: ignore[method-assign]
    transport._request_bytes = fake_bytes  # type: ignore[method-assign]
    result = transport.run(session_id, ActionRequest(name=ActionName.SCREENSHOT))
    assert Path(result.artifact_path or "").exists()
    assert dest in Path(result.artifact_path or "").parents
    assert Path(result.artifact_path or "").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_duplicate_and_cloud_register(fleet: Fleet) -> None:
    client = TestClient(create_app(fleet))
    first = client.post(
        "/devices",
        json={
            "device_id": "a",
            "provider": "stub",
            "provider_ref": "stub-phone-4",
        },
    )
    assert first.status_code == 200
    dup = client.post(
        "/devices",
        json={
            "device_id": "b",
            "provider": "stub",
            "provider_ref": "stub-phone-4",
        },
    )
    assert dup.status_code == 409
    cloud = client.post(
        "/devices",
        json={
            "device_id": "cloud-1",
            "provider": "cloud",
            "provider_ref": "slot-1",
        },
    )
    assert cloud.status_code == 409
