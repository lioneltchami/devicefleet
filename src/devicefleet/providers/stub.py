"""Hosted phone simulator for demos and tests. No credentials required."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from devicefleet.models import CloudDeviceSpec, DeviceStatus, DiscoveredDevice, ProviderKind
from devicefleet.pngutil import solid_png
from devicefleet.providers.base import CloudDeviceProvider, DeviceProvider, ProviderError
from devicefleet.store import YamlStore

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_HANDLE = "stub-phone-1"
MAX_ACTIONS = 200
MAX_TEXT = 4096


@dataclass
class _VirtualPhone:
    handle: str
    display_name: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    last_tap: tuple[int, int] | None = None
    last_text: str = ""
    last_key: str = ""
    released: bool = False
    actions: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_ACTIONS))
    focused: str = "home"

    def log(self, message: str) -> None:
        self.actions.append(message)


def _default_phones() -> dict[str, _VirtualPhone]:
    return {
        DEFAULT_HANDLE: _VirtualPhone(
            handle=DEFAULT_HANDLE,
            display_name="Stub Demo Phone",
        )
    }


class StubCloudProvider(DeviceProvider):
    """Reference CloudDeviceProvider: a fake Android that records actions.

    Use this when you have no USB phone and no paid device-farm account.
    Production farms should copy the method signatures, not this
    implementation. State is optionally persisted so CLI processes share one
    virtual phone.
    """

    provider_id = "stub"

    def __init__(self, state_path: Path | None = None) -> None:
        self._store = YamlStore(state_path) if state_path is not None else None
        self._phones: dict[str, _VirtualPhone] = _load_phones(self._store)

    def discover(self) -> list[DiscoveredDevice]:
        return [
            self._as_discovered(phone)
            for phone in self._phones.values()
            if not phone.released
        ]

    def screenshot(self, handle: str) -> bytes:
        phone = self._require(handle)
        phone.log("screenshot")
        self._persist()
        return solid_png(
            phone.width,
            phone.height,
            color=(28, 32, 40),
            mark=phone.last_tap,
        )

    def tap(self, handle: str, x: int, y: int) -> None:
        phone = self._require(handle)
        self._in_bounds(phone, x, y)
        phone.last_tap = (x, y)
        phone.log(f"tap {x},{y}")
        self._persist()

    def swipe(
        self,
        handle: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> None:
        phone = self._require(handle)
        self._in_bounds(phone, x1, y1)
        self._in_bounds(phone, x2, y2)
        if duration_ms < 1:
            raise ValueError("duration_ms must be >= 1")
        phone.last_tap = (x2, y2)
        phone.log(f"swipe {x1},{y1}->{x2},{y2} {duration_ms}ms")
        self._persist()

    def type_text(self, handle: str, text: str) -> None:
        if text is None:
            raise ValueError("text is required")
        phone = self._require(handle)
        phone.last_text = (phone.last_text + text)[-MAX_TEXT:]
        phone.log(f"type {text!r}")
        self._persist()

    def keyevent(self, handle: str, key: str) -> None:
        if not key or not str(key).strip():
            raise ValueError("key is required")
        phone = self._require(handle)
        phone.last_key = str(key).strip().upper()
        if phone.last_key == "HOME":
            phone.focused = "home"
        elif phone.last_key == "BACK":
            phone.focused = "previous"
        phone.log(f"key {phone.last_key}")
        self._persist()

    def dump_ui(self, handle: str) -> str:
        phone = self._require(handle)
        phone.log("dump_ui")
        self._persist()
        tap = phone.last_tap or (0, 0)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<hierarchy rotation="0" focused="{phone.focused}">\n'
            f'  <node class="android.widget.FrameLayout" bounds="[0,0][{phone.width},{phone.height}]" text="">\n'
            f'    <node class="android.widget.TextView" text="Devicefleet stub" bounds="[80,120][1000,220]"/>\n'
            f'    <node class="android.widget.Button" text="Continue" bounds="[300,1600][780,1740]"/>\n'
            f'    <node class="stub.Cursor" text="last_tap" bounds="[{tap[0]},{tap[1]}][{tap[0]+1},{tap[1]+1}]"/>\n'
            "  </node>\n"
            "</hierarchy>\n"
        )

    def describe(self, handle: str) -> dict[str, str]:
        phone = self._require(handle)
        return {
            "handle": phone.handle,
            "provider": self.provider_id,
            "model": "Devicefleet Stub Phone",
            "manufacturer": "devicefleet",
            "width": str(phone.width),
            "height": str(phone.height),
            "last_tap": "" if phone.last_tap is None else f"{phone.last_tap[0]},{phone.last_tap[1]}",
            "actions": str(len(phone.actions)),
        }

    def provision(self, spec: CloudDeviceSpec) -> DiscoveredDevice:
        """Allocate another virtual phone. Used to demo cloud acquire."""
        if spec.platform != "android":
            raise ProviderError("StubCloudProvider only simulates Android")
        handle = self._next_handle()
        name = spec.model or f"Stub Cloud Phone {handle}"
        phone = _VirtualPhone(handle=handle, display_name=name)
        self._phones[handle] = phone
        phone.log(f"provisioned platform={spec.platform}")
        self._persist()
        return self._as_discovered(phone, tags=list(spec.tags))

    def ensure(self, handle: str, display_name: str | None = None) -> None:
        """Rehydrate a registry stub so leftover YAML devices stay usable."""
        if not handle or not handle.strip():
            raise ValueError("device handle is required")
        phone = self._phones.get(handle)
        if phone is None:
            self._phones[handle] = _VirtualPhone(
                handle=handle,
                display_name=display_name or handle,
            )
            self._persist()
            return
        if phone.released:
            phone.released = False
            phone.log("rehydrated")
            if display_name:
                phone.display_name = display_name
            self._persist()

    def _next_handle(self) -> str:
        index = 1
        while True:
            handle = f"stub-phone-{index}"
            if handle not in self._phones:
                return handle
            index += 1

    def release_cloud(self, handle: str) -> None:
        phone = self._require(handle)
        phone.released = True
        phone.log("released")
        self._persist()

    def health(self, handle: str) -> bool:
        phone = self._phones.get(handle)
        return phone is not None and not phone.released

    def action_log(self, handle: str) -> list[str]:
        """Test helper: recorded actions on a virtual phone."""
        return list(self._require(handle).actions)

    def _require(self, handle: str) -> _VirtualPhone:
        if not handle or not handle.strip():
            raise ValueError("device handle is required")
        phone = self._phones.get(handle)
        if phone is None or phone.released:
            raise ProviderError(f"stub device not available: {handle}")
        return phone

    @staticmethod
    def _in_bounds(phone: _VirtualPhone, x: int, y: int) -> None:
        if not isinstance(x, int) or not isinstance(y, int):
            raise ValueError("coordinates must be integers")
        if x < 0 or y < 0 or x >= phone.width or y >= phone.height:
            raise ValueError(
                f"point ({x},{y}) is outside {phone.width}x{phone.height}"
            )

    def _persist(self) -> None:
        if self._store is None:
            return
        payload: dict[str, object] = {
            "phones": [_phone_to_dict(phone) for phone in self._phones.values()]
        }
        self._store.save(payload)

    @staticmethod
    def _as_discovered(
        phone: _VirtualPhone, tags: list[str] | None = None
    ) -> DiscoveredDevice:
        return DiscoveredDevice(
            provider=ProviderKind.STUB,
            provider_ref=phone.handle,
            display_name=phone.display_name,
            status=DeviceStatus.ONLINE,
            metadata={
                "width": str(phone.width),
                "height": str(phone.height),
                "kind": "stub-cloud",
            },
            suggested_id="stub-demo" if phone.handle == DEFAULT_HANDLE else phone.handle,
            suggested_tags=tags or ["demo", "stub", "android"],
        )


def _phone_to_dict(phone: _VirtualPhone) -> dict[str, object]:
    return {
        "handle": phone.handle,
        "display_name": phone.display_name,
        "width": phone.width,
        "height": phone.height,
        "last_tap": list(phone.last_tap) if phone.last_tap else None,
        "last_text": phone.last_text,
        "last_key": phone.last_key,
        "released": phone.released,
        "actions": list(phone.actions),
        "focused": phone.focused,
    }


def _phone_from_dict(raw: dict[str, object]) -> _VirtualPhone:
    tap_raw = raw.get("last_tap")
    last_tap: tuple[int, int] | None = None
    if isinstance(tap_raw, list) and len(tap_raw) == 2:
        last_tap = (int(tap_raw[0]), int(tap_raw[1]))
    actions_raw = raw.get("actions") or []
    action_items = [str(item) for item in actions_raw] if isinstance(actions_raw, list) else []
    return _VirtualPhone(
        handle=str(raw.get("handle") or DEFAULT_HANDLE),
        display_name=str(raw.get("display_name") or "Stub Demo Phone"),
        width=int(raw.get("width") or DEFAULT_WIDTH),
        height=int(raw.get("height") or DEFAULT_HEIGHT),
        last_tap=last_tap,
        last_text=str(raw.get("last_text") or "")[-MAX_TEXT:],
        last_key=str(raw.get("last_key") or ""),
        released=bool(raw.get("released")),
        actions=deque(action_items[-MAX_ACTIONS:], maxlen=MAX_ACTIONS),
        focused=str(raw.get("focused") or "home"),
    )


def _load_phones(store: YamlStore | None) -> dict[str, _VirtualPhone]:
    if store is None:
        return _default_phones()
    document = store.load()
    raw_phones = document.get("phones")
    if not isinstance(raw_phones, list) or not raw_phones:
        return _default_phones()
    phones: dict[str, _VirtualPhone] = {}
    for item in raw_phones:
        if not isinstance(item, dict):
            continue
        phone = _phone_from_dict(item)
        phones[phone.handle] = phone
    return phones or _default_phones()


# Runtime check: the stub is a valid CloudDeviceProvider.
def _assert_protocol() -> None:
    provider: CloudDeviceProvider = StubCloudProvider()
    assert provider.provider_id == "stub"


_assert_protocol()
