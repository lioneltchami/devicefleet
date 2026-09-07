# Devicefleet skill

Use **devicefleet** when you need to drive one or more real (or stub) Android phones from an agent: screenshots, taps, swipes, text, key events, and UI dumps. Prefer this tool over ad-hoc `adb` when more than one phone exists, when another agent might be using the lab, or when the phones live on a remote host.

Product: devicefleet. Owner repo: https://github.com/lioneltchami/devicefleet

## Setup

```bash
pip install -e ".[dev]"   # from the repo
devicefleet doctor
```

`doctor` is the first command you run. It tells you:

- whether Python is 3.11+
- where the data directory is (`DEVICEFLEET_HOME`, default `~/.devicefleet`)
- whether `adb` is installed and which serials are attached
- that `StubCloudProvider` works with **no hardware and no paid cloud account**

If `adb` is missing, do **not** block. Use `--device stub-demo` for demos and tests.

## Core workflow

Always take a session before you touch a phone. Always release it when you are done.

```bash
devicefleet devices list
devicefleet session start --device stub-demo --agent <your-name>
devicefleet run screenshot
devicefleet run dump-ui
# tap / swipe / type / key as needed
devicefleet session stop
```

Useful selectors:

```bash
devicefleet session start --device pixel-lab
devicefleet session start --tag lab --tag android --agent coder
devicefleet session attach ses_ab12cd34ef56 --agent coder
devicefleet session list --active
```

`--tag` is AND. If every matching device is busy, start fails — pick another tag or wait. Do not “just run adb” on a device that already has a session.

Locally, the CLI remembers **your** current session, keyed by `--agent` / `DEVICEFLEET_AGENT`. Another agent in the same data dir will not pick it up. `DEVICEFLEET_CURRENT_SESSION` overrides that memory for this process only. On `--remote`, always pass `--session` (there is no shared current session). Attach requires the same agent label that started the lease.

## Helpers

```bash
devicefleet run screenshot
devicefleet run tap 540 960
devicefleet run swipe 540 1600 540 400 --duration 250
devicefleet run type "hello world"
devicefleet run key BACK          # also HOME, ENTER, numeric keycodes
devicefleet run dump-ui
devicefleet run info
```

Screenshots and UI XML land under `$DEVICEFLEET_HOME/artifacts/<session>/` on the machine that ran the helper. On `--remote`, the CLI downloads those files from the host into the **local** `$DEVICEFLEET_HOME/artifacts/<session>/` so the agent can read them without SSH. Do not treat the host path in a raw API response as a local file.

`adb shell input text` is ASCII-oriented. Spaces become `%s`; `%` and backticks are percent-encoded so they are not rewritten or executed. Newlines are sent as ENTER keyevents. Keep typed strings short. Use key events for Back / Home / Enter.

`devicefleet doctor` inspects the **local** machine only. Do not pass `--remote` to doctor — run it on the fleet host.

Add `--json` to any list/start/run command when you need to parse output.

## Multi-device and remote lab

```bash
devicefleet devices discover --save
devicefleet devices register pixel-2 --provider adb --ref EMULATOR-5554 --tag emu
```

If the phones are attached to another machine:

```bash
# on the lab host (0.0.0.0 requires DEVICEFLEET_TOKEN)
export DEVICEFLEET_TOKEN=replace-me
devicefleet serve --host 0.0.0.0 --port 8765

# on the agent machine
export DEVICEFLEET_TOKEN=replace-me
devicefleet --remote http://lab:8765 --agent coder session start --tag android
devicefleet --remote http://lab:8765 --agent coder run screenshot --session ses_…
```

`DEVICEFLEET_REMOTE_URL` sets the default remote. Send the token with `--token` or `DEVICEFLEET_TOKEN`.

## Cloud providers

v0 ships `StubCloudProvider` only. A real BrowserStack / Device Farm adapter must implement `CloudDeviceProvider` (see ARCHITECTURE.md). Do not ask the user for paid credentials just to run the stub path.

## Do not

- Do not start a second session on a device that is already `busy`.
- Do not skip `session stop` after a successful run.
- Do not invent iOS mirroring commands — that path is not implemented.
- Do not copy or vendor other phone-harness projects. Stay on devicefleet APIs.

## Print this skill

```bash
devicefleet skill
```
