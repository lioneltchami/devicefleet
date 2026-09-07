"""FastAPI surface for session create/list/action on a fleet host."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from devicefleet import __version__
from devicefleet.auth import agent_from_headers, require_fleet_token
from devicefleet.fleet import DeviceInUseError, Fleet, FleetError
from devicefleet.models import (
    ActionRequest,
    ActionResult,
    DeviceListItem,
    DeviceRecord,
    DiscoveredDevice,
    ProviderKind,
    SessionRecord,
)
from devicefleet.providers.base import ProviderError
from devicefleet.registry import DeviceNotFoundError
from devicefleet.sessions import DeviceBusyError, SessionError, SessionNotFoundError, SessionOwnershipError


class DiscoverBody(BaseModel):
    save: bool = False


class StartSessionBody(BaseModel):
    device_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    agent_label: str = "anonymous"


class AttachBody(BaseModel):
    agent_label: str


class RegisterDeviceBody(BaseModel):
    device_id: str
    provider: ProviderKind
    provider_ref: str
    display_name: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SessionOwnershipError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (SessionNotFoundError, DeviceNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DeviceBusyError, DeviceInUseError, FleetError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (SessionError, ProviderError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="internal fleet error")


def create_app(fleet: Fleet | None = None) -> FastAPI:
    """Build the fleet host. Tests pass an isolated Fleet."""

    host_fleet = fleet or Fleet()
    app = FastAPI(
        title="Devicefleet",
        version=__version__,
        description="Multi-device phone control host for software agents.",
    )
    app.state.fleet = host_fleet

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    protected = APIRouter(dependencies=[Depends(require_fleet_token)])

    @protected.get("/devices", response_model=list[DeviceListItem])
    def list_devices(
        tags: str | None = Query(default=None, description="Comma-separated tags"),
    ) -> list[DeviceListItem]:
        tag_list = [part for part in (tags or "").split(",") if part.strip()]
        return host_fleet.list_devices(tags=tag_list or None)

    @protected.post("/devices/discover", response_model=list[DiscoveredDevice])
    def discover(body: DiscoverBody) -> list[DiscoveredDevice]:
        return host_fleet.discover(save=body.save)

    @protected.post("/devices", response_model=DeviceRecord)
    def register_device(body: RegisterDeviceBody) -> DeviceRecord:
        try:
            return host_fleet.register_device(
                device_id=body.device_id,
                provider=body.provider,
                provider_ref=body.provider_ref,
                display_name=body.display_name,
                tags=body.tags,
                notes=body.notes,
            )
        except ValueError as exc:
            raise _http_error(exc) from exc

    @protected.delete("/devices/{device_id}", response_model=DeviceRecord)
    def remove_device(device_id: str) -> DeviceRecord:
        try:
            return host_fleet.remove_device(device_id)
        except (DeviceNotFoundError, DeviceInUseError) as exc:
            raise _http_error(exc) from exc

    @protected.post("/sessions", response_model=SessionRecord)
    def start_session(body: StartSessionBody) -> SessionRecord:
        try:
            return host_fleet.start_session(
                device_id=body.device_id,
                tags=body.tags or None,
                agent_label=body.agent_label,
            )
        except (DeviceNotFoundError, DeviceBusyError, FleetError) as exc:
            raise _http_error(exc) from exc

    @protected.get("/sessions", response_model=list[SessionRecord])
    def list_sessions(active_only: bool = False) -> list[SessionRecord]:
        return host_fleet.sessions.list_sessions(active_only=active_only)

    @protected.get("/sessions/{session_id}", response_model=SessionRecord)
    def get_session(session_id: str) -> SessionRecord:
        try:
            return host_fleet.sessions.get(session_id)
        except SessionNotFoundError as exc:
            raise _http_error(exc) from exc

    @protected.post("/sessions/{session_id}/attach", response_model=SessionRecord)
    def attach_session(session_id: str, body: AttachBody) -> SessionRecord:
        try:
            return host_fleet.attach_session(session_id, agent_label=body.agent_label)
        except (SessionNotFoundError, SessionError, SessionOwnershipError) as exc:
            raise _http_error(exc) from exc

    @protected.delete("/sessions/{session_id}", response_model=SessionRecord)
    def stop_session(session_id: str, request: Request) -> SessionRecord:
        agent = agent_from_headers(request)
        try:
            return host_fleet.stop_session(session_id, agent_label=agent)
        except (
            SessionNotFoundError,
            SessionOwnershipError,
            DeviceNotFoundError,
            SessionError,
        ) as exc:
            raise _http_error(exc) from exc

    @protected.post("/sessions/{session_id}/actions", response_model=ActionResult)
    def run_action(session_id: str, body: ActionRequest, request: Request) -> ActionResult:
        agent = agent_from_headers(request)
        try:
            return host_fleet.run(session_id, body, agent_label=agent)
        except (
            SessionNotFoundError,
            SessionOwnershipError,
            DeviceNotFoundError,
            SessionError,
            ProviderError,
            ValueError,
        ) as exc:
            raise _http_error(exc) from exc

    app.include_router(protected)
    return app
