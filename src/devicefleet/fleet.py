"""Fleet orchestrator: registry + sessions + providers, used by CLI and API."""

from __future__ import annotations

from pathlib import Path

from devicefleet.config import Settings, load_settings
from devicefleet.models import (
    ActionName,
    ActionRequest,
    ActionResult,
    CloudDeviceSpec,
    DeviceListItem,
    DeviceRecord,
    DeviceStatus,
    DiscoveredDevice,
    ProviderKind,
    SessionRecord,
    SessionStatus,
)
from devicefleet.providers.adb import LocalAdbProvider
from devicefleet.providers.base import DeviceProvider, ProviderError
from devicefleet.providers.stub import StubCloudProvider
from devicefleet.registry import DeviceNotFoundError, DeviceRegistry
from devicefleet.sessions import SessionError, SessionManager, SessionNotFoundError
from devicefleet.store import YamlStore


class FleetError(RuntimeError):
    """High-level fleet operation failed."""


class Fleet:
    """In-process fleet host.

    This is the object both the CLI (local transport) and the FastAPI app call.
    Remote agents talk to the same operations over HTTP.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.settings.ensure_dirs()
        self.registry = DeviceRegistry(YamlStore(self.settings.devices_path))
        self.sessions = SessionManager(YamlStore(self.settings.sessions_path))
        self.state_store = YamlStore(self.settings.state_path)
        self.adb = LocalAdbProvider(
            adb_bin=self.settings.adb_bin,
            timeout_s=self.settings.adb_timeout_s,
        )
        self.stub = StubCloudProvider(state_path=self.settings.stub_state_path)
        self.registry.ensure_stub_demo()

    def provider_for(self, kind: ProviderKind) -> DeviceProvider:
        if kind is ProviderKind.ADB:
            return self.adb
        if kind is ProviderKind.STUB:
            return self.stub
        if kind is ProviderKind.CLOUD:
            # Reserved for a future paid farm adapter registered at runtime.
            return self.stub
        raise FleetError(f"unsupported provider: {kind}")

    def discover(self, save: bool = False) -> list[DiscoveredDevice]:
        """Ask every available backend what phones it can see."""
        found: list[DiscoveredDevice] = []
        found.extend(self.stub.discover())
        if self.adb.available():
            try:
                found.extend(self.adb.discover())
            except ProviderError:
                pass
        if save:
            for item in found:
                device_id = item.suggested_id or item.provider_ref
                try:
                    existing = self.registry.get(device_id)
                    self.registry.touch(existing.id)
                except DeviceNotFoundError:
                    self.registry.register(
                        device_id=device_id,
                        provider=item.provider,
                        provider_ref=item.provider_ref,
                        display_name=item.display_name,
                        tags=item.suggested_tags,
                        metadata=item.metadata,
                    )
        return found

    def list_devices(
        self,
        tags: list[str] | None = None,
        provider: ProviderKind | None = None,
    ) -> list[DeviceListItem]:
        items: list[DeviceListItem] = []
        for device in self.registry.find(tags=tags, provider=provider):
            session = self.sessions.active_for_device(device.id)
            status = DeviceStatus.BUSY if session else DeviceStatus.ONLINE
            items.append(
                DeviceListItem(
                    device=device,
                    status=status,
                    session_id=session.id if session else None,
                    agent_label=session.agent_label if session else None,
                )
            )
        return items

    def start_session(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        agent_label: str = "anonymous",
    ) -> SessionRecord:
        device = self._select_idle_device(device_id=device_id, tags=tags)
        session = self.sessions.start(device.id, agent_label=agent_label)
        self._set_current_session(session.id)
        return session

    def attach_session(
        self, session_id: str, agent_label: str | None = None
    ) -> SessionRecord:
        session = self.sessions.attach(session_id, agent_label=agent_label)
        self._set_current_session(session.id)
        return session

    def stop_session(self, session_id: str) -> SessionRecord:
        session = self.sessions.stop(session_id)
        current = self.current_session_id()
        if current == session_id:
            self._set_current_session(None)
        device = self.registry.get(session.device_id)
        if device.provider in {ProviderKind.STUB, ProviderKind.CLOUD}:
            # Local ADB phones stay physically attached; cloud handles are leased.
            if device.provider_ref != "stub-phone-1":
                try:
                    self.stub.release_cloud(device.provider_ref)
                except ProviderError:
                    pass
        return session

    def run(self, session_id: str, request: ActionRequest) -> ActionResult:
        session = self.sessions.get(session_id)
        if session.status != SessionStatus.ACTIVE:
            raise SessionError(f"session {session_id} is not active")
        device = self.registry.get(session.device_id)
        provider = self.provider_for(device.provider)
        handle = device.provider_ref
        artifact: str | None = None
        payload: dict[str, object] = {}
        message = "ok"

        if request.name is ActionName.SCREENSHOT:
            png = provider.screenshot(handle)
            path = self._write_artifact(session.id, "screenshot.png", png)
            artifact = str(path)
            payload = {"bytes": len(png), "path": artifact}
            message = f"wrote {artifact}"
        elif request.name is ActionName.TAP:
            if request.x is None or request.y is None:
                raise ValueError("tap requires x and y")
            provider.tap(handle, request.x, request.y)
            payload = {"x": request.x, "y": request.y}
        elif request.name is ActionName.SWIPE:
            if None in (request.x, request.y, request.x2, request.y2):
                raise ValueError("swipe requires x y x2 y2")
            provider.swipe(
                handle,
                request.x or 0,
                request.y or 0,
                request.x2 or 0,
                request.y2 or 0,
                request.duration_ms,
            )
            payload = {
                "x": request.x,
                "y": request.y,
                "x2": request.x2,
                "y2": request.y2,
                "duration_ms": request.duration_ms,
            }
        elif request.name is ActionName.TYPE:
            if request.text is None:
                raise ValueError("type requires text")
            provider.type_text(handle, request.text)
            payload = {"text": request.text}
        elif request.name is ActionName.KEY:
            if request.key is None:
                raise ValueError("key requires a key name or code")
            provider.keyevent(handle, request.key)
            payload = {"key": request.key}
        elif request.name is ActionName.DUMP_UI:
            xml = provider.dump_ui(handle)
            path = self._write_artifact(session.id, "ui.xml", xml.encode("utf-8"))
            artifact = str(path)
            payload = {"chars": len(xml), "path": artifact, "xml": xml}
            message = f"wrote {artifact}"
        elif request.name is ActionName.INFO:
            payload = dict(provider.describe(handle))
        else:
            raise ValueError(f"unsupported action: {request.name}")

        self.sessions.touch(session.id)
        self.registry.touch(device.id)
        return ActionResult(
            ok=True,
            action=request.name,
            session_id=session.id,
            device_id=device.id,
            message=message,
            artifact_path=artifact,
            payload=payload,
        )

    def provision_stub(self, spec: CloudDeviceSpec | None = None) -> DeviceRecord:
        """Acquire an extra stub cloud phone and register it."""
        discovered = self.stub.provision(spec or CloudDeviceSpec())
        return self.registry.register(
            device_id=discovered.suggested_id or discovered.provider_ref,
            provider=ProviderKind.STUB,
            provider_ref=discovered.provider_ref,
            display_name=discovered.display_name,
            tags=discovered.suggested_tags,
            metadata=discovered.metadata,
        )

    def current_session_id(self) -> str | None:
        if self.settings.current_session:
            return self.settings.current_session
        document = self.state_store.load()
        value = document.get("current_session")
        return value if isinstance(value, str) and value else None

    def resolve_session_id(self, session_id: str | None) -> str:
        resolved = session_id or self.current_session_id()
        if not resolved:
            raise SessionNotFoundError(
                "no session id given and no current session; run `devicefleet session start`"
            )
        return resolved

    def _select_idle_device(
        self,
        device_id: str | None,
        tags: list[str] | None,
    ) -> DeviceRecord:
        candidates = self.registry.find(device_id=device_id, tags=tags)
        if not candidates:
            hint = device_id or (",".join(tags or []) or "any")
            raise DeviceNotFoundError(f"no registered device matches {hint}")
        idle = [
            device
            for device in candidates
            if self.sessions.active_for_device(device.id) is None
        ]
        if not idle:
            raise FleetError(
                "all matching devices have an active session; "
                "stop one or pick a different id/tag"
            )
        return idle[0]

    def _set_current_session(self, session_id: str | None) -> None:
        self.state_store.save({"current_session": session_id})

    def _write_artifact(self, session_id: str, name: str, data: bytes) -> Path:
        folder = self.settings.artifacts_dir / session_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        return path
