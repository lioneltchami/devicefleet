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
from devicefleet.registry import DeviceNotFoundError, DeviceRegistry, DuplicateDeviceError
from devicefleet.sessions import DeviceBusyError, SessionError, SessionManager, SessionNotFoundError
from devicefleet.store import ExclusiveFileLock, YamlStore


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
        self._cloud_provider: DeviceProvider | None = None
        self.registry.ensure_stub_demo()
        self._rehydrate_stub_registry()

    def set_cloud_provider(self, provider: DeviceProvider) -> None:
        """Install a real hosted-phone adapter. Stub is never used for CLOUD."""
        if provider is None:
            raise ValueError("cloud provider is required")
        self._cloud_provider = provider

    def provider_for(self, kind: ProviderKind) -> DeviceProvider:
        if kind is ProviderKind.ADB:
            return self.adb
        if kind is ProviderKind.STUB:
            return self.stub
        if kind is ProviderKind.CLOUD:
            if self._cloud_provider is None:
                raise FleetError(
                    "no cloud adapter is registered; ProviderKind.CLOUD "
                    "does not fall back to StubCloudProvider"
                )
            return self._cloud_provider
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
        if self._cloud_provider is not None:
            try:
                found.extend(self._cloud_provider.discover())
            except ProviderError:
                pass
        if save:
            for item in found:
                by_ref = self.registry.get_by_ref(item.provider, item.provider_ref)
                if by_ref is not None:
                    self.registry.touch(by_ref.id)
                    self.registry.set_status(by_ref.id, item.status)
                    continue
                device_id = item.suggested_id or item.provider_ref
                try:
                    collision = self.registry.get(device_id)
                except DeviceNotFoundError:
                    collision = None
                if collision is not None:
                    continue
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
        notes: str | None = None,
    ) -> DeviceRecord:
        """Add a phone to the host registry (used by CLI and HTTP)."""
        if provider is ProviderKind.CLOUD and self._cloud_provider is None:
            raise FleetError(
                "cannot register a CLOUD device until a cloud adapter is configured"
            )
        ref = provider_ref.strip()
        cleaned = device_id.strip()
        try:
            existing = self.registry.get(cleaned)
        except DeviceNotFoundError:
            existing = None
        identity_changed = existing is not None and (
            existing.provider is not provider or existing.provider_ref != ref
        )
        if identity_changed:
            with self._lease_lock(cleaned):
                busy = self.sessions.active_for_device(cleaned)
                if busy is not None:
                    raise DeviceInUseError(
                        f"device {cleaned} is held by session {busy.id} "
                        f"({busy.agent_label}); stop the session before changing "
                        "provider or provider_ref"
                    )
                return self._persist_register(
                    cleaned, provider, ref, display_name, tags, metadata, notes
                )
        return self._persist_register(
            cleaned, provider, ref, display_name, tags, metadata, notes
        )

    def _persist_register(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None,
        tags: list[str] | None,
        metadata: dict[str, str] | None,
        notes: str | None,
    ) -> DeviceRecord:
        try:
            record = self.registry.register(
                device_id=device_id,
                provider=provider,
                provider_ref=provider_ref,
                display_name=display_name,
                tags=tags,
                metadata=metadata,
                notes=notes,
            )
        except DuplicateDeviceError as exc:
            raise FleetError(str(exc)) from exc
        if record.provider is ProviderKind.STUB:
            self.stub.ensure(record.provider_ref, record.display_name)
        return record

    def remove_device(self, device_id: str) -> DeviceRecord:
        """Remove a phone. Refuses if an active session holds it."""
        cleaned = device_id.strip()
        with self._lease_lock(cleaned):
            busy = self.sessions.active_for_device(cleaned)
            if busy is not None:
                raise DeviceInUseError(
                    f"device {cleaned} is held by session {busy.id} "
                    f"({busy.agent_label}); stop the session first"
                )
            return self.registry.remove(cleaned)

    def start_session(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        agent_label: str = "anonymous",
    ) -> SessionRecord:
        device = self._select_idle_device(device_id=device_id, tags=tags)
        label = agent_label.strip() or "anonymous"
        with self._lease_lock(device.id):
            fresh = self.registry.get(device.id)
            if not self._is_leaseable(fresh):
                busy = self.sessions.active_for_device(fresh.id)
                if busy is not None:
                    raise DeviceBusyError(
                        f"device {fresh.id} is held by session {busy.id} ({busy.agent_label})"
                    )
                raise FleetError(f"device {fresh.id} is not available to lease")
            session = self.sessions.start(
                fresh.id,
                agent_label=label,
                metadata=_session_device_metadata(fresh),
            )
            self._set_current_session(session.id, label)
            return session

    def attach_session(
        self,
        session_id: str,
        agent_label: str | None = None,
        session_secret: str | None = None,
    ) -> SessionRecord:
        peek = self.sessions.get(session_id)
        with self._lease_lock(peek.device_id):
            session = self.sessions.attach(
                session_id, agent_label=agent_label, session_secret=session_secret
            )
            self._set_current_session(session.id, session.agent_label)
            return session

    def stop_session(
        self,
        session_id: str,
        agent_label: str | None = None,
        session_secret: str | None = None,
    ) -> SessionRecord:
        peek = self.sessions.get(session_id)
        with self._lease_lock(peek.device_id):
            current = self.sessions.get(session_id)
            if current.status is SessionStatus.RELEASED:
                self._clear_current_session(current.id, current.agent_label)
                return current
            self.sessions.require_secret(session_id, session_secret, agent_label)
            session = self.sessions.stop(session_id)
            self._clear_current_session(session.id, session.agent_label)
            if current.status is SessionStatus.ACTIVE:
                self._release_cloud_handle(session)
            return session

    def run(
        self,
        session_id: str,
        request: ActionRequest,
        agent_label: str | None = None,
        session_secret: str | None = None,
    ) -> ActionResult:
        peek = self.sessions.get(session_id)
        with self._lease_lock(peek.device_id):
            return self._run_locked(session_id, request, session_secret, agent_label)

    def _run_locked(
        self,
        session_id: str,
        request: ActionRequest,
        session_secret: str | None,
        agent_label: str | None,
    ) -> ActionResult:
        session = self.sessions.require_secret(session_id, session_secret, agent_label)
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
        with self._provision_lock():
            devices = self.registry.list_devices()
            reserved = {device.id for device in devices}
            reserved.update(device.provider_ref for device in devices)
            discovered = self.stub.provision(
                spec or CloudDeviceSpec(), reserved_ids=reserved
            )
            meta = dict(discovered.metadata)
            meta["provisioned"] = "true"
            return self.registry.register(
                device_id=discovered.suggested_id or discovered.provider_ref,
                provider=ProviderKind.STUB,
                provider_ref=discovered.provider_ref,
                display_name=discovered.display_name,
                tags=discovered.suggested_tags,
                metadata=meta,
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
        idle = [device for device in candidates if self._is_leaseable(device)]
        if not idle:
            raise FleetError(
                "all matching devices are leased or unavailable; "
                "stop a session or pick a different id/tag"
            )
        return idle[0]

    def _is_leaseable(self, device: DeviceRecord) -> bool:
        if self.sessions.active_for_device(device.id) is not None:
            return False
        if device.provider is ProviderKind.STUB:
            return self.stub.health(device.provider_ref)
        live = self._probe_live_statuses().get((device.provider, device.provider_ref))
        if device.provider is ProviderKind.ADB:
            return live is DeviceStatus.ONLINE
        if device.provider is ProviderKind.CLOUD:
            if self._cloud_provider is None:
                return False
            return live is DeviceStatus.ONLINE
        return False

    def _lease_lock(self, device_id: str) -> ExclusiveFileLock:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in device_id)
        return ExclusiveFileLock(self.settings.home / "locks" / f"{safe}.lock")

    def _provision_lock(self) -> ExclusiveFileLock:
        return ExclusiveFileLock(self.settings.home / "locks" / "provision-stub.lock")

    def artifact_file(self, session_id: str, name: str) -> Path:
        """Resolve a host-side artifact path; reject traversal."""
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("invalid artifact name")
        path = (self.settings.artifacts_dir / session_id / name).resolve()
        root = self.settings.artifacts_dir.resolve()
        if root not in path.parents and path.parent != root:
            raise ValueError("artifact path escapes the artifacts directory")
        return path

    def _set_current_session(self, session_id: str | None, agent_label: str) -> None:
        agent = agent_label.strip() or "anonymous"

        def mutator(document: dict[str, object]) -> None:
            mapping = document.get("current_sessions")
            merged: dict[str, object] = dict(mapping) if isinstance(mapping, dict) else {}
            if session_id:
                merged[agent] = session_id
            else:
                merged.pop(agent, None)
            document["current_sessions"] = merged

        self.state_store.update(mutator)

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
        if self._cloud_provider is not None:
            try:
                for item in self._cloud_provider.discover():
                    live[(ProviderKind.CLOUD, item.provider_ref)] = item.status
            except ProviderError:
                pass
            for device in self.registry.list_devices():
                if device.provider is ProviderKind.CLOUD:
                    live.setdefault(
                        (ProviderKind.CLOUD, device.provider_ref), DeviceStatus.OFFLINE
                    )
        return live

    def _release_cloud_handle(self, session: SessionRecord) -> None:
        """Release the handle captured at lease time, not the current registry ref."""
        handle = (session.metadata.get("provider_ref") or "").strip()
        kind = (session.metadata.get("provider") or "").strip()
        if kind not in {ProviderKind.STUB.value, ProviderKind.CLOUD.value}:
            return
        if not handle or handle == DEFAULT_HANDLE:
            return
        if kind == ProviderKind.STUB.value and not _is_provisioned(session):
            return
        provider = self.stub if kind == ProviderKind.STUB.value else self._cloud_provider
        release = getattr(provider, "release_cloud", None) if provider is not None else None
        if callable(release):
            try:
                release(handle)
            except ProviderError:
                pass
        if kind != ProviderKind.STUB.value:
            return
        try:
            device = self.registry.get(session.device_id)
        except DeviceNotFoundError:
            return
        if device.provider is ProviderKind.STUB and device.provider_ref == handle:
            try:
                self.registry.remove(session.device_id)
            except DeviceNotFoundError:
                pass


def _session_device_metadata(device: DeviceRecord) -> dict[str, str]:
    metadata = {
        "provider": device.provider.value,
        "provider_ref": device.provider_ref,
    }
    if device.metadata.get("provisioned") == "true":
        metadata["provisioned"] = "true"
    return metadata


def _is_provisioned(session: SessionRecord) -> bool:
    return session.metadata.get("provisioned") == "true"
