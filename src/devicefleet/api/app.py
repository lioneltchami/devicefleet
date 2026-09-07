"""FastAPI surface for session create/list/action on a fleet host."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from devicefleet import __version__
from devicefleet.fleet import Fleet, FleetError
from devicefleet.models import ActionRequest, ActionResult, DeviceListItem, DiscoveredDevice, SessionRecord
from devicefleet.providers.base import ProviderError
from devicefleet.registry import DeviceNotFoundError
from devicefleet.sessions import DeviceBusyError, SessionError, SessionNotFoundError


class DiscoverBody(BaseModel):
    save: bool = False


class StartSessionBody(BaseModel):
    device_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    agent_label: str = "anonymous"


class AttachBody(BaseModel):
    agent_label: str | None = None


class CurrentSessionBody(BaseModel):
    session_id: str | None = None


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

    @app.get("/devices", response_model=list[DeviceListItem])
    def list_devices(
        tags: str | None = Query(default=None, description="Comma-separated tags"),
    ) -> list[DeviceListItem]:
        tag_list = [part for part in (tags or "").split(",") if part.strip()]
        return host_fleet.list_devices(tags=tag_list or None)

    @app.post("/devices/discover", response_model=list[DiscoveredDevice])
    def discover(body: DiscoverBody) -> list[DiscoveredDevice]:
        return host_fleet.discover(save=body.save)

    @app.post("/sessions", response_model=SessionRecord)
    def start_session(body: StartSessionBody) -> SessionRecord:
        try:
            return host_fleet.start_session(
                device_id=body.device_id,
                tags=body.tags or None,
                agent_label=body.agent_label,
            )
        except (DeviceNotFoundError, DeviceBusyError, FleetError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/sessions", response_model=list[SessionRecord])
    def list_sessions(active_only: bool = False) -> list[SessionRecord]:
        return host_fleet.sessions.list_sessions(active_only=active_only)

    @app.get("/sessions/current", response_model=CurrentSessionBody)
    def current_session() -> CurrentSessionBody:
        return CurrentSessionBody(session_id=host_fleet.current_session_id())

    @app.get("/sessions/{session_id}", response_model=SessionRecord)
    def get_session(session_id: str) -> SessionRecord:
        try:
            return host_fleet.sessions.get(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/sessions/{session_id}/attach", response_model=SessionRecord)
    def attach_session(session_id: str, body: AttachBody) -> SessionRecord:
        try:
            return host_fleet.attach_session(session_id, agent_label=body.agent_label)
        except (SessionNotFoundError, SessionError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/sessions/{session_id}", response_model=SessionRecord)
    def stop_session(session_id: str) -> SessionRecord:
        try:
            return host_fleet.stop_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/sessions/{session_id}/actions", response_model=ActionResult)
    def run_action(session_id: str, request: ActionRequest) -> ActionResult:
        try:
            return host_fleet.run(session_id, request)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (SessionError, ProviderError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app
