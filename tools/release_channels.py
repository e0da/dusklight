#!/usr/bin/env python3
"""Record selected versions and explicitly promote release channel pointers."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote


CATALOG = "config/channels.json"
POINTERS = "releases/channels.json"
NUMBER = r"(?:0|[1-9][0-9]*)"
IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
TAG_PATTERN = re.compile(
    rf"midna/v{NUMBER}\.{NUMBER}\.{NUMBER}"
    rf"(?:-{IDENTIFIER}(?:\.{IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError(f"Git lookup failed: {' '.join(args)}")
    return result.stdout.decode("utf-8").strip()


def validate_tag(tag):
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise ValueError("tag must be midna/vSEMVER, for example midna/v0.2.0")


def validate_channel(channel):
    if not isinstance(channel, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", channel):
        raise ValueError("channel must be a nonempty identifier using letters, digits, _ or -")


def record_path(repo, tag):
    validate_tag(tag)
    return Path(repo) / "releases/versions" / (quote(tag, safe="") + ".json")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read JSON: {path}") from error


def snapshot(repo, tag, commit):
    validate_tag(tag)
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("commit must be a full lowercase Git object ID")
    ref = f"refs/tags/{tag}"
    tag_object = git(repo, "rev-parse", "--verify", ref)
    actual_commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if actual_commit != commit:
        raise ValueError("tag does not resolve to the selected commit")
    blob = git(repo, "rev-parse", "--verify", f"{commit}:{CATALOG}")
    try:
        catalog = json.loads(git(repo, "show", f"{commit}:{CATALOG}"))
    except ValueError as error:
        raise ValueError("release catalog is missing or invalid JSON") from error
    if (not isinstance(catalog, dict) or type(catalog.get("schemaVersion")) is not int
            or catalog["schemaVersion"] != 1 or not isinstance(catalog.get("channels"), dict)):
        raise ValueError("release catalog must have schemaVersion 1 and a channels object")
    return tag_object, blob, catalog


def atomic_json(path, value, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".release-", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if read_json(path) != value:
                    raise ValueError("version already exists with different metadata")
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare(repo, tag, commit, artifact_metadata=None):
    tag_object, blob, _ = snapshot(repo, tag, commit)
    metadata = {} if artifact_metadata is None else artifact_metadata
    if not isinstance(metadata, dict):
        raise ValueError("artifact metadata must be a JSON object")
    record = {
        "schemaVersion": 1, "tag": tag, "commit": commit, "tagObjectSha": tag_object,
        "catalogPath": CATALOG, "catalogBlobSha": blob, "artifactMetadata": metadata,
    }
    atomic_json(record_path(repo, tag), record, immutable=True)
    return record


def load_pointers(repo):
    pointers = read_json(Path(repo) / POINTERS)
    if (not isinstance(pointers, dict) or type(pointers.get("schemaVersion")) is not int
            or pointers["schemaVersion"] != 1 or not isinstance(pointers.get("channels"), dict)):
        raise ValueError("channel pointers must contain schemaVersion 1 and a channels object")
    for channel, entry in pointers["channels"].items():
        validate_channel(channel)
        if entry is None:
            continue
        if not isinstance(entry, dict) or set(entry) != {"tag", "commit", "catalogBlobSha"}:
            raise ValueError("invalid channel pointer")
        validate_tag(entry["tag"])
        for key in ("commit", "catalogBlobSha"):
            if not isinstance(entry[key], str) or not re.fullmatch(
                    r"[0-9a-f]{40}|[0-9a-f]{64}", entry[key]):
                raise ValueError("invalid channel pointer object ID")
    return pointers


def promote(repo, channel, tag):
    validate_channel(channel)
    record = read_json(record_path(repo, tag))
    keys = {"schemaVersion", "tag", "commit", "tagObjectSha", "catalogPath",
            "catalogBlobSha", "artifactMetadata"}
    if (not isinstance(record, dict) or set(record) != keys
            or type(record.get("schemaVersion")) is not int or record["schemaVersion"] != 1
            or record.get("tag") != tag or record.get("catalogPath") != CATALOG
            or not isinstance(record.get("artifactMetadata"), dict)):
        raise ValueError("invalid version record")
    tag_object, blob, catalog = snapshot(repo, tag, record["commit"])
    if tag_object != record["tagObjectSha"] or blob != record["catalogBlobSha"]:
        raise ValueError("tag or catalog no longer matches the immutable version record")
    if channel not in catalog["channels"] or not isinstance(catalog["channels"][channel], dict):
        raise ValueError("channel does not exist in the recorded release catalog")
    pointers = load_pointers(repo)
    pointers["channels"][channel] = {
        key: record[key] for key in ("tag", "commit", "catalogBlobSha")
    }
    atomic_json(Path(repo) / POINTERS, pointers)
    return pointers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="action", required=True)
    selected = commands.add_parser("prepare", help="record an already tagged version")
    selected.add_argument("--tag", required=True)
    selected.add_argument("--commit", required=True)
    selected.add_argument("--artifact-metadata", type=Path, help="optional JSON object file")
    promoted = commands.add_parser("promote", help="explicitly update a tracked channel pointer")
    promoted.add_argument("--channel", required=True, help="channel defined in the recorded release catalog")
    promoted.add_argument("--tag", required=True)
    commands.add_parser("status", help="show selected release channel pointers")
    args = parser.parse_args()
    try:
        repo = Path(git(args.repo, "rev-parse", "--show-toplevel"))
        if args.action == "prepare":
            metadata = read_json(args.artifact_metadata) if args.artifact_metadata else None
            result = prepare(repo, args.tag, args.commit, metadata)
        elif args.action == "promote":
            result = promote(repo, args.channel, args.tag)
        else:
            result = load_pointers(repo)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (ValueError, OSError, TypeError) as error:
        parser.exit(1, f"release-channels: {error}\n")


if __name__ == "__main__":
    main()
