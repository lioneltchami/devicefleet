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
from devicefleet.providers.stub import DEFAULT_HANDLE, StubCloudProvider
from devicefleet.registry import DeviceNotFoundError, DeviceRegistry
from devicefleet.sessions import SessionError, SessionManager, SessionNotFoundError
from devicefleet.store import YamlStore


class FleetError(RuntimeError):
    """High-level fleet operation failed."""


class DeviceInUseError(FleetError):
    """A device cannot be removed while a session holds it."""


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
        self._rehydrate_stub_registry()

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
                    self.registry.set_status(existing.id, item.status)
                except DeviceNotFoundError:
                    self.registry.register(
                        device_id=device_id,
                        provider=item.provider,
                        provider_ref=item.provider_ref,
                        display_name=item.display_name,
                        tags=item.suggested_tags,
                        metadata=item.metadata,
                        last_status=item.status,
                    )
                    if item.provider is ProviderKind.STUB:
                        self.stub.ensure(item.provider_ref, item.display_name)
        return found

    def list_devices(
        self,
        tags: list[str] | None = None,
        provider: ProviderKind | None = None,
    ) -> list[DeviceListItem]:
        live = self._probe_live_statuses()
        items: list[DeviceListItem] = []
        for device in self.registry.find(tags=tags, provider=provider):
            session = self.sessions.active_for_device(device.id)
            if session:
                status = DeviceStatus.BUSY
            else:
                status = live.get((device.provider, device.provider_ref), device.last_status)
                if status is DeviceStatus.UNKNOWN:
                    status = live.get(
                        (device.provider, device.provider_ref), DeviceStatus.UNKNOWN
                    )
                if status is not DeviceStatus.BUSY and status != device.last_status:
                    self.registry.set_status(device.id, status)
                    device = self.registry.get(device.id)
            items.append(
                DeviceListItem(
                    device=device,
                    status=status,
                    session_id=session.id if session else None,
                    agent_label=session.agent_label if session else None,
                )
            )
        return items

    def register_device(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        notes: str = "",
    ) -> DeviceRecord:
        """Add a phone to the host registry (used by CLI and HTTP)."""
        record = self.registry.register(
            device_id=device_id,
            provider=provider,
            provider_ref=provider_ref,
            display_name=display_name,
            tags=tags,
            metadata=metadata,
            notes=notes,
        )
        if record.provider is ProviderKind.STUB:
            self.stub.ensure(record.provider_ref, record.display_name)
        return record

    def remove_device(self, device_id: str) -> DeviceRecord:
        """Remove a phone. Refuses if an active session holds it."""
        busy = self.sessions.active_for_device(device_id)
        if busy is not None:
            raise DeviceInUseError(
                f"device {device_id} is held by session {busy.id} "
                f"({busy.agent_label}); stop the session first"
            )
        return self.registry.remove(device_id)

    def start_session(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        agent_label: str = "anonymous",
    ) -> SessionRecord:
        device = self._select_idle_device(device_id=device_id, tags=tags)
        label = agent_label.strip() or "anonymous"
        session = self.sessions.start(
            device.id,
            agent_label=label,
            metadata={
                "provider": device.provider.value,
                "provider_ref": device.provider_ref,
            },
        )
        self._set_current_session(session.id, label)
        return session

    def attach_session(
        self, session_id: str, agent_label: str | None = None
    ) -> SessionRecord:
        session = self.sessions.attach(session_id, agent_label=agent_label)
        self._set_current_session(session.id, session.agent_label)
        return session

    def stop_session(
        self, session_id: str, agent_label: str | None = None
    ) -> SessionRecord:
        if agent_label:
            self.sessions.require_owner(session_id, agent_label)
        session = self.sessions.stop(session_id)
        self._clear_current_session(session.id, session.agent_label)
        self._release_cloud_handle(session)
        return session

    def run(
        self,
        session_id: str,
        request: ActionRequest,
        agent_label: str | None = None,
    ) -> ActionResult:
        if agent_label:
            session = self.sessions.require_owner(session_id, agent_label)
        else:
            session = self.sessions.get(session_id)
            if session.status != SessionStatus.ACTIVE:
                raise SessionError(f"session {session_id} is not active")
        try:
            device = self.registry.get(session.device_id)
        except DeviceNotFoundError:
            raise DeviceNotFoundError(
                f"device {session.device_id} for session {session_id} is gone"
            ) from None
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
            last_status=DeviceStatus.ONLINE,
        )

    def current_session_id(self, agent_label: str | None = None) -> str | None:
        """Return this agent's remembered session, never another agent's."""
        if self.settings.current_session:
            return self.settings.current_session
        agent = (agent_label or self.settings.agent or "anonymous").strip() or "anonymous"
        document = self.state_store.load()
        mapping = document.get("current_sessions")
        if isinstance(mapping, dict):
            value = mapping.get(agent)
            if isinstance(value, str) and value:
                return value
        if agent == "anonymous":
            legacy = document.get("current_session")
            if isinstance(legacy, str) and legacy:
                return legacy
        return None

    def resolve_session_id(
        self, session_id: str | None, agent_label: str | None = None
    ) -> str:
        resolved = session_id or self.current_session_id(agent_label)
        if not resolved:
            raise SessionNotFoundError(
                "no session id given and no current session for this agent; "
                "run `devicefleet session start` or pass --session"
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

    def _set_current_session(self, session_id: str | None, agent_label: str) -> None:
        agent = agent_label.strip() or "anonymous"
        document = self.state_store.load()
        mapping = document.get("current_sessions")
        if not isinstance(mapping, dict):
            mapping = {}
        else:
            mapping = dict(mapping)
        if session_id:
            mapping[agent] = session_id
        else:
            mapping.pop(agent, None)
        self.state_store.save({"current_sessions": mapping})

    def _clear_current_session(self, session_id: str, agent_label: str) -> None:
        current = self.current_session_id(agent_label)
        if current == session_id:
            self._set_current_session(None, agent_label)

    def _write_artifact(self, session_id: str, name: str, data: bytes) -> Path:
        folder = self.settings.artifacts_dir / session_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        return path

    def _rehydrate_stub_registry(self) -> None:
        for device in self.registry.list_devices():
            if device.provider is ProviderKind.STUB:
                self.stub.ensure(device.provider_ref, device.display_name)

    def _probe_live_statuses(
        self,
    ) -> dict[tuple[ProviderKind, str], DeviceStatus]:
        live: dict[tuple[ProviderKind, str], DeviceStatus] = {}
        for device in self.registry.list_devices():
            if device.provider is ProviderKind.STUB:
                online = self.stub.health(device.provider_ref)
                live[(ProviderKind.STUB, device.provider_ref)] = (
                    DeviceStatus.ONLINE if online else DeviceStatus.OFFLINE
                )
        if self.adb.available():
            try:
                for item in self.adb.discover():
                    live[(ProviderKind.ADB, item.provider_ref)] = item.status
            except ProviderError:
                pass
            for device in self.registry.list_devices():
                if device.provider is ProviderKind.ADB:
                    live.setdefault(
                        (ProviderKind.ADB, device.provider_ref), DeviceStatus.OFFLINE
                    )
        else:
            for device in self.registry.list_devices():
                if device.provider is ProviderKind.ADB:
                    live[(ProviderKind.ADB, device.provider_ref)] = DeviceStatus.OFFLINE
        return live

    def _release_cloud_handle(self, session: SessionRecord) -> None:
        handle = session.metadata.get("provider_ref")
        kind = session.metadata.get("provider")
        try:
            device = self.registry.get(session.device_id)
            handle = device.provider_ref
            kind = device.provider.value
        except DeviceNotFoundError:
            pass
        if kind not in {ProviderKind.STUB.value, ProviderKind.CLOUD.value}:
            return
        if not handle or handle == DEFAULT_HANDLE:
            return
        try:
            self.stub.release_cloud(handle)
        except ProviderError:
            pass
