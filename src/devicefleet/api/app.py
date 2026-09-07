"""FastAPI surface for session create/list/action on a fleet host."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from devicefleet import __version__
from devicefleet.auth import (
    agent_from_headers,
    agent_header_optional,
    require_fleet_token,
    session_secret_from_headers,
)
from devicefleet.fleet import DeviceInUseError, Fleet, FleetError
from devicefleet.models import (
    ActionRequest,
    ActionResult,
    DeviceListItem,
    DeviceRecord,
    DiscoveredDevice,
    ProviderKind,
    SessionGrant,
    SessionPublic,
)
from devicefleet.providers.base import ProviderError
from devicefleet.registry import DeviceNotFoundError, DuplicateDeviceError
from devicefleet.sessions import DeviceBusyError, SessionError, SessionNotFoundError, SessionOwnershipError


class DiscoverBody(BaseModel):
    save: bool = False


class StartSessionBody(BaseModel):
    device_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    agent_label: str | None = None


class AttachBody(BaseModel):
    agent_label: str | None = None
    secret: str | None = None


class RegisterDeviceBody(BaseModel):
    device_id: str
    provider: ProviderKind
    provider_ref: str
    display_name: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SessionOwnershipError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (SessionNotFoundError, DeviceNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DeviceBusyError, DeviceInUseError, DuplicateDeviceError, FleetError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (SessionError, ProviderError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="internal fleet error")


def _grant(session) -> SessionGrant:
    return SessionGrant.model_validate(session.model_dump())


def _public(session) -> SessionPublic:
    return SessionPublic.model_validate(session.public_dump())


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
        tag: list[str] | None = Query(default=None),
    ) -> list[DeviceListItem]:
        return host_fleet.list_devices(tags=tag)

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
        except (ValueError, FleetError, DuplicateDeviceError) as exc:
            raise _http_error(exc) from exc

    @protected.delete("/devices/{device_id:path}", response_model=DeviceRecord)
    def remove_device(device_id: str) -> DeviceRecord:
        try:
            return host_fleet.remove_device(device_id)
        except (DeviceNotFoundError, DeviceInUseError) as exc:
            raise _http_error(exc) from exc

    @protected.post("/sessions", response_model=SessionGrant)
    def start_session(body: StartSessionBody, request: Request) -> SessionGrant:
        agent = body.agent_label or agent_from_headers(request)
        try:
            session = host_fleet.start_session(
                device_id=body.device_id,
                tags=body.tags or None,
                agent_label=agent,
            )
        except (DeviceNotFoundError, DeviceBusyError, FleetError) as exc:
            raise _http_error(exc) from exc
        return _grant(session)

    @protected.get("/sessions", response_model=list[SessionPublic])
    def list_sessions(active_only: bool = False) -> list[SessionPublic]:
        return [_public(item) for item in host_fleet.sessions.list_sessions(active_only=active_only)]

    @protected.get("/sessions/{session_id}", response_model=SessionPublic)
    def get_session(session_id: str) -> SessionPublic:
        try:
            return _public(host_fleet.sessions.get(session_id))
        except SessionNotFoundError as exc:
            raise _http_error(exc) from exc

    @protected.post("/sessions/{session_id}/attach", response_model=SessionGrant)
    def attach_session(
        session_id: str,
        request: Request,
        body: AttachBody = Body(default_factory=AttachBody),
    ) -> SessionGrant:
        agent = body.agent_label or agent_header_optional(request)
        secret = body.secret or session_secret_from_headers(request)
        try:
            session = host_fleet.attach_session(
                session_id, agent_label=agent, session_secret=secret
            )
        except (SessionNotFoundError, SessionError, SessionOwnershipError) as exc:
            raise _http_error(exc) from exc
        return _grant(session)

    @protected.delete("/sessions/{session_id}", response_model=SessionPublic)
    def stop_session(session_id: str, request: Request) -> SessionPublic:
        agent = agent_header_optional(request)
        secret = session_secret_from_headers(request)
        try:
            session = host_fleet.stop_session(
                session_id, agent_label=agent, session_secret=secret
            )
        except (
            SessionNotFoundError,
            SessionOwnershipError,
            DeviceNotFoundError,
            SessionError,
        ) as exc:
            raise _http_error(exc) from exc
        return _public(session)

    @protected.get("/sessions/{session_id}/artifacts/{name}")
    def get_session_artifact(
        session_id: str, name: str, request: Request
    ) -> FileResponse:
        secret = session_secret_from_headers(request)
        try:
            host_fleet.sessions.require_secret(session_id, secret)
            path = host_fleet.artifact_file(session_id, name)
        except (SessionNotFoundError, SessionOwnershipError, SessionError, ValueError) as exc:
            raise _http_error(exc) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
        return FileResponse(path)

    @protected.post("/sessions/{session_id}/actions", response_model=ActionResult)
    def run_action(session_id: str, body: ActionRequest, request: Request) -> ActionResult:
        agent = agent_header_optional(request)
        secret = session_secret_from_headers(request)
        try:
            return host_fleet.run(
                session_id, body, agent_label=agent, session_secret=secret
            )
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
