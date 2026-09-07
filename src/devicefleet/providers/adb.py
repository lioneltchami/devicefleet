"""Local Android devices via the Android Debug Bridge."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from devicefleet.models import DeviceStatus, DiscoveredDevice, ProviderKind
from devicefleet.providers.base import DeviceProvider, ProviderError, ProviderUnavailableError

# Common Android keyevent names -> keycodes.
KEY_ALIASES: dict[str, str] = {
    "BACK": "4",
    "HOME": "3",
    "ENTER": "66",
    "DEL": "67",
    "DELETE": "67",
    "TAB": "61",
    "SPACE": "62",
    "ESCAPE": "111",
    "POWER": "26",
    "MENU": "82",
    "APP_SWITCH": "187",
    "RECENTS": "187",
    "VOLUME_UP": "24",
    "VOLUME_DOWN": "25",
    "DPAD_UP": "19",
    "DPAD_DOWN": "20",
    "DPAD_LEFT": "21",
    "DPAD_RIGHT": "22",
    "DPAD_CENTER": "23",
}


class LocalAdbProvider(DeviceProvider):
    """Talk to USB/emulator Android phones with `adb`.

    This is the first working hardware backend. Cloud adapters should implement
    the same DeviceProvider methods so session actions stay identical.
    """

    provider_id = "adb"

    def __init__(self, adb_bin: str = "adb", timeout_s: float = 30.0) -> None:
        if not adb_bin.strip():
            raise ValueError("adb_bin is required")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.adb_bin = adb_bin
        self.timeout_s = timeout_s

    def available(self) -> bool:
        """True when the adb executable is on PATH or at the configured path."""
        return shutil.which(self.adb_bin) is not None or Path(self.adb_bin).is_file()

    def discover(self) -> list[DiscoveredDevice]:
        if not self.available():
            raise ProviderUnavailableError(
                f"adb not found ({self.adb_bin}). Install Android platform-tools."
            )
        result = self._run(["devices", "-l"], serial=None)
        if result.returncode != 0:
            raise ProviderError(self._stderr(result) or "adb devices failed")
        return _parse_adb_devices(result.stdout.decode("utf-8", errors="replace"))

    def screenshot(self, handle: str) -> bytes:
        result = self._run(["exec-out", "screencap", "-p"], serial=handle)
        if result.returncode != 0 or not result.stdout:
            raise ProviderError(
                self._stderr(result) or f"screencap failed on {handle}"
            )
        if result.stdout[:8] != b"\x89PNG\r\n\x1a\n":
            raise ProviderError("screencap did not return a PNG")
        return result.stdout

    def tap(self, handle: str, x: int, y: int) -> None:
        _require_point(x, y)
        self._checked(["shell", "input", "tap", str(x), str(y)], handle)

    def swipe(
        self,
        handle: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> None:
        _require_point(x1, y1)
        _require_point(x2, y2)
        if duration_ms < 1:
            raise ValueError("duration_ms must be >= 1")
        self._checked(
            [
                "shell",
                "input",
                "swipe",
                str(x1),
                str(y1),
                str(x2),
                str(y2),
                str(duration_ms),
            ],
            handle,
        )

    def type_text(self, handle: str, text: str) -> None:
        if text is None:
            raise ValueError("text is required")
        escaped = _adb_input_escape(text)
        if not escaped:
            return
        self._checked(["shell", "input", "text", escaped], handle)

    def keyevent(self, handle: str, key: str) -> None:
        if not key or not str(key).strip():
            raise ValueError("key is required")
        code = _resolve_key(str(key))
        self._checked(["shell", "input", "keyevent", code], handle)

    def dump_ui(self, handle: str) -> str:
        """Best-effort uiautomator hierarchy dump."""
        direct = self._run(
            ["exec-out", "uiautomator", "dump", "/dev/tty"],
            serial=handle,
        )
        text = direct.stdout.decode("utf-8", errors="replace")
        if direct.returncode == 0 and "<hierarchy" in text:
            return text
        remote = "/sdcard/window_dump.xml"
        dump = self._run(["shell", "uiautomator", "dump", remote], serial=handle)
        if dump.returncode != 0:
            raise ProviderError(
                self._stderr(dump)
                or self._stderr(direct)
                or "uiautomator dump is not available on this device"
            )
        cat = self._run(["exec-out", "cat", remote], serial=handle)
        xml = cat.stdout.decode("utf-8", errors="replace")
        if "<hierarchy" not in xml:
            raise ProviderError("uiautomator dump produced no hierarchy XML")
        return xml

    def describe(self, handle: str) -> dict[str, str]:
        props = {
            "handle": handle,
            "provider": self.provider_id,
        }
        for key, prop in (
            ("model", "ro.product.model"),
            ("manufacturer", "ro.product.manufacturer"),
            ("sdk", "ro.build.version.sdk"),
            ("release", "ro.build.version.release"),
        ):
            result = self._run(["shell", "getprop", prop], serial=handle)
            if result.returncode == 0:
                value = result.stdout.decode("utf-8", errors="replace").strip()
                if value:
                    props[key] = value
        return props

    def _run(
        self, args: list[str], serial: str | None
    ) -> subprocess.CompletedProcess[bytes]:
        command = [self.adb_bin]
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        try:
            return subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailableError(
                f"adb not found ({self.adb_bin}). Install Android platform-tools."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"adb timed out: {' '.join(command)}") from exc

    def _checked(self, args: list[str], serial: str) -> None:
        result = self._run(args, serial=serial)
        if result.returncode != 0:
            raise ProviderError(self._stderr(result) or f"adb command failed: {args}")

    @staticmethod
    def _stderr(result: subprocess.CompletedProcess[bytes]) -> str:
        return result.stderr.decode("utf-8", errors="replace").strip()


def _parse_adb_devices(output: str) -> list[DiscoveredDevice]:
    devices: list[DiscoveredDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state, *rest = parts
        extras = _parse_adb_extras(rest)
        model = extras.get("model", serial).replace("_", " ")
        status = DeviceStatus.ONLINE if state == "device" else DeviceStatus.OFFLINE
        tags = ["android", "adb"]
        if serial.startswith("emulator-"):
            tags.append("emulator")
        devices.append(
            DiscoveredDevice(
                provider=ProviderKind.ADB,
                provider_ref=serial,
                display_name=model,
                status=status,
                metadata={"adb_state": state, **extras},
                suggested_id=_suggest_id(serial, extras),
                suggested_tags=tags,
            )
        )
    return devices


def _parse_adb_extras(tokens: list[str]) -> dict[str, str]:
    extras: dict[str, str] = {}
    for token in tokens:
        if ":" not in token:
            continue
        key, value = token.split(":", 1)
        extras[key] = value
    return extras


def _suggest_id(serial: str, extras: dict[str, str]) -> str:
    model = extras.get("model") or extras.get("device") or serial
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in model).strip("-")
    return slug or serial


def _require_point(x: int, y: int) -> None:
    if not isinstance(x, int) or not isinstance(y, int):
        raise ValueError("coordinates must be integers")
    if x < 0 or y < 0:
        raise ValueError("coordinates must be >= 0")


def _resolve_key(key: str) -> str:
    token = key.strip()
    if token.isdigit():
        return token
    alias = KEY_ALIASES.get(token.upper())
    if alias:
        return alias
    # Allow KEYCODE_* names that adb already understands.
    return token.upper()


def _adb_input_escape(text: str) -> str:
    """Escape a string for `adb shell input text`.

    The input text helper is ASCII-oriented: spaces become %s and a small set
    of shell metacharacters is percent-encoded. Rich IME input is out of scope.
    """
    pieces: list[str] = []
    for char in text:
        if char == " ":
            pieces.append("%s")
        elif char == "\n":
            pieces.append("%n")
        elif char in r'&|<>()$?!"\'\\;':
            pieces.append(f"%{ord(char):02x}")
        elif ord(char) < 32 or ord(char) > 126:
            pieces.append(f"%{ord(char):02x}")
        else:
            pieces.append(char)
    return "".join(pieces)
