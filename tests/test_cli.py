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


def test_skill_mentions_sessions() -> None:
    result = runner.invoke(app, ["skill"])
    assert "session start" in result.stdout
