# Devicefleet architecture

Devicefleet is a small Python package with four jobs:

1. Remember many phones (**registry**).
2. Hand out exclusive leases (**sessions**).
3. Run input/screenshot helpers on whatever backend owns the phone (**providers**).
4. Expose the same operations on a CLI and an HTTP host (**transport**).

```
 Agent CLI / HTTP client
          |
     FleetTransport
      /          \
 LocalTransport   HttpTransport
      |               |
    Fleet  <----- FastAPI host
      |
 +----+----+------------+
 |         |            |
Registry Sessions   Providers
 YAML      YAML      adb | stub | (your farm)
```

## Modules

| Module | Role |
| --- | --- |
| `devicefleet.config` | `DEVICEFLEET_*` settings and the data directory |
| `devicefleet.models` | Pydantic records shared by CLI, API, and tests |
| `devicefleet.store` | Atomic YAML read/write (unique temp per save + exclusive file lock) |
| `devicefleet.registry` | Multi-device catalog (id, tags, provider ref) |
| `devicefleet.sessions` | Exclusive create / attach / release |
| `devicefleet.providers.base` | `DeviceProvider` ABC + `CloudDeviceProvider` protocol |
| `devicefleet.providers.adb` | Local Android via `adb devices` / `input` / `screencap` |
| `devicefleet.providers.stub` | In-memory hosted phone (reference cloud) |
| `devicefleet.fleet` | Orchestrator used by CLI and API |
| `devicefleet.transport` | Local process vs remote host |
| `devicefleet.api` | FastAPI session/action surface |
| `devicefleet.cli` | `devicefleet` entrypoint |
| `devicefleet.doctor` | Environment report for agents |

State lives under `$DEVICEFLEET_HOME` (default `~/.devicefleet`):

- `devices.yaml` — registered phones
- `sessions.yaml` — leases
- `state.yaml` — per-agent current session ids for the local CLI
- `stub-state.yaml` — virtual phone state so CLI commands share one stub
- `artifacts/<session>/` — screenshots and UI dumps (remote clients download into their own home)
- `locks/` — per-device lease locks so start/stop/run cannot interleave unsafely

## Why sessions are first-class

A USB phone and a cloud farm slot are both scarce. If two agents tap the same device, both get garbage. `SessionManager.start` refuses a second lease on an occupied `device_id`. Agents targeting different ids proceed in parallel. Releasing a session is what makes the phone reusable; it is not implicit process exit.

## Provider contract

Every backend implements:

- `discover()` → `DiscoveredDevice` list
- `screenshot(handle) -> bytes` (PNG)
- `tap`, `swipe`, `type_text`, `keyevent`
- `dump_ui()` → hierarchy XML when the OS can provide it

Cloud backends additionally implement `CloudDeviceProvider`:

- `provision(CloudDeviceSpec)` — acquire a hosted phone
- `release_cloud(handle)` — give it back to the farm
- `health(handle)` — cheap liveness

`LocalAdbProvider` is the first hardware backend. `handle` is the adb serial.

`StubCloudProvider` is the first cloud backend. It records actions on a 1080×1920 virtual display and returns a generated PNG. Tests and CI use this so they never need a handset or an API key.

### Adding a real cloud adapter

1. Create `devicefleet/providers/browserstack.py` (or similar) that subclasses `DeviceProvider` and satisfies `CloudDeviceProvider`.
2. Translate the farm’s session token into `DeviceRecord.provider_ref`.
3. Call `Fleet.set_cloud_provider(...)` (or register it in `Fleet.provider_for`) under `ProviderKind.CLOUD`.
4. Keep secrets in the environment (`BROWSERSTACK_USERNAME`, …), not in YAML.

`ProviderKind.CLOUD` does **not** fall back to `StubCloudProvider`. Registering or leasing a CLOUD device before an adapter is installed is an error.

Do not special-case the CLI or the HTTP routes. New farms should be invisible above the provider layer.

## Transport

`LocalTransport` calls `Fleet` in-process. `HttpTransport` calls the same operations on `devicefleet serve`. After a remote screenshot or UI dump, the client fetches `GET /sessions/{id}/artifacts/{name}` into local `$DEVICEFLEET_HOME/artifacts`. The CLI chooses HTTP when `--remote` or `DEVICEFLEET_REMOTE_URL` is set. That is how a laptop agent drives a lab machine that actually has the USB hub.

`YamlStore.save` writes a unique temp file then `os.replace`s it. Concurrent `save()` calls are last-writer-wins for the whole document. `YamlStore.update` holds an exclusive lock for a read-modify-write so session start and current-session maps merge safely. Current-session keys are last-writer-wins per agent.

## Future work (not in v0)

- iOS via macOS iPhone Mirroring / Accessibility — needs a separate provider, not an ADB look-alike
- Production BrowserStack / Device Farm adapters
- Richer authn than a shared `DEVICEFLEET_TOKEN` (v0 uses bearer / `X-Devicefleet-Token` plus agent ownership)
- Rich IME / unicode input beyond `adb shell input text`
- Richer distributed locking if many hosts share one `DEVICEFLEET_HOME` over NFS

## Design rules

- Prefer a boring YAML file over a hidden database.
- Validate arguments at every public boundary (Pydantic + explicit checks).
- Keep CLI and API thin; put behavior on `Fleet`.
- Do not copy other harness codebases. The problem is “agent controls a phone”; the product is a **fleet**.
