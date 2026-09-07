from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from devicefleet.providers.adb import (
    LocalAdbProvider,
    _adb_input_escape,
    _parse_adb_devices,
    _resolve_key,
    type_segments,
)
from devicefleet.providers.base import ProviderError, ProviderUnavailableError


def test_parse_adb_devices_sample() -> None:
    output = """\
List of devices attached
R58M30ABC\tdevice usb:1-1 product:panther model:Pixel_7 device:panther transport_id:3
emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64
ABCD                   unauthorized usb:1-2
"""
    devices = _parse_adb_devices(output)
    assert len(devices) == 3
    pixel = devices[0]
    assert pixel.provider_ref == "R58M30ABC"
    assert pixel.display_name == "Pixel 7"
    assert pixel.suggested_id == "pixel-7-R58M30ABC"
    assert pixel.status.value == "online"
    assert devices[1].suggested_id == "sdk-gphone64-arm64-emulator-5554"
    assert devices[1].suggested_tags[-1] == "emulator" or "emulator" in devices[1].suggested_tags
    assert devices[2].status.value == "offline"
    other = _parse_adb_devices(
        "List of devices attached\nR58OTHER device model:Pixel_7\n"
    )
    assert other[0].suggested_id == "pixel-7-R58OTHER"
    assert other[0].suggested_id != pixel.suggested_id


def test_input_escape_and_keys() -> None:
    assert _adb_input_escape("hello world") == "hello%sworld"
    assert _adb_input_escape("`cmd`") == "%60cmd%60"
    assert _adb_input_escape("hello%s") == "hello%25s"
    assert _adb_input_escape("100%") == "100%25"
    with pytest.raises(ValueError, match="newline"):
        _adb_input_escape("hello\n")
    assert type_segments("a\nb") == [("text", "a"), ("key", "ENTER"), ("text", "b")]
    assert type_segments("line\n") == [("text", "line"), ("key", "ENTER")]
    assert _resolve_key("BACK") == "4"
    assert _resolve_key("66") == "66"


def test_available_requires_executable(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-adb"
    assert LocalAdbProvider(adb_bin=str(missing)).available() is False

    blob = tmp_path / "adb-blob"
    blob.write_text("#!/bin/sh\n", encoding="utf-8")
    blob.chmod(0o644)
    assert os.access(blob, os.X_OK) is False
    assert LocalAdbProvider(adb_bin=str(blob)).available() is False

    blob.chmod(0o755)
    assert LocalAdbProvider(adb_bin=str(blob)).available() is True


def test_run_maps_os_errors(tmp_path: Path) -> None:
    provider = LocalAdbProvider(adb_bin=str(tmp_path / "adb"))
    with patch("devicefleet.providers.adb.subprocess.run", side_effect=FileNotFoundError("gone")):
        with pytest.raises(ProviderUnavailableError):
            provider._run(["devices"], serial=None)
    with patch("devicefleet.providers.adb.subprocess.run", side_effect=PermissionError("denied")):
        with pytest.raises(ProviderError, match="permission denied"):
            provider._run(["devices"], serial=None)
    with patch("devicefleet.providers.adb.subprocess.run", side_effect=OSError("bad fd")):
        with pytest.raises(ProviderError, match="cannot execute adb"):
            provider._run(["devices"], serial=None)
