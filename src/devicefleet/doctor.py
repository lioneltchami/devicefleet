"""Environment checks for agents: adb, data dir, registered phones."""

from __future__ import annotations

import shutil
import sys
from pydantic import BaseModel, Field

from devicefleet import __version__
from devicefleet.fleet import Fleet
from devicefleet.providers.base import ProviderError


class Check(BaseModel):
    name: str
    ok: bool
    detail: str


class DoctorReport(BaseModel):
    version: str
    ok: bool
    checks: list[Check] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)
    adb_serials: list[str] = Field(default_factory=list)


def run_doctor(fleet: Fleet) -> DoctorReport:
    """Collect a structured report. Never raises for missing hardware."""
    checks: list[Check] = []
    adb_serials: list[str] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        Check(
            name="python",
            ok=py_ok,
            detail=f"{sys.version.split()[0]} (need 3.11+)",
        )
    )

    home = fleet.settings.home
    writable = home.exists() and home.is_dir()
    checks.append(
        Check(
            name="data_dir",
            ok=writable,
            detail=str(home),
        )
    )

    adb_path = shutil.which(fleet.settings.adb_bin)
    if adb_path:
        checks.append(Check(name="adb", ok=True, detail=adb_path))
        try:
            discovered = fleet.adb.discover()
            adb_serials = [item.provider_ref for item in discovered]
            online = [item for item in discovered if item.status.value == "online"]
            if online:
                detail = ", ".join(
                    f"{item.provider_ref} ({item.display_name})" for item in online
                )
            elif discovered:
                detail = "adb found devices but none are in the 'device' state"
            else:
                detail = "adb is installed; no USB/emulator phones attached"
            checks.append(Check(name="adb_devices", ok=True, detail=detail))
        except ProviderError as exc:
            checks.append(Check(name="adb_devices", ok=False, detail=str(exc)))
    else:
        checks.append(
            Check(
                name="adb",
                ok=False,
                detail=(
                    f"{fleet.settings.adb_bin} not on PATH. "
                    "Install Android platform-tools, or use the stub provider "
                    "(devicefleet session start --device stub-demo)."
                ),
            )
        )

    stub_ok = bool(fleet.stub.discover())
    checks.append(
        Check(
            name="stub_provider",
            ok=stub_ok,
            detail="StubCloudProvider is available (no hardware or paid account)",
        )
    )

    registered = fleet.registry.list_devices()
    devices = [f"{item.id} [{item.provider.value}] tags={','.join(item.tags)}" for item in registered]
    checks.append(
        Check(
            name="registry",
            ok=bool(registered),
            detail=f"{len(registered)} registered device(s)",
        )
    )

    ok = all(check.ok for check in checks if check.name in {"python", "data_dir", "stub_provider"})
    return DoctorReport(
        version=__version__,
        ok=ok,
        checks=checks,
        devices=devices,
        adb_serials=adb_serials,
    )
