"""Command-line interface for Devicefleet."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from devicefleet import __version__
from devicefleet.auth import validate_serve_bind
from devicefleet.config import Settings, load_settings
from devicefleet.doctor import run_doctor
from devicefleet.fleet import DeviceInUseError, Fleet, FleetError
from devicefleet.models import ActionName, ActionRequest, DeviceListItem, ProviderKind, SessionRecord
from devicefleet.registry import DeviceNotFoundError
from devicefleet.sessions import DeviceBusyError, SessionOwnershipError
from devicefleet.skilltext import load_skill_markdown
from devicefleet.store import YamlStore
from devicefleet.transport.http import HttpTransport
from devicefleet.transport.local import LocalTransport

app = typer.Typer(
    name="devicefleet",
    help="Multi-device + cloud phone control for agents.",
    no_args_is_help=True,
    add_completion=False,
)
devices_app = typer.Typer(help="Discover and register phones.")
session_app = typer.Typer(help="Create, attach, list, and release sessions.")
run_app = typer.Typer(help="Execute helpers against an active session.")
app.add_typer(devices_app, name="devices")
app.add_typer(session_app, name="session")
app.add_typer(run_app, name="run")

console = Console()


def _settings(home: Path | None) -> Settings:
    return load_settings(home)


def _fleet(ctx: typer.Context) -> Fleet:
    fleet = ctx.obj.get("fleet")
    if fleet is None:
        fleet = Fleet(ctx.obj["settings"])
        ctx.obj["fleet"] = fleet
    return fleet


def _transport(ctx: typer.Context) -> LocalTransport | HttpTransport:
    existing = ctx.obj.get("transport")
    if existing is not None:
        return existing
    settings: Settings = ctx.obj["settings"]
    remote = ctx.obj.get("remote_url") or settings.remote_url
    agent = ctx.obj.get("agent") or settings.agent
    token = ctx.obj.get("token") or settings.token
    if remote:
        transport: LocalTransport | HttpTransport = HttpTransport(
            remote,
            token=token,
            agent_label=agent,
            artifacts_dir=settings.artifacts_dir,
            secret_store=YamlStore(settings.state_path),
            session_secret=settings.session_secret,
        )
    else:
        transport = LocalTransport(_fleet(ctx), agent_label=agent)
    ctx.obj["transport"] = transport
    return transport


def _emit(data: object, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(data, default=str))
    else:
        console.print(data)


def _version_flag(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    home: Annotated[
        Optional[Path],
        typer.Option("--home", help="Override DEVICEFLEET_HOME data directory."),
    ] = None,
    remote: Annotated[
        Optional[str],
        typer.Option("--remote", help="Talk to a fleet host URL instead of local state."),
    ] = None,
    token: Annotated[
        Optional[str],
        typer.Option("--token", help="Shared secret for a remote fleet host (DEVICEFLEET_TOKEN)."),
    ] = None,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", help="Agent label for session ownership (DEVICEFLEET_AGENT)."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the package version and exit.",
            callback=_version_flag,
            is_eager=True,
        ),
    ] = False,
) -> None:
    del version
    settings = _settings(home)
    ctx.obj = {
        "settings": settings,
        "remote_url": remote,
        "token": token or settings.token,
        "agent": (agent or settings.agent or "anonymous").strip() or "anonymous",
        "fleet": None,
    }


@app.command()
def doctor(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check Python, data dir, adb, and registered devices."""
    settings: Settings = ctx.obj["settings"]
    if ctx.obj.get("remote_url") or settings.remote_url:
        raise typer.BadParameter(
            "doctor inspects the local machine; --remote is not supported. "
            "Run `devicefleet doctor` on the fleet host instead."
        )
    report = run_doctor(_fleet(ctx))
    if as_json:
        console.print_json(report.model_dump_json())
        if not report.ok:
            raise typer.Exit(code=1)
        return
    console.print(f"[bold]devicefleet {report.version}[/bold] doctor")
    for check in report.checks:
        mark = "[green]ok[/green]" if check.ok else "[yellow]warn[/yellow]"
        console.print(f"  {mark}  {check.name}: {check.detail}")
    if report.devices:
        console.print("\n[bold]Registered[/bold]")
        for line in report.devices:
            console.print(f"  {line}", markup=False)
    if report.adb_serials:
        console.print("\n[bold]adb serials[/bold]")
        for serial in report.adb_serials:
            console.print(f"  {serial}", markup=False)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def skill() -> None:
    """Print SKILL.md so coding agents can load the workflow."""
    console.print(load_skill_markdown())


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[Optional[str], typer.Option("--host")] = None,
    port: Annotated[Optional[int], typer.Option("--port")] = None,
) -> None:
    """Run the HTTP fleet host for remote agents."""
    import uvicorn

    from devicefleet.api.app import create_app

    settings: Settings = ctx.obj["settings"]
    bind_host = host or settings.host
    bind_port = settings.port if port is None else port
    token = ctx.obj.get("token") or settings.token
    try:
        validate_serve_bind(bind_host, token)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if token:
        settings.token = token
    console.print(f"Serving devicefleet on http://{bind_host}:{bind_port}")
    uvicorn.run(create_app(_fleet(ctx)), host=bind_host, port=bind_port)


@devices_app.command("list")
def devices_list(
    ctx: typer.Context,
    tag: Annotated[Optional[list[str]], typer.Option("--tag", help="Filter (AND).")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List registered phones and which session holds them."""
    items = _transport(ctx).list_devices(tags=tag)
    if as_json:
        _emit([item.model_dump(mode="json") for item in items], True)
        return
    _print_devices(items)


@devices_app.command("discover")
def devices_discover(
    ctx: typer.Context,
    save: Annotated[
        bool,
        typer.Option("--save", help="Register newly seen devices."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ask ADB and the stub cloud provider which phones are visible."""
    found = _transport(ctx).discover(save=save)
    if as_json:
        _emit([item.model_dump(mode="json") for item in found], True)
        return
    if not found:
        console.print("No devices discovered. Attach a phone or use stub-demo.")
        return
    table = Table(title="Discovered")
    table.add_column("provider")
    table.add_column("ref")
    table.add_column("name")
    table.add_column("status")
    table.add_column("suggested id")
    for item in found:
        table.add_row(
            item.provider.value,
            item.provider_ref,
            item.display_name,
            item.status.value,
            item.suggested_id or "",
        )
    console.print(table)
    if save:
        console.print("Saved matching devices into the registry.")


@devices_app.command("register")
def devices_register(
    ctx: typer.Context,
    device_id: Annotated[str, typer.Argument()],
    provider: Annotated[str, typer.Option("--provider")] = "adb",
    ref: Annotated[str, typer.Option("--ref", help="Serial or cloud handle.")] = "",
    name: Annotated[Optional[str], typer.Option("--name")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
) -> None:
    """Manually add a phone to the registry."""
    if not ref.strip():
        raise typer.BadParameter("--ref (adb serial or cloud handle) is required")
    try:
        kind = ProviderKind(provider)
    except ValueError as exc:
        raise typer.BadParameter("provider must be adb, stub, or cloud") from exc
    try:
        record = _transport(ctx).register_device(
            device_id=device_id,
            provider=kind,
            provider_ref=ref.strip(),
            display_name=name,
            tags=tag,
        )
    except (FleetError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"registered {record.id} ({record.provider.value}:{record.provider_ref})")


@devices_app.command("rm")
def devices_rm(
    ctx: typer.Context,
    device_id: Annotated[str, typer.Argument()],
) -> None:
    """Remove a phone from the registry."""
    try:
        removed = _transport(ctx).remove_device(device_id)
    except (DeviceInUseError, DeviceNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"removed {removed.id}")


@session_app.command("start")
def session_start(
    ctx: typer.Context,
    device: Annotated[Optional[str], typer.Option("--device", "-d")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    agent: Annotated[Optional[str], typer.Option("--agent")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Lease an idle device. Other agents cannot take it until you stop."""
    label = _use_agent(ctx, agent)
    try:
        session = _transport(ctx).start_session(
            device_id=device, tags=tag or None, agent_label=label
        )
    except (DeviceNotFoundError, DeviceBusyError, FleetError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _emit(session.model_dump(mode="json"), True)
        return
    console.print(
        f"session [bold]{session.id}[/bold] on [bold]{session.device_id}[/bold] "
        f"(agent={session.agent_label})"
    )


@session_app.command("attach")
def session_attach(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument()],
    agent: Annotated[Optional[str], typer.Option("--agent")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Rejoin an existing active session you already own."""
    label = _use_agent(ctx, agent)
    try:
        session = _transport(ctx).attach_session(session_id, agent_label=label)
    except SessionOwnershipError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _emit(session.model_dump(mode="json"), True)
        return
    console.print(f"attached {session.id} on {session.device_id}")


@session_app.command("list")
def session_list(
    ctx: typer.Context,
    active: Annotated[bool, typer.Option("--active")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show sessions."""
    sessions = _transport(ctx).list_sessions(active_only=active)
    if as_json:
        _emit([item.public_dump() for item in sessions], True)
        return
    _print_sessions(sessions)


@session_app.command("stop")
def session_stop(
    ctx: typer.Context,
    session_id: Annotated[Optional[str], typer.Argument()] = None,
    session_opt: Annotated[
        Optional[str],
        typer.Option(
            "--session",
            "-s",
            help="Session id (same as the positional argument; other commands use this flag).",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Release a session so the device is free again."""
    transport = _transport(ctx)
    resolved = session_id or session_opt
    if not resolved:
        if isinstance(transport, HttpTransport):
            raise typer.BadParameter(
                "pass the session id as a positional argument "
                "(`session stop ses_…`) or `--session`"
            )
        resolved = transport.current_session_id()
    if not resolved:
        raise typer.BadParameter("session id required (or start a session first as this agent)")
    try:
        session = transport.stop_session(resolved)
    except SessionOwnershipError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _emit(session.model_dump(mode="json"), True)
        return
    console.print(f"stopped {session.id} ({session.device_id})")


def _use_agent(ctx: typer.Context, agent: str | None) -> str:
    label = (agent or ctx.obj.get("agent") or "anonymous").strip() or "anonymous"
    if ctx.obj.get("agent") != label:
        ctx.obj["agent"] = label
        ctx.obj["transport"] = None
    return label


def _resolve_session(ctx: typer.Context, session_id: str | None) -> str:
    transport = _transport(ctx)
    if session_id:
        return session_id
    if isinstance(transport, HttpTransport):
        raise typer.BadParameter("pass --session when talking to a remote host")
    return transport.fleet.resolve_session_id(None, agent_label=ctx.obj.get("agent"))


def _run_helper(ctx: typer.Context, request: ActionRequest, session: str | None, as_json: bool) -> None:
    session_id = _resolve_session(ctx, session)
    try:
        result = _transport(ctx).run(session_id, request)
    except SessionOwnershipError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _emit(result.model_dump(mode="json"), True)
        return
    console.print(f"{result.action.value}: {result.message or 'ok'}")
    if result.artifact_path:
        console.print(f"  artifact: {result.artifact_path}")
    if result.payload and request.name in {ActionName.INFO, ActionName.DUMP_UI}:
        interesting = {
            key: value
            for key, value in result.payload.items()
            if key != "xml"
        }
        console.print(interesting)
        xml = result.payload.get("xml")
        if isinstance(xml, str) and request.name is ActionName.DUMP_UI:
            preview = xml if len(xml) < 4000 else xml[:4000] + "\n... (truncated)"
            console.print(preview)


@run_app.callback(invoke_without_command=False)
def run_root() -> None:
    """Execute helpers against an active session."""


@run_app.command("screenshot")
def run_screenshot(
    ctx: typer.Context,
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(ctx, ActionRequest(name=ActionName.SCREENSHOT), session, as_json)


@run_app.command("tap")
def run_tap(
    ctx: typer.Context,
    x: Annotated[int, typer.Argument()],
    y: Annotated[int, typer.Argument()],
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(ctx, ActionRequest(name=ActionName.TAP, x=x, y=y), session, as_json)


@run_app.command("swipe")
def run_swipe(
    ctx: typer.Context,
    x1: Annotated[int, typer.Argument()],
    y1: Annotated[int, typer.Argument()],
    x2: Annotated[int, typer.Argument()],
    y2: Annotated[int, typer.Argument()],
    duration: Annotated[int, typer.Option("--duration")] = 300,
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(
        ctx,
        ActionRequest(
            name=ActionName.SWIPE, x=x1, y=y1, x2=x2, y2=y2, duration_ms=duration
        ),
        session,
        as_json,
    )


@run_app.command("type")
def run_type(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument()],
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(ctx, ActionRequest(name=ActionName.TYPE, text=text), session, as_json)


@run_app.command("key")
def run_key(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument()],
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(ctx, ActionRequest(name=ActionName.KEY, key=key), session, as_json)


@run_app.command("dump-ui")
def run_dump_ui(
    ctx: typer.Context,
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(ctx, ActionRequest(name=ActionName.DUMP_UI), session, as_json)


@run_app.command("info")
def run_info(
    ctx: typer.Context,
    session: Annotated[Optional[str], typer.Option("--session", "-s")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _run_helper(ctx, ActionRequest(name=ActionName.INFO), session, as_json)


def _print_devices(items: list[DeviceListItem]) -> None:
    table = Table(title="Fleet devices")
    table.add_column("id")
    table.add_column("provider")
    table.add_column("ref")
    table.add_column("tags")
    table.add_column("status")
    table.add_column("session")
    for item in items:
        table.add_row(
            item.device.id,
            item.device.provider.value,
            item.device.provider_ref,
            ",".join(item.device.tags),
            item.status.value,
            item.session_id or "-",
        )
    console.print(table)


def _print_sessions(sessions: list[SessionRecord]) -> None:
    table = Table(title="Sessions")
    table.add_column("id")
    table.add_column("device")
    table.add_column("agent")
    table.add_column("status")
    table.add_column("created")
    for session in sessions:
        table.add_row(
            session.id,
            session.device_id,
            session.agent_label,
            session.status.value,
            session.created_at.isoformat(timespec="seconds"),
        )
    console.print(table)


def run() -> None:
    app()
