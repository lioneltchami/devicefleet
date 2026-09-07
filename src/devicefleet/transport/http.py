"""HTTP client transport for agents that talk to a remote fleet host."""

from __future__ import annotations

from typing import Any

import httpx

from devicefleet.auth import AGENT_HEADER, TOKEN_HEADER
from devicefleet.models import (
    ActionRequest,
    ActionResult,
    DeviceListItem,
    DeviceRecord,
    DiscoveredDevice,
    ProviderKind,
    SessionRecord,
)


class HttpTransport:
    """Call the FastAPI fleet host. Same operations as LocalTransport."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        agent_label: str = "anonymous",
        timeout_s: float = 60.0,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_label = agent_label.strip() or "anonymous"
        self.timeout_s = timeout_s

    def discover(self, save: bool = False) -> list[DiscoveredDevice]:
        data = self._request("POST", "/devices/discover", json={"save": save})
        return [DiscoveredDevice.model_validate(item) for item in data]

    def list_devices(self, tags: list[str] | None = None) -> list[DeviceListItem]:
        params: dict[str, str] = {}
        if tags:
            params["tags"] = ",".join(tags)
        data = self._request("GET", "/devices", params=params)
        return [DeviceListItem.model_validate(item) for item in data]

    def register_device(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
    ) -> DeviceRecord:
        payload = {
            "device_id": device_id,
            "provider": provider.value,
            "provider_ref": provider_ref,
            "display_name": display_name,
            "tags": tags or [],
        }
        data = self._request("POST", "/devices", json=payload)
        return DeviceRecord.model_validate(data)

    def remove_device(self, device_id: str) -> DeviceRecord:
        data = self._request("DELETE", f"/devices/{device_id}")
        return DeviceRecord.model_validate(data)

    def start_session(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        agent_label: str = "anonymous",
    ) -> SessionRecord:
        payload = {
            "device_id": device_id,
            "tags": tags or [],
            "agent_label": agent_label,
        }
        data = self._request("POST", "/sessions", json=payload)
        return SessionRecord.model_validate(data)

    def attach_session(
        self, session_id: str, agent_label: str | None = None
    ) -> SessionRecord:
        label = agent_label or self.agent_label
        data = self._request(
            "POST",
            f"/sessions/{session_id}/attach",
            json={"agent_label": label},
        )
        return SessionRecord.model_validate(data)

    def list_sessions(self, active_only: bool = False) -> list[SessionRecord]:
        data = self._request(
            "GET",
            "/sessions",
            params={"active_only": "true" if active_only else "false"},
        )
        return [SessionRecord.model_validate(item) for item in data]

    def stop_session(self, session_id: str) -> SessionRecord:
        data = self._request("DELETE", f"/sessions/{session_id}")
        return SessionRecord.model_validate(data)

    def run(self, session_id: str, request: ActionRequest) -> ActionResult:
        data = self._request(
            "POST",
            f"/sessions/{session_id}/actions",
            json=request.model_dump(mode="json"),
        )
        return ActionResult.model_validate(data)

    def current_session_id(self) -> str | None:
        """Remote agents must pass --session; the host has no shared current."""
        return None

    def _headers(self) -> dict[str, str]:
        headers = {AGENT_HEADER: self.agent_label}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers[TOKEN_HEADER] = self.token
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.request(
                method, url, json=json, params=params, headers=self._headers()
            )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise RuntimeError(f"fleet host {method} {path} failed: {detail}")
        return response.json()


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return response.text
