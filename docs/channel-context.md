# Shared channel context

`config/channels.json` is managed code. Each source revision defines its complete
channel catalog, default selection, inherited collections, application identity,
tooling configuration, and hierarchical feature objects. Play and Lab are runtime
contexts; `main` remains an integration branch.

## Configuration format

Prefer TOML for human-authored configuration, including feature collections and
channel declarations. Use JSON for generated manifests, resolved snapshots, and
debug transport messages where machine interchange is the primary purpose.
Normalize authored configuration through the canonical Rust resolver so tooling
and runtime share validation and inheritance semantics.

The current catalog is still JSON; TOML loading and catalog migration are follow-up
work. Existing commands and artifact contracts below describe the implemented JSON
format. When adding TOML support, use tables for nested feature paths and quoted
keys when a literal identifier contains dots.

The Rust crate in `libs/channel_context` is the canonical resolver used by both the
application and the Python tooling wrapper. Collections merge left to right, then
the child deep-overrides objects. Scalars and arrays replace inherited values.
Missing parents, cycles, invalid defaults, and malformed catalog objects fail
resolution. No selected channel means the catalog's `defaultChannel`.

## Runtime and reification

The application embeds the entire raw catalog. Startup selects CLI `--channel`,
then `DUSK_CHANNEL`, then the staged `channel-output.json`, then the tracked default.
Packaging copies the exact build catalog as `channel-catalog.json`; tooling rejects
a supplied catalog that disagrees with that artifact.

Rust owns immutable snapshots, scoped child overrides, typed scalar columns,
prepared transitions, generations, and dirty JSON-Pointer paths. Consumers can
retain snapshots after the runtime changes. Scoped overrides and typed columns
are initially exposed through the Rust API; the engine adapter uses owned C ABI
snapshot/transition handles.

The C++ adapter validates supported features, queues a transition, then commits and
reconciles it at a game-thread boundary. Current bindings control gyro aiming,
input/gyro overlays, and the live-control transport through existing settings
callbacks. Repeated unchanged values do not notify settings subscribers. Controller
discovery policy is a startup feature; changing it live is rejected with a restart
reason. Features requiring GPU/resource reconstruction will need their own prepare
and retirement bindings before they can be switched safely.

Use the overlay's **Channel** tab or the console:

```text
channel
channel lab
channel play
```

The console command reports a scheduled transition; inspect `channel` again for
the committed generation. Play disables the live-control bridge by default, so use
the controller-accessible Channel menu to return to Lab. Feature-owned settings use
non-persistent overrides; editing their backing preference does not override the
active context. User controller bindings and unrelated preferences remain separate.

## Build and tools

Compile all supported channel capabilities into the same application. The default
build includes live-control capability; its runtime activation follows the context
and still requires private endpoint/token configuration. An explicitly capability-
reduced build rejects a context that requires unavailable live control.

```sh
cargo run --manifest-path libs/channel_context/Cargo.toml -- \
  resolve --catalog config/channels.json --all
python3 tools/channel_context.py resolve --channel lab
cargo test --manifest-path libs/channel_context/Cargo.toml
```

Packaging selects an output context for name, icon, bundle identity and initial
runtime selection. Runtime switching reifies features inside that installed app;
its Home screen identity remains the selected packaging identity. See
[tvos-channels.md](tvos-channels.md).

Version tags identify immutable releases. Explicit release pointers select which
release is distributed through each channel; they are not changed by context
selection, ordinary builds, or merges. See [releases.md](releases.md).
