"""Shared fixtures: isolated DEVICEFLEET_HOME per test."""

from __future__ import annotations

from pathlib import Path

import pytest

from devicefleet.config import load_settings
from devicefleet.fleet import Fleet


@pytest.fixture
def fleet_home(tmp_path: Path) -> Path:
    home = tmp_path / "fleet"
    home.mkdir()
    return home


@pytest.fixture
def fleet(fleet_home: Path) -> Fleet:
    return Fleet(load_settings(fleet_home))
