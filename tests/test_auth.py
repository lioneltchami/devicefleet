from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devicefleet.api.app import create_app
from devicefleet.auth import is_loopback_host, validate_serve_bind
from devicefleet.config import load_settings
from devicefleet.fleet import Fleet
from devicefleet.models import ActionName, ActionRequest
from devicefleet.transport.local import LocalTransport


def _authed_fleet(tmp_path: Path, token: str = "secret-token") -> Fleet:
    settings = load_settings(tmp_path / "home")
    settings.token = token
    return Fleet(settings)


def test_validate_serve_bind() -> None:
    validate_serve_bind("127.0.0.1", None)
    validate_serve_bind("0.0.0.0", "secret")
    assert is_loopback_host("localhost")
    with pytest.raises(ValueError, match="DEVICEFLEET_TOKEN"):
        validate_serve_bind("0.0.0.0", None)
    with pytest.raises(ValueError, match="DEVICEFLEET_TOKEN"):
        validate_serve_bind("10.0.0.5", "")


def test_api_requires_token_when_configured(tmp_path: Path) -> None:
    fleet = _authed_fleet(tmp_path)
    client = TestClient(create_app(fleet))
    assert client.get("/health").status_code == 200
    assert client.get("/devices").status_code == 401
    assert client.post("/sessions", json={"device_id": "stub-demo"}).status_code == 401

    headers = {"Authorization": "Bearer secret-token", "X-Devicefleet-Agent": "api-test"}
    listed = client.get("/devices", headers=headers)
    assert listed.status_code == 200

    created = client.post(
        "/sessions",
        json={"device_id": "stub-demo", "agent_label": "api-test"},
        headers=headers,
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    secret = created.json()["secret"]
    owned = {**headers, "X-Devicefleet-Session": secret}

    listed = client.get("/sessions", headers=headers)
    assert listed.status_code == 200
    assert all("secret" not in item for item in listed.json())

    spoofed = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.TAP.value, "x": 1, "y": 1},
        headers={**headers, "X-Devicefleet-Agent": "api-test"},
    )
    assert spoofed.status_code == 403

    hijack = client.post(
        f"/sessions/{session_id}/attach",
        json={"agent_label": "other-agent"},
        headers={**headers, "X-Devicefleet-Agent": "other-agent"},
    )
    assert hijack.status_code == 403

    attach = client.post(
        f"/sessions/{session_id}/attach",
        json={},
        headers=owned,
    )
    assert attach.status_code == 200
    assert attach.json()["secret"] == secret

    stolen = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.TAP.value, "x": 1, "y": 1},
        headers={**headers, "X-Devicefleet-Agent": "thief"},
    )
    assert stolen.status_code == 403

    shot = client.post(
        f"/sessions/{session_id}/actions",
        json={"name": ActionName.SCREENSHOT.value},
        headers=owned,
    )
    assert shot.status_code == 200

    stop_other = client.delete(
        f"/sessions/{session_id}",
        headers={**headers, "X-Devicefleet-Agent": "thief"},
    )
    assert stop_other.status_code == 403

    stopped = client.delete(f"/sessions/{session_id}", headers=owned)
    assert stopped.status_code == 200


def test_http_transport_never_implies_a_current_session() -> None:
    from devicefleet.transport.http import HttpTransport

    transport = HttpTransport("http://127.0.0.1:8765", token="secret", agent_label="alice")
    assert transport.current_session_id() is None


def test_header_token_is_accepted(tmp_path: Path) -> None:
    fleet = _authed_fleet(tmp_path)
    client = TestClient(create_app(fleet))
    headers = {"X-Devicefleet-Token": "secret-token"}
    assert client.get("/devices", headers=headers).status_code == 200


def test_local_transport_uses_transport_agent(tmp_path: Path) -> None:
    fleet = Fleet(load_settings(tmp_path / "home"))
    transport = LocalTransport(fleet, agent_label="alice")
    session = transport.start_session(device_id="stub-demo")
    assert session.agent_label == "alice"
    result = transport.run(session.id, ActionRequest(name=ActionName.INFO))
    assert result.ok
    stopped = transport.stop_session(session.id)
    assert stopped.status.value == "released"
