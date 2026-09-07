"""Provider contracts. Cloud farms plug in by implementing CloudDeviceProvider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from devicefleet.models import CloudDeviceSpec, DiscoveredDevice


class DeviceProvider(ABC):
    """Backend that can discover phones and run input/screenshot helpers.

    Local ADB and hosted farms share this surface so the fleet host does not
    care where the pixels come from.
    """

    provider_id: str

    @abstractmethod
    def discover(self) -> list[DiscoveredDevice]:
        """List phones currently visible to this backend."""

    @abstractmethod
    def screenshot(self, handle: str) -> bytes:
        """Return PNG bytes for the current frame."""

    @abstractmethod
    def tap(self, handle: str, x: int, y: int) -> None:
        """Tap at display coordinates."""

    @abstractmethod
    def swipe(
        self,
        handle: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> None:
        """Swipe from (x1, y1) to (x2, y2)."""

    @abstractmethod
    def type_text(self, handle: str, text: str) -> None:
        """Type a short ASCII string into the focused field."""

    @abstractmethod
    def keyevent(self, handle: str, key: str) -> None:
        """Send a named or numeric key event (BACK, HOME, ENTER, ...)."""

    @abstractmethod
    def dump_ui(self, handle: str) -> str:
        """Return an accessibility / UI hierarchy dump when the OS supports it."""

    def describe(self, handle: str) -> dict[str, str]:
        """Optional extra facts (model, size). Override when cheap to query."""
        return {"handle": handle, "provider": self.provider_id}


@runtime_checkable
class CloudDeviceProvider(Protocol):
    """Hosted-phone farms implement this protocol.

    A production adapter (BrowserStack, AWS Device Farm, a private lab) should
    implement DeviceProvider methods plus the lifecycle hooks below. Devicefleet
    ships StubCloudProvider as the reference that needs no paid credentials.

    Expected methods
    ----------------
    provider_id: str
    discover() -> list[DiscoveredDevice]
    screenshot / tap / swipe / type_text / keyevent / dump_ui
    provision(spec: CloudDeviceSpec) -> DiscoveredDevice
    release_cloud(handle: str) -> None
    health(handle: str) -> bool
    """

    provider_id: str

    def discover(self) -> list[DiscoveredDevice]: ...

    def screenshot(self, handle: str) -> bytes: ...

    def tap(self, handle: str, x: int, y: int) -> None: ...

    def swipe(
        self,
        handle: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> None: ...

    def type_text(self, handle: str, text: str) -> None: ...

    def keyevent(self, handle: str, key: str) -> None: ...

    def dump_ui(self, handle: str) -> str: ...

    def provision(self, spec: CloudDeviceSpec) -> DiscoveredDevice: ...

    def release_cloud(self, handle: str) -> None: ...

    def health(self, handle: str) -> bool: ...


class ProviderError(RuntimeError):
    """A backend could not complete an action."""


class ProviderUnavailableError(ProviderError):
    """The backend binary or service is missing."""
