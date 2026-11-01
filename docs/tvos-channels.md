# Apple TV channel contexts

A channel selects settings from the tracked [catalog](../config/channels.json).
Every staged app carries the complete resolved catalog and the complete executable.
Selecting a channel changes its initial context and app identity; it does not
remove feature code, create a version, or promote a build.

| Channel | Home screen name | Icon badge | Default bundle ID |
| --- | --- | --- | --- |
| Play | Dusklight Play | Gold PLAY | `dev.e0da.dusklight.play` |
| Lab | Dusklight Lab | Purple LAB | `dev.e0da.dusklight.lab` |

Distinct bundle IDs give the installations separate tvOS containers, saves,
controller settings, and caches. Keep a known working installation while testing
updates. Channel names describe context, independently of source branches or
version tags; either channel can use the same compiled executable.

## Resolve the tracked catalog

Collections provide reusable settings. `extends` references collections, in order;
later parents override earlier parents, then the child overrides the result.
Objects merge recursively; arrays and scalar values replace their predecessors.
`null` is a value, with no deletion semantics. Cycles, missing references, duplicate
JSON keys, and invalid schema types are errors.

The catalog defaults to Play. Both channels enable gyro aiming and the full
controller policy. Play disables live control, the input overlay, and its gyro
indicators. Lab inherits the same gameplay settings and adds a diagnostics mixin
that enables those diagnostics. Tooling defaults include debug port 8766 and
one telemetry sample per second. Features and tooling can grow nested settings
without adding another flat flag namespace.

Rust in `libs/channel_context` owns resolution. The Python entrypoint delegates to
that CLI and contains no inheritance implementation. Install Rust/Cargo first,
or set `DUSK_CHANNEL_CONTEXT_BIN` to a previously built `channel-context` binary:

```sh
python3 tools/channel_context.py resolve --catalog config/channels.json
python3 tools/channel_context.py resolve --catalog config/channels.json --channel lab
python3 tools/channel_context.py resolve --catalog config/channels.json --all
```

`--all` emits all resolved contexts for build/runtime embedding. A single resolved
context contains `channel`, `app`, `tooling`, and `features`. The complete result
contains `schemaVersion`, `defaultChannel`, and `channels`. Build integration,
host tooling, and runtime selection consume this shared model. Staging records an
initial channel in `channel-output.json` and `DusklightOutputChannel`. At startup,
`--channel` or `DUSK_CHANNEL` overrides that file; without any selection, runtime
uses the compiled catalog default. The compiled raw catalog remains runtime
authority; the staged resolved catalog supports inspection. Runtime changes do not rewrite the
committed catalog or the installed bundle identity.

## Build and stage

Build the desired source revision with `tvos-default` as described in
[building.md](building.md). Record the actual build commit and source ref before
reusing a build directory. Neither channel requires a particular branch.

```sh
python3 tools/tvos_channels.py stage --channel lab \
  --app build/tvos-default/Dusklight.app --output build/tvos-channels \
  --commit FULL_BUILD_COMMIT --source-ref YOUR_BUILD_REF \
  --catalog config/channels.json --version-tag midna/v1.2.3 \
  --game-image /private/path/game.rvz
```

Omit `--channel` to use `defaultChannel` from the artifact catalog. The source app
must contain `channel-catalog.json`, copied from the same raw catalog that CMake
embedded in the executable. Staging uses that artifact by default. An explicit
`--catalog` must match it structurally; formatting differences are allowed. A
missing or different catalog is rejected before staging. This guards against
using new tooling settings with an older binary; it does not independently prove
the provenance of an arbitrary executable.
`--version-tag` is optional recorded metadata: staging
creates no Git tags and changes no promotion pointers. Use the actual executable's
source commit even when staging from a newer tooling checkout. A full hexadecimal
commit ID and an executable SHA-256 preserve the supplied provenance; packaging
does not independently prove that an arbitrary binary came from that commit.

Staging copies the complete app, invokes the Swift icon generator and Xcode
`actool`, and merges the generated icon plist keys. It sets the catalog's display
name and bundle suffix, `UIDeviceFamily` to `[3]`, and the initial output channel.
It embeds `channel-contexts.json` with every resolved channel and
`channel-output.json` with the initial selection, and writes a sibling
`channel-manifest.json` with the selected context, catalog hash, source provenance,
executable hash, artifact catalog hash, and optional version tag. It preserves bundled profiles and code;
changes to the bundle require signing again. Staging performs no signing, install,
launch, or removal.

Outputs are `build/tvos-channels/{play,lab}/Dusklight.app`. Existing outputs require
`--replace`, and replacement must match the existing app/channel identity. Source
and output paths cannot overlap. Failed icon generation or compilation leaves the
previous staged output intact.

Optional private disc images use stable names such as `game.rvz` or `game.iso`.
The manifest records `gameImageRelativePath` and root image filenames. Output
containing images, including images inherited from the source app, must be ignored
when inside a Git repository. Keep game data, provisioning profiles, and credentials
private and outside commits.

## Sign, install, and validate

Use a tvOS development profile matching the selected catalog-derived bundle ID.
Verify the final signature and identity before installing with `devicectl`.
Staging may preserve an old signature or source profile; neither establishes that
the changed bundle is ready to install.

Each installation needs its own readable game path and controller settings. For a
privately bundled image, resolve the installed bundle URL and launch with
`--dvd /DEVICE/BUNDLE/PATH/game.rvz`. Updates can change that URL. Copy initial
saves and controller profiles explicitly with backups; channel containers have no
live save synchronization. tvOS cache storage remains evictable.

Validation must identify the actual executable revision, resolved catalog, selected
channel, display name, icon, profile, and bundle ID. Successful installation alone
does not prove older-tvOS compatibility, graphics performance, or every context
transition. Keep the prior working installation until its replacement is checked.

```sh
python3 -m unittest discover -s tests -p test_tvos_channels.py -v
cargo test --manifest-path libs/channel_context/Cargo.toml
```

Packaging tests stub the Rust resolver and native tools to exercise identity,
metadata, preservation, merging, failure recovery, overlap, replacement, and private
image guards. Rust tests own inheritance and runtime-context semantics. A real
Swift/actool staging smoke additionally checks the generated catalog contract.

For the physical-device checkpoint, personal controller layout, save limitations,
and screenshot evidence, see [Apple TV development and validation](tvos.md).
