from __future__ import annotations

from devicefleet.providers.adb import _adb_input_escape, _parse_adb_devices, _resolve_key


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
    assert pixel.status.value == "online"
    assert devices[1].suggested_tags[-1] == "emulator" or "emulator" in devices[1].suggested_tags
    assert devices[2].status.value == "offline"


def test_input_escape_and_keys() -> None:
    assert _adb_input_escape("hello world") == "hello%sworld"
    assert _resolve_key("BACK") == "4"
    assert _resolve_key("66") == "66"
