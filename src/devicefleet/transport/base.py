"""Transport protocol: the CLI talks to either a local Fleet or a remote host."""

from __future__ import annotations

from typing import Protocol

from devicefleet.models import (
    ActionRequest,
    ActionResult,
    DeviceListItem,
    DeviceRecord,
    DiscoveredDevice,
    ProviderKind,
    SessionRecord,
)


class FleetTransport(Protocol):
    """Everything an agent CLI needs, independent of process locality."""

    def discover(self, save: bool = False) -> list[DiscoveredDevice]: ...

    def list_devices(
        self, tags: list[str] | None = None
    ) -> list[DeviceListItem]: ...

    def register_device(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
    ) -> DeviceRecord: ...

    def remove_device(self, device_id: str) -> DeviceRecord: ...

    def start_session(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        agent_label: str | None = None,
    ) -> SessionRecord: ...

    def attach_session(
        self,
        session_id: str,
        agent_label: str | None = None,
        session_secret: str | None = None,
    ) -> SessionRecord: ...

    def list_sessions(self, active_only: bool = False) -> list[SessionRecord]: ...

    def stop_session(self, session_id: str) -> SessionRecord: ...

    def run(self, session_id: str, request: ActionRequest) -> ActionResult: ...

    def current_session_id(self) -> str | None: ...
