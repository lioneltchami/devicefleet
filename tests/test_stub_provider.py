from __future__ import annotations

from devicefleet.models import CloudDeviceSpec
from devicefleet.providers.base import CloudDeviceProvider
from devicefleet.providers.stub import MAX_ACTIONS, MAX_TEXT, StubCloudProvider


def test_stub_satisfies_cloud_protocol() -> None:
    provider = StubCloudProvider()
    assert isinstance(provider, CloudDeviceProvider)


def test_screenshot_tap_and_dump() -> None:
    provider = StubCloudProvider()
    handle = "stub-phone-1"
    before = provider.screenshot(handle)
    assert before.startswith(b"\x89PNG\r\n\x1a\n")
    provider.tap(handle, 100, 200)
    provider.swipe(handle, 100, 200, 300, 400, duration_ms=120)
    provider.type_text(handle, "hi")
    provider.keyevent(handle, "BACK")
    xml = provider.dump_ui(handle)
    assert "<hierarchy" in xml
    assert "100,200" in "\n".join(provider.action_log(handle)) or "tap 100,200" in provider.action_log(
        handle
    )
    after = provider.screenshot(handle)
    assert after.startswith(b"\x89PNG\r\n\x1a\n")
    assert after != before  # tap mark changes pixels
    info = provider.describe(handle)
    assert info["last_tap"] == "300,400"


def test_state_survives_reload(tmp_path) -> None:
    path = tmp_path / "stub-state.yaml"
    first = StubCloudProvider(state_path=path)
    first.tap("stub-phone-1", 111, 222)
    second = StubCloudProvider(state_path=path)
    assert second.describe("stub-phone-1")["last_tap"] == "111,222"
    xml = second.dump_ui("stub-phone-1")
    assert "[111,222]" in xml


def test_action_log_and_text_are_capped() -> None:
    provider = StubCloudProvider()
    handle = "stub-phone-1"
    for index in range(MAX_ACTIONS + 50):
        provider.tap(handle, 1, 1)
    assert len(provider.action_log(handle)) == MAX_ACTIONS
    provider.type_text(handle, "x" * (MAX_TEXT + 80))
    assert len(provider.describe(handle)["last_tap"]) > 0
    phone = provider._require(handle)
    assert len(phone.last_text) == MAX_TEXT


def test_provision_and_release() -> None:
    provider = StubCloudProvider()
    extra = provider.provision(CloudDeviceSpec(model="Farm Pixel"))
    assert extra.display_name == "Farm Pixel"
    assert provider.health(extra.provider_ref)
    provider.release_cloud(extra.provider_ref)
    assert provider.health(extra.provider_ref) is False
