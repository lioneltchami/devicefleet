from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from devicefleet.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "session" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_doctor_and_stub_flow(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    doctor = runner.invoke(app, ["--home", home, "doctor"])
    assert doctor.exit_code == 0
    assert "stub_provider" in doctor.stdout

    started = runner.invoke(
        app,
        ["--home", home, "session", "start", "--device", "stub-demo", "--json"],
    )
    assert started.exit_code == 0, started.stdout
    assert "ses_" in started.stdout

    shot = runner.invoke(app, ["--home", home, "run", "screenshot", "--json"])
    assert shot.exit_code == 0, shot.stdout
    assert "screenshot.png" in shot.stdout

    tap = runner.invoke(app, ["--home", home, "run", "tap", "10", "20"])
    assert tap.exit_code == 0, tap.stdout

    skill = runner.invoke(app, ["skill"])
    assert skill.exit_code == 0
    assert "devicefleet" in skill.stdout.lower()

    stopped = runner.invoke(app, ["--home", home, "session", "stop"])
    assert stopped.exit_code == 0, stopped.stdout


def test_devices_rm_refuses_active_session(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    started = runner.invoke(
        app, ["--home", home, "session", "start", "--device", "stub-demo"]
    )
    assert started.exit_code == 0, started.stdout
    removed = runner.invoke(app, ["--home", home, "devices", "rm", "stub-demo"])
    assert removed.exit_code != 0
    assert "held by session" in removed.output or "held by session" in (removed.stderr or "")


def test_devices_register_updates_registry(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    registered = runner.invoke(
        app,
        [
            "--home",
            home,
            "devices",
            "register",
            "lab-1",
            "--provider",
            "stub",
            "--ref",
            "stub-phone-4",
            "--name",
            "Lab",
        ],
    )
    assert registered.exit_code == 0, registered.stdout
    listed = runner.invoke(app, ["--home", home, "devices", "list", "--json"])
    assert listed.exit_code == 0
    assert "lab-1" in listed.stdout


def test_skill_mentions_sessions() -> None:
    result = runner.invoke(app, ["skill"])
    assert "session start" in result.stdout
    assert "DEVICEFLEET_CURRENT_SESSION" in result.stdout
    scrubbed = result.stdout.replace("DEVICEFLEET_CURRENT_SESSION", "").replace(
        "DEVICEFLEET_SESSION_SECRET", ""
    )
    assert "DEVICEFLEET_SESSION" not in scrubbed
    assert "downloads those files" in result.stdout


def test_remote_doctor_is_rejected(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    result = runner.invoke(
        app,
        ["--home", home, "--remote", "http://127.0.0.1:8765", "doctor"],
    )
    assert result.exit_code != 0
    output = (result.stdout or "") + (result.stderr or "") + (result.output or "")
    assert "not supported" in output or "fleet host" in output


def test_register_strips_ref_whitespace(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    registered = runner.invoke(
        app,
        [
            "--home",
            home,
            "devices",
            "register",
            "lab-pad",
            "--provider",
            "stub",
            "--ref",
            "  stub-phone-5  ",
            "--name",
            "Padded",
        ],
    )
    assert registered.exit_code == 0, registered.stdout
    listed = runner.invoke(app, ["--home", home, "devices", "list", "--json"])
    assert listed.exit_code == 0
    assert "stub-phone-5" in listed.stdout
    assert "  stub-phone-5  " not in listed.stdout
