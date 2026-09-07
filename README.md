# Devicefleet

**Devicefleet** is an agent-facing phone control platform built for **scale: many devices, exclusive sessions, and pluggable cloud backends**.

A coding agent (Cursor, Claude, Codex, or your own runner) should be able to grab an idle phone from a fleet, drive it with screenshots and input helpers, and release it so the next agent can take a different device. That is a different problem from controlling a single USB handset on one laptop.

This project is original software under the MIT license. It is **not** a fork of any existing phone-harness toolkit and does not reuse their source, docs, or branding.

## Fleet vs a single phone

| Single-phone tool | Devicefleet |
| --- | --- |
| Assumes one attached device | Registry of many local and remote phones |
| Implicit “whoever runs the command owns the phone” | Explicit **sessions**: create, attach, release |
| USB / emulator only | `DeviceProvider` for ADB **plus** `CloudDeviceProvider` for hosted farms |
| Local process only | CLI *or* HTTP host so remote agents share one lab |

The first working hardware backend is **local Android via `adb`**. The first working cloud backend is **`StubCloudProvider`**, an in-memory phone that needs no hardware and no paid credentials. Real farms (BrowserStack, AWS Device Farm, a private lab) plug in by implementing the same protocol.

iPhone control (macOS mirroring / pyobjc) is **out of scope for v0** and listed as future work.

## Install

Python 3.11+ is required.

```bash
git clone https://github.com/lioneltchami/devicefleet.git
cd devicefleet
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
devicefleet --help
devicefleet doctor
```

Optional, for real Android hardware or an emulator:

1. Install [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) so `adb` is on your `PATH`.
2. Enable USB debugging (or start an emulator).
3. Run `devicefleet doctor` and `devicefleet devices discover --save`.

You do **not** need `adb` or a cloud account to exercise the stub path.

## Quick start (no phone)

```bash
devicefleet doctor
devicefleet devices list
devicefleet session start --device stub-demo --agent my-agent
devicefleet run screenshot
devicefleet run tap 540 960
devicefleet run type "hello"
devicefleet run key BACK
devicefleet run dump-ui
devicefleet session stop
```

`doctor` reports whether `adb` is installed and lists attached serials. If `adb` is missing it explains that, and still tells you the stub provider is available.

## Sessions

A **session** is an exclusive lease on one registered device.

- `devicefleet session start --device <id>` — fail if that phone is already leased
- `devicefleet session start --tag lab --tag android` — first idle device matching all tags
- `devicefleet session attach <id>` — rejoin a live lease
- `devicefleet session list` / `session stop`

Two agents can run at once as long as they hold **different** devices. That is the whole point of the session model: no colliding taps on the same screen.

Locally, the CLI remembers **this agent's** current session under `$DEVICEFLEET_HOME` (default `~/.devicefleet`), keyed by `--agent` / `DEVICEFLEET_AGENT`. Agent A cannot silently drive agent B's device. `DEVICEFLEET_CURRENT_SESSION` overrides the remembered id for the current process only. On a remote host, always pass `--session`.

## Registry

Devices are stored as YAML (`devices.yaml`) in the data directory.

```bash
devicefleet devices discover              # adb + stub
devicefleet devices discover --save       # persist newly seen phones
devicefleet devices register pixel-lab \
  --provider adb --ref R58M30XXXX \
  --name "Pixel 7" --tag lab --tag pixel
devicefleet devices list --tag lab
```

Pick devices by **id** or **tags**. Tags are an AND match.

## Cloud provider interface

Hosted phones implement `CloudDeviceProvider` in `devicefleet.providers.base`:

- everything a local backend can do: discover, screenshot, tap, swipe, type, key, dump UI
- plus lifecycle: `provision(spec)`, `release_cloud(handle)`, `health(handle)`

`StubCloudProvider` is the reference implementation. To add BrowserStack, AWS Device Farm, or a custom lab:

1. Subclass `DeviceProvider` (or structurally satisfy `CloudDeviceProvider`).
2. Map their session/device id onto `DeviceRecord.provider_ref`.
3. Register the backend in `Fleet.provider_for`.
4. Keep paid credentials in environment variables — never in the registry file.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and extension points.

## HTTP host

Remote agents should not each spawn their own ADB daemon against the same USB bus. Run one fleet host and point clients at it.

`devicefleet serve` binds **127.0.0.1** by default. Binding `0.0.0.0` (or any non-loopback address) requires an explicit `--host` **and** `DEVICEFLEET_TOKEN`. Clients send the same secret as `Authorization: Bearer …` or `X-Devicefleet-Token`. Attach/stop/action also require a matching `X-Devicefleet-Agent` so a listed session id cannot steal another agent's lease.

```bash
# local lab (loopback, token optional)
devicefleet serve --port 8765

# reachable on the network (token required)
export DEVICEFLEET_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
devicefleet serve --host 0.0.0.0 --port 8765

# elsewhere
export DEVICEFLEET_TOKEN=...   # same secret
devicefleet --remote http://lab:8765 --agent coder session start --tag android
devicefleet --remote http://lab:8765 --agent coder run screenshot --session ses_…
```

Useful routes: `GET /health` (open), `GET /devices`, `POST /devices`, `DELETE /devices/{id}`, `POST /sessions`, `POST /sessions/{id}/actions`, `DELETE /sessions/{id}`. Open `/docs` for the generated OpenAPI UI.

## Agent skill

```bash
devicefleet skill
```

Prints [SKILL.md](SKILL.md). Drop that file into an agent skill slot, or tell the agent to run `devicefleet skill` and follow the workflow.

## Configuration

| Variable | Meaning |
| --- | --- |
| `DEVICEFLEET_HOME` | Data directory (registry, sessions, screenshots) |
| `DEVICEFLEET_REMOTE_URL` | Default fleet host for the CLI |
| `DEVICEFLEET_TOKEN` | Shared secret for the HTTP host and `--remote` clients |
| `DEVICEFLEET_AGENT` | Agent label used for session ownership and the local current-session map |
| `DEVICEFLEET_CURRENT_SESSION` | Process-local session id override (does not change other agents) |
| `DEVICEFLEET_ADB_BIN` | `adb` executable (default `adb`) |
| `DEVICEFLEET_HOST` / `DEVICEFLEET_PORT` | Bind address for `serve` (default `127.0.0.1:8765`) |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Non-goals (v0)

- No iOS / macOS iPhone Mirroring path yet
- No vendor lock-in or copy of third-party harness source
- No requirement for paid cloud credentials on the default stub path

## License

MIT. Copyright (c) 2026 lioneltchami.
