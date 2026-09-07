"""Persistent multi-device registry (JSON/YAML on disk)."""

from __future__ import annotations

from datetime import datetime

from devicefleet.models import DeviceRecord, ProviderKind, utcnow
from devicefleet.store import YamlStore


class DeviceNotFoundError(KeyError):
    """Raised when a registry lookup misses."""


class DeviceRegistry:
    """Register, list, and look up phones by id or tags."""

    def __init__(self, store: YamlStore) -> None:
        self._store = store

    def _read_all(self) -> list[DeviceRecord]:
        document = self._store.load()
        raw_items = document.get("devices", [])
        if raw_items is None:
            return []
        if not isinstance(raw_items, list):
            raise ValueError("devices.yaml must contain a list under 'devices'")
        return [DeviceRecord.model_validate(item) for item in raw_items]

    def _write_all(self, devices: list[DeviceRecord]) -> None:
        self._store.save(
            {"devices": [device.model_dump(mode="json") for device in devices]}
        )

    def list_devices(self) -> list[DeviceRecord]:
        """Return every registered device, oldest first."""
        return self._read_all()

    def get(self, device_id: str) -> DeviceRecord:
        """Return a device by fleet id."""
        for device in self._read_all():
            if device.id == device_id:
                return device
        raise DeviceNotFoundError(f"device not found: {device_id}")

    def find(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        provider: ProviderKind | None = None,
    ) -> list[DeviceRecord]:
        """Filter devices. Tags are an AND match on the normalized set."""
        wanted = {tag.strip().lower() for tag in (tags or []) if tag.strip()}
        matches: list[DeviceRecord] = []
        for device in self._read_all():
            if device_id and device.id != device_id:
                continue
            if provider and device.provider != provider:
                continue
            if wanted and not wanted.issubset(set(device.tags)):
                continue
            matches.append(device)
        return matches

    def upsert(self, device: DeviceRecord) -> DeviceRecord:
        """Insert or replace a device with the same id."""
        devices = [item for item in self._read_all() if item.id != device.id]
        devices.append(device)
        devices.sort(key=lambda item: item.registered_at)
        self._write_all(devices)
        return device

    def register(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        notes: str = "",
    ) -> DeviceRecord:
        """Add a device. Re-registering the same id updates fields."""
        existing: DeviceRecord | None
        try:
            existing = self.get(device_id)
        except DeviceNotFoundError:
            existing = None
        record = DeviceRecord(
            id=device_id,
            display_name=display_name or device_id,
            provider=provider,
            provider_ref=provider_ref,
            tags=tags or (existing.tags if existing else []),
            metadata=metadata or (existing.metadata if existing else {}),
            registered_at=existing.registered_at if existing else utcnow(),
            last_seen=existing.last_seen if existing else None,
            notes=notes or (existing.notes if existing else ""),
        )
        return self.upsert(record)

    def remove(self, device_id: str) -> DeviceRecord:
        """Delete a device from the registry."""
        devices = self._read_all()
        kept: list[DeviceRecord] = []
        removed: DeviceRecord | None = None
        for device in devices:
            if device.id == device_id:
                removed = device
            else:
                kept.append(device)
        if removed is None:
            raise DeviceNotFoundError(f"device not found: {device_id}")
        self._write_all(kept)
        return removed

    def touch(self, device_id: str, when: datetime | None = None) -> DeviceRecord:
        """Update last_seen after a successful discover or action."""
        device = self.get(device_id)
        updated = device.model_copy(update={"last_seen": when or utcnow()})
        return self.upsert(updated)

    def ensure_stub_demo(self) -> DeviceRecord:
        """Guarantee a demo device exists so the stub path works offline."""
        try:
            return self.get("stub-demo")
        except DeviceNotFoundError:
            return self.register(
                device_id="stub-demo",
                provider=ProviderKind.STUB,
                provider_ref="stub-phone-1",
                display_name="Stub Demo Phone",
                tags=["demo", "stub", "android"],
                notes="Virtual device from StubCloudProvider. No hardware required.",
            )
