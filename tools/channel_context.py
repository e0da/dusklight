#!/usr/bin/env python3
"""Python bridge to the canonical Rust channel-context CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "config/channels.json"
MANIFEST = ROOT / "libs/channel_context/Cargo.toml"


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def runtime_sdk():
    return {
        "crate": MANIFEST.parent,
        "manifest": MANIFEST,
        "header": MANIFEST.parent / "include/channel_context.h",
    }


def load_catalog(path=DEFAULT_CATALOG):
    with Path(path).open(encoding="utf-8") as source:
        return json.load(source, object_pairs_hook=unique_object)


def rust_command():
    executable = os.environ.get("DUSK_CHANNEL_CONTEXT_BIN")
    if executable:
        return [executable]
    cargo = os.environ.get("CARGO") or shutil.which("cargo")
    if not cargo:
        raise ValueError("Rust cargo is required, or set DUSK_CHANNEL_CONTEXT_BIN")
    return [cargo, "run", "--quiet", "--manifest-path", str(MANIFEST),
            "--bin", "channel-context", "--"]


def resolve_catalog(catalog):
    return _resolve(catalog, ["--all"])


def resolve_context(catalog, channel=None):
    return _resolve(catalog, [] if channel is None else ["--channel", channel])


def _resolve(catalog, options):
    with tempfile.TemporaryDirectory(prefix="dusklight-context-") as temporary:
        path = Path(temporary) / "catalog.json"
        path.write_text(json.dumps(catalog, allow_nan=False), encoding="utf-8")
        result = subprocess.run(rust_command() + ["resolve", "--catalog", str(path), *options],
                                capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Rust channel resolver failed")
    return json.loads(result.stdout)


def main():
    try:
        return subprocess.run(rust_command() + sys.argv[1:]).returncode
    except (OSError, ValueError) as error:
        print(f"channel-context: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
