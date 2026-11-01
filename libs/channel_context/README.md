# Channel context core

This crate owns schema version 1 catalog resolution for the game and packaging tools.
Collections may extend other collections. Channels extend collections. Parents merge in
listed order, then the child's fields merge last. Objects merge recursively; arrays,
scalars and null replace earlier values. Every collection is validated, including unused
collections. Unknown parents, cycles and an unknown default channel reject the catalog.

```sh
cargo test --manifest-path libs/channel_context/Cargo.toml
cargo run --quiet --manifest-path libs/channel_context/Cargo.toml --bin channel-context -- \
  resolve --catalog config/channels.json --channel lab
cargo run --quiet --manifest-path libs/channel_context/Cargo.toml --bin channel-context -- \
  resolve --catalog config/channels.json --all
cargo run --quiet --manifest-path libs/channel_context/Cargo.toml --bin channel-context -- \
  get --catalog config/channels.json --path features.input.gyroAim.enabled
```

Omitting `--channel` selects `defaultChannel`. A resolved context includes its `channel`
name. `--all` returns `{schemaVersion, defaultChannel, channels}` with resolved objects
keyed by channel name; those objects omit the redundant `channel` field.

The runtime starts at generation zero. `prepare` creates a candidate and dirty leaf paths
without publishing it. Reify supported engine changes first, then `commit`. A transition
from another runtime or from an older snapshot fails without changing the current state.
Even a no-op selection creates a new generation and invalidates older prepared work.
Engine changes and rollback remain the caller's responsibility.

Snapshots use `Arc`; holding an older snapshot preserves its data across commits and
runtime destruction. Rust scoped overrides return a new snapshot, retain their parent,
and leave siblings and the runtime unchanged. Overrides cannot change the channel name.

Dirty paths and column paths are JSON Pointers, with `~` and `/` escaped. Arrays are
indivisible leaves. Object-to-scalar changes report removed child leaves and the new
parent leaf. Typed scalar columns group booleans, signed/unsigned integers, floats,
strings and nulls. IDs are deterministic FNV-1a hashes of pointer paths, with collisions
within a snapshot rejected. Columns are a simple typed view of each snapshot; this
implementation makes no throughput or memory-saving claim.

The C interface is in `include/channel_context.h`. Every returned handle has one matching
destroy call; getters borrow strings until that handle is destroyed. Commit borrows its
transition and does not consume it. Runtime access requires external synchronization.
Snapshots may outlive the runtime. Null destruction is allowed. Errors are thread-local
and remain available until the next failure on that thread. JSON remains canonical; the
C interface does not expose the typed columns or scoped overrides yet.

The static library is `libchannel_context.a`. Cross-check tvOS with a toolchain that has
the target installed:

```sh
cargo +nightly build --manifest-path libs/channel_context/Cargo.toml \
  --target aarch64-apple-tvos --lib
```
