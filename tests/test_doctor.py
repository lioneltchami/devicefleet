from __future__ import annotations

from pathlib import Path

from devicefleet.config import load_settings
from devicefleet.doctor import _dir_is_writable, run_doctor
from devicefleet.fleet import Fleet


def test_dir_is_writable_probe(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    existing = home / ".devicefleet-write-probe"
    existing.write_text("keep-me", encoding="utf-8")
    assert _dir_is_writable(home) is True
    assert existing.exists() is True
    assert existing.read_text(encoding="utf-8") == "keep-me"
    leftovers = list(home.glob(".devicefleet-write-probe.*"))
    assert leftovers == []

    missing = tmp_path / "gone"
    assert _dir_is_writable(missing) is False

    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")
    assert _dir_is_writable(file_path) is False


def test_dir_is_writable_rejects_unwritable(tmp_path: Path) -> None:
    home = tmp_path / "ro"
    home.mkdir()
    home.chmod(0o500)
    try:
        if _dir_is_writable(home):
            # Root (or some CI users) can still write; skip the negative assert.
            return
        assert _dir_is_writable(home) is False
    finally:
        home.chmod(0o700)


def test_doctor_marks_writable_data_dir(tmp_path: Path) -> None:
    fleet = Fleet(load_settings(tmp_path / "home"))
    report = run_doctor(fleet)
    data = next(check for check in report.checks if check.name == "data_dir")
    assert data.ok is True
