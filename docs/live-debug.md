# Live debug workbench

The optional live debug bridge lets a running Dusklight app connect outward to a
Python host over WebSocket. It exposes the game's registered debug commands and
sampled telemetry. This is useful when the app runs on another machine or cannot
accept an incoming connection. LLDB remains complementary: use it for breakpoints,
stacks, memory inspection, and failures before the bridge starts.

Physical-device operation has not been verified. Host loopback tests exercise the
protocol with a simulated app; they do not prove tvOS signing, networking,
environment-variable injection, or an actual device connection.

## Build and prepare

The bridge is disabled by default. Enable it explicitly when configuring a build:

```sh
cmake --preset macos-default-relwithdebinfo -DDUSK_ENABLE_LIVE_DEBUG=ON
cmake --build --preset macos-default-relwithdebinfo
```

Use the appropriate preset for your platform. Install the separate host dependency
in an isolated environment (macOS/Linux host, Python 3.11 or newer):

```sh
python3 -m venv build/live-debug-venv
build/live-debug-venv/bin/python -m pip install -r tools/requirements-live-debug.txt
```

The dependency is pinned to `websockets==17.4`, using its maintained
[asyncio server API](https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html).

## Connect locally

Generate a token directly into an environment variable so it is not printed into
terminal logs or written into a file. Keep the same value available to the host
and app processes:

```sh
export DUSK_DEBUG_TOKEN="$(build/live-debug-venv/bin/python tools/live_debug.py --generate-token)"
build/live-debug-venv/bin/python tools/live_debug.py
```

Start the app from a second shell with that same token and the host URL:

```sh
export DUSK_DEBUG_URL=ws://127.0.0.1:8765
# Set DUSK_DEBUG_TOKEN to the same secret in this shell.
build/macos-default-relwithdebinfo/Dusklight.app/Contents/MacOS/Dusklight --dvd /path/to/game.iso
```

The host waits up to 60 seconds for the app; use `--wait SECONDS` to change this.
The URL and token must reach the app's environment before launch. The bridge's
build flag and both variables are required; ordinary builds do not expose it.

For a remote app, set `--host` to the host's reachable interface and put that host's
address in `DUSK_DEBUG_URL`. The default binding is `127.0.0.1`; `0.0.0.0` binds all
interfaces. Plain `ws://` sends commands, state, and the bearer token without
transport encryption. Use a trusted isolated network or an encrypted tunnel, and
do not publish the listener on the internet. The host also accepts `--port` and
`--token`; prefer the environment to placing a secret in process arguments.

## Commands and telemetry

For an installed development build on Apple TV, `devicectl` can pass the bridge
environment without placing the token in command-line arguments:

```sh
export DEVICECTL_CHILD_DUSK_DEBUG_URL=ws://LAPTOP_LAN_ADDRESS:8765
export DEVICECTL_CHILD_DUSK_DEBUG_TOKEN="$DUSK_DEBUG_TOKEN"
xcrun devicectl device process launch --device 'APPLE_TV_NAME' --console \
  dev.twilitrealm.dusk -- --dvd /DEVICE/PATH/game.rvz
```

Start the host listener on its LAN interface first. The game image path must be
readable on the Apple TV; a laptop path is not sufficient. This command assumes
the app has already been staged, provisioned, signed, and installed.

For native debugging, launch with `--start-stopped`, then use `xcrun lldb`:

```text
device select APPLE_TV_NAME
device process attach -p APP_PROCESS_ID
```

The launch result supplies the process ID. LLDB can stop the process, including
its networking; the WebSocket stream is for inspection while execution continues.

At the host prompt, enter a registered game debug command, for example `pos` to
inspect position. Existing commands include `list`, `find`, `camera`, `warp`,
`time`, and `freeze`/`rate`; arguments follow the game console syntax. The workbench prints
the response as JSON, including `lines`, `truncated`, and `completed`. Built-in host
controls are:

- `/ping`: check the app connection.
- `/subscribe 5`: request telemetry at five samples per second (integer 1–10).
- `/unsubscribe`: stop telemetry.
- `/quit`: close the workbench.

The host writes each telemetry event as one JSON line. Telemetry includes
`timestampNs`, `pollIntervalMs`, `pollFps`, and `gameReady`; a ready game also
reports `simRate`, and an available player adds `stage`, `room`, and
`player` (`x`, `y`, `z`). `pollFps` measures main-loop polling cadence and does not
measure GPU rendering performance. `seq` identifies samples;
`dropped` reports samples discarded by the app's bounded delivery path. A stalled
host may lose samples. This is a diagnostic stream, not a lossless capture.

For a scripted command, pass `--command 'GAME COMMAND'` (repeatable). The host runs
those commands once the app connects, prints responses, and exits unless
`--subscribe HZ` was also supplied. Piped stdin also accepts commands and host
controls; EOF ends the session. Requests time out after ten seconds. On disconnect,
pending requests fail and an interactive host can accept a later app connection.
The app retries failed or closed connections after 1, 2, 4, 8, 16, then 30 seconds,
with later retries capped at 30 seconds. A successful connection resets the delay.
Reconnection clears command state and telemetry subscriptions; subscribe again on the host.

Add `--record build/live-debug.jsonl` to append inbound response/event objects to
a JSONL recording. Files are created with mode `0600`; existing recording files also have their
permissions restricted to `0600`. `--record-max-bytes` defaults to 16 MiB and includes existing file
size. Once a record would exceed the budget it is omitted; recording never grows
past that budget. Recordings can contain private state and command output. Keep
them out of Git and share only the relevant redacted excerpts.

## Protocol v1

The app initiates the connection with `Authorization: Bearer TOKEN`. The host
rejects unauthorized HTTP handshakes before accepting any messages. The first text
message must be `{"type":"hello","version":1}` within five seconds. One app may
connect at a time; another receives close code 1013. Malformed messages and
unsupported protocol versions close the connection with code 1008.

Host requests contain `id` (string), `method`, and `params` (object). Supported
methods are `ping`, `command` (`line`, at most 4096 UTF-8 bytes), `subscribe` (`hz`,
integer 1–10), and `unsubscribe`. Responses correlate by `id` and carry either an
object `result` or `error` with `code` and `message`. Telemetry has this shape:

```json
{"type":"event","event":"telemetry","seq":1,"dropped":0,"data":{}}
```

The host limits inbound messages to 64 KiB, queues at most eight received frames,
and permits at most 32 outstanding requests. Responses with unknown or duplicate
IDs are protocol errors; a response arriving after its request timed out is also
unknown. Commands are executed by the app, so their availability and safety depend
on the current game state and registered command implementation.

## Host validation

```sh
build/live-debug-venv/bin/python -m unittest discover -s tests -p test_live_debug.py -v
```

These loopback tests cover authentication, hello/version validation, out-of-order
response correlation, telemetry, remote errors, malformed messages, disconnects,
reconnection, duplicate apps, limits, timeouts, and bounded private recordings.

For a native macOS/Linux configuration with the bridge enabled, the standalone
bridge regression uses a fake transport to exercise delayed startup and shutdown:

```sh
cmake --build --preset macos-default-relwithdebinfo --target dusk_live_debug_test
build/macos-default-relwithdebinfo/dusk_live_debug_test
```

This host-only target is excluded from normal builds and unavailable when
cross-compiling. Its assertions remain enabled in release configurations.
