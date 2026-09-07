"""In-process transport used when the CLI owns the fleet data directory."""

from __future__ import annotations

from devicefleet.fleet import Fleet
from devicefleet.models import (
    ActionRequest,
    ActionResult,
    DeviceListItem,
    DeviceRecord,
    DiscoveredDevice,
    ProviderKind,
    SessionRecord,
)


class LocalTransport:
    def __init__(self, fleet: Fleet, agent_label: str | None = None) -> None:
        if fleet is None:
            raise ValueError("fleet is required")
        self.fleet = fleet
        self.agent_label = (agent_label or fleet.settings.agent or "anonymous").strip() or "anonymous"

    def discover(self, save: bool = False) -> list[DiscoveredDevice]:
        return self.fleet.discover(save=save)

    def list_devices(self, tags: list[str] | None = None) -> list[DeviceListItem]:
        return self.fleet.list_devices(tags=tags)

    def register_device(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
    ) -> DeviceRecord:
        return self.fleet.register_device(
            device_id=device_id,
            provider=provider,
            provider_ref=provider_ref,
            display_name=display_name,
            tags=tags,
        )

    def remove_device(self, device_id: str) -> DeviceRecord:
        return self.fleet.remove_device(device_id)

    def start_session(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        agent_label: str = "anonymous",
    ) -> SessionRecord:
        return self.fleet.start_session(
            device_id=device_id, tags=tags, agent_label=agent_label
        )

    def attach_session(
        self, session_id: str, agent_label: str | None = None
    ) -> SessionRecord:
        return self.fleet.attach_session(
            session_id, agent_label=agent_label or self.agent_label
        )

    def list_sessions(self, active_only: bool = False) -> list[SessionRecord]:
        return self.fleet.sessions.list_sessions(active_only=active_only)

    def stop_session(self, session_id: str) -> SessionRecord:
        return self.fleet.stop_session(session_id, agent_label=self.agent_label)

    def run(self, session_id: str, request: ActionRequest) -> ActionResult:
        return self.fleet.run(session_id, request, agent_label=self.agent_label)

    def current_session_id(self) -> str | None:
        return self.fleet.current_session_id(self.agent_label)
