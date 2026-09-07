"""Persistent multi-device registry (JSON/YAML on disk)."""

from __future__ import annotations

from datetime import datetime

from devicefleet.models import DeviceRecord, DeviceStatus, ProviderKind, utcnow
from devicefleet.store import YamlStore


class DeviceNotFoundError(KeyError):
    """Raised when a registry lookup misses."""


class DuplicateDeviceError(ValueError):
    """Another fleet id already uses this provider + provider_ref."""


class DeviceRegistry:
    """Register, list, and look up phones by id or tags."""

    def __init__(self, store: YamlStore) -> None:
        self._store = store

    def _parse(self, document: dict[str, object]) -> list[DeviceRecord]:
        raw_items = document.get("devices", [])
        if raw_items is None:
            return []
        if not isinstance(raw_items, list):
            raise ValueError("devices.yaml must contain a list under 'devices'")
        return [DeviceRecord.model_validate(item) for item in raw_items]

    def _dump(self, devices: list[DeviceRecord]) -> dict[str, object]:
        return {"devices": [device.model_dump(mode="json") for device in devices]}

    def _read_all(self) -> list[DeviceRecord]:
        return self._parse(self._store.load())

    def _write_all(self, devices: list[DeviceRecord]) -> None:
        self._store.save(self._dump(devices))

    def list_devices(self) -> list[DeviceRecord]:
        """Return every registered device, oldest first."""
        return self._read_all()

    def get(self, device_id: str) -> DeviceRecord:
        """Return a device by fleet id."""
        cleaned = device_id.strip()
        for device in self._read_all():
            if device.id == cleaned:
                return device
        raise DeviceNotFoundError(f"device not found: {cleaned}")

    def find(
        self,
        device_id: str | None = None,
        tags: list[str] | None = None,
        provider: ProviderKind | None = None,
    ) -> list[DeviceRecord]:
        """Filter devices. Tags are an AND match on the normalized set."""
        wanted = {tag.strip().lower() for tag in (tags or []) if tag.strip()}
        wanted_id = device_id.strip() if device_id else ""
        matches: list[DeviceRecord] = []
        for device in self._read_all():
            if wanted_id and device.id != wanted_id:
                continue
            if provider and device.provider != provider:
                continue
            if wanted and not wanted.issubset(set(device.tags)):
                continue
            matches.append(device)
        return matches

    def upsert(self, device: DeviceRecord) -> DeviceRecord:
        """Insert or replace a device with the same id."""

        def mutator(document: dict[str, object]) -> DeviceRecord:
            devices = [item for item in self._parse(document) if item.id != device.id]
            _reject_duplicate_ref(devices, device)
            devices.append(device)
            devices.sort(key=lambda item: item.registered_at)
            document.clear()
            document.update(self._dump(devices))
            return device

        return self._store.update(mutator)

    def register(
        self,
        device_id: str,
        provider: ProviderKind,
        provider_ref: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        notes: str | None = None,
        last_status: DeviceStatus | None = None,
    ) -> DeviceRecord:
        """Add a device. Re-registering the same id updates fields.

        `None` for tags/metadata/notes means "leave existing". An explicit empty
        value (`[]`, `{}`, `""`) clears the field.
        """
        ref = provider_ref.strip()
        if not ref:
            raise ValueError("provider_ref must not be empty")
        cleaned_id = device_id.strip()
        if not cleaned_id:
            raise ValueError("device id must not be empty")

        def mutator(document: dict[str, object]) -> DeviceRecord:
            devices = self._parse(document)
            existing = next((item for item in devices if item.id == cleaned_id), None)
            if display_name is None:
                name = existing.display_name if existing else cleaned_id
            else:
                name = display_name.strip() or (
                    existing.display_name if existing else cleaned_id
                )
            record = DeviceRecord(
                id=cleaned_id,
                display_name=name,
                provider=provider,
                provider_ref=ref,
                tags=existing.tags if tags is None and existing else (tags or []),
                metadata=existing.metadata
                if metadata is None and existing
                else (metadata or {}),
                registered_at=existing.registered_at if existing else utcnow(),
                last_seen=existing.last_seen if existing else None,
                last_status=last_status
                if last_status is not None
                else (existing.last_status if existing else DeviceStatus.UNKNOWN),
                notes=existing.notes if notes is None and existing else (notes or ""),
            )
            kept = [item for item in devices if item.id != cleaned_id]
            _reject_duplicate_ref(kept, record)
            kept.append(record)
            kept.sort(key=lambda item: item.registered_at)
            document.clear()
            document.update(self._dump(kept))
            return record

        return self._store.update(mutator)

    def get_by_ref(
        self, provider: ProviderKind, provider_ref: str
    ) -> DeviceRecord | None:
        """Return the device registered for this backend handle, if any."""
        ref = provider_ref.strip()
        if not ref:
            raise ValueError("provider_ref must not be empty")
        for device in self._read_all():
            if device.provider is provider and device.provider_ref == ref:
                return device
        return None

    def remove(self, device_id: str) -> DeviceRecord:
        """Delete a device from the registry."""

        def mutator(document: dict[str, object]) -> DeviceRecord:
            cleaned = device_id.strip()
            devices = self._parse(document)
            kept: list[DeviceRecord] = []
            removed: DeviceRecord | None = None
            for device in devices:
                if device.id == cleaned:
                    removed = device
                else:
                    kept.append(device)
            if removed is None:
                raise DeviceNotFoundError(f"device not found: {cleaned}")
            document.clear()
            document.update(self._dump(kept))
            return removed

        return self._store.update(mutator)

    def set_status(self, device_id: str, status: DeviceStatus) -> DeviceRecord:
        """Persist last known provider availability (not occupancy)."""
        if status is DeviceStatus.BUSY:
            raise ValueError("BUSY is session occupancy, not a persisted provider status")
        cleaned = device_id.strip()

        def mutator(document: dict[str, object]) -> DeviceRecord:
            devices = self._parse(document)
            found = next((item for item in devices if item.id == cleaned), None)
            if found is None:
                raise DeviceNotFoundError(f"device not found: {cleaned}")
            if found.last_status is status:
                return found
            updated = found.model_copy(update={"last_status": status})
            kept = [item for item in devices if item.id != cleaned]
            kept.append(updated)
            kept.sort(key=lambda item: item.registered_at)
            document.clear()
            document.update(self._dump(kept))
            return updated

        return self._store.update(mutator)

    def touch(self, device_id: str, when: datetime | None = None) -> DeviceRecord:
        """Update last_seen after a successful discover or action."""
        cleaned = device_id.strip()
        stamp = when or utcnow()

        def mutator(document: dict[str, object]) -> DeviceRecord:
            devices = self._parse(document)
            found = next((item for item in devices if item.id == cleaned), None)
            if found is None:
                raise DeviceNotFoundError(f"device not found: {cleaned}")
            updated = found.model_copy(update={"last_seen": stamp})
            kept = [item for item in devices if item.id != cleaned]
            kept.append(updated)
            kept.sort(key=lambda item: item.registered_at)
            document.clear()
            document.update(self._dump(kept))
            return updated

        return self._store.update(mutator)

    def ensure_stub_demo(self) -> DeviceRecord:
        """Guarantee a demo device exists so the stub path works offline."""
        try:
            existing = self.get("stub-demo")
        except DeviceNotFoundError:
            existing = None
        if (
            existing is not None
            and existing.provider is ProviderKind.STUB
            and existing.provider_ref == "stub-phone-1"
        ):
            return existing
        return self.register(
            device_id="stub-demo",
            provider=ProviderKind.STUB,
            provider_ref="stub-phone-1",
            display_name="Stub Demo Phone",
            tags=["demo", "stub", "android"],
            notes="Virtual device from StubCloudProvider. No hardware required.",
        )


def _reject_duplicate_ref(others: list[DeviceRecord], candidate: DeviceRecord) -> None:
    for item in others:
        if (
            item.provider is candidate.provider
            and item.provider_ref == candidate.provider_ref
        ):
            raise DuplicateDeviceError(
                f"{candidate.provider.value} handle {candidate.provider_ref} "
                f"is already registered as {item.id}"
            )
