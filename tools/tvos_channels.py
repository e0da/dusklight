#!/usr/bin/env python3
"""Stage independent, unsigned tvOS channel bundles; never sign or install them."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile


CHANNELS = {"trunk": "Dusklight Trunk", "dev": "Dusklight Dev"}
DISC_SUFFIXES = {".rvz", ".iso", ".gcm", ".wia", ".wbfs", ".ciso", ".gcz"}


def merge_plist(destination, generated):
    for key, value in generated.items():
        if isinstance(value, dict) and isinstance(destination.get(key), dict):
            merge_plist(destination[key], value)
        else:
            destination[key] = value


def load_plist(path):
    with path.open("rb") as source:
        value = plistlib.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"expected plist dictionary: {path}")
    return value


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contains(parent, child):
    return child == parent or parent in child.parents


def require_private_output(path):
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            result = subprocess.run(
                ["git", "-C", str(parent), "check-ignore", "--quiet", "--", str(path / "dusklight-channel-output")],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise ValueError("game-image output inside a repository must be Git-ignored")
            return


def stage(args, runner=subprocess.run):
    channel = args.channel
    if channel not in CHANNELS:
        raise ValueError("channel must be trunk or dev")
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", args.commit):
        raise ValueError("commit must be a full 40- or 64-digit hexadecimal object ID")
    if not args.source_ref or any(c.isspace() for c in args.source_ref):
        raise ValueError("source-ref must be a nonempty ref without whitespace")
    if channel == "trunk" and args.source_ref not in ("main", "refs/heads/main"):
        raise ValueError("trunk requires source-ref main or refs/heads/main")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", args.bundle_prefix):
        raise ValueError("bundle-prefix must be a reverse-DNS bundle identifier")
    if args.bundle_prefix.endswith((".trunk", ".dev")):
        raise ValueError("bundle-prefix cannot contain a reserved channel suffix")
    app = Path(args.app).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve()
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("app must be an existing .app directory")
    if contains(app, output) or contains(output, app):
        raise ValueError("output and source app must not overlap")
    target = output / channel
    bundle_id = f"{args.bundle_prefix}.{channel}"
    info = load_plist(app / "Info.plist")
    source_bundle_id = info.get("CFBundleIdentifier")
    other = "dev" if channel == "trunk" else "trunk"
    if info.get("DusklightChannel") == other or info.get("CFBundleIdentifier") == f"{args.bundle_prefix}.{other}":
        raise ValueError("cannot relabel the other reserved channel")
    executable = info.get("CFBundleExecutable")
    if not isinstance(executable, str) or not executable or Path(executable).name != executable:
        raise ValueError("CFBundleExecutable must be a filename")
    if not (app / executable).is_file() or (app / executable).is_symlink():
        raise ValueError("bundle executable must be a regular file")
    if target.is_symlink():
        raise ValueError("channel destination must not be a symlink")
    if target.exists():
        if not args.replace:
            raise ValueError("channel destination exists; pass --replace for an intentional update")
        old_info = load_plist(target / "Dusklight.app" / "Info.plist")
        if old_info.get("CFBundleIdentifier") != bundle_id or old_info.get("DusklightChannel") != channel:
            raise ValueError("replacement destination has the wrong channel identity")
    game_image = None
    if args.game_image:
        game_image = Path(args.game_image).expanduser().resolve(strict=True)
        if not game_image.is_file() or game_image.suffix.lower() not in DISC_SUFFIXES:
            raise ValueError("game-image must be an existing supported private disc image")
        if contains(target, game_image):
            raise ValueError("game-image source cannot be inside the replacement destination")
    if game_image or any(path.suffix.lower() in DISC_SUFFIXES for path in app.rglob("*") if path.is_file()):
        require_private_output(output)
    output.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{channel}-stage-", dir=output))
    try:
        staged_app = temporary / "Dusklight.app"
        shutil.copytree(app, staged_app, symlinks=True,
                        ignore=shutil.ignore_patterns("_CodeSignature", "embedded.mobileprovision"))
        icons = temporary / "icons"
        runner(["swift", str(Path(__file__).resolve().with_name("tvos_icons.swift")),
                "--channel", channel, "--output", str(icons)], check=True)
        partial = temporary / "asset-info.plist"
        runner(["xcrun", "actool", str(icons / "Assets.xcassets"),
                "--compile", str(staged_app), "--platform", "appletvos",
                "--target-device", "tv", "--minimum-deployment-target", str(info.get("MinimumOSVersion", "17.0")),
                "--app-icon", "AppIcon", "--output-partial-info-plist", str(partial)], check=True)
        if not (staged_app / "Assets.car").is_file():
            raise ValueError("actool did not produce Assets.car")
        merge_plist(info, load_plist(partial))
        info.update({"CFBundleIdentifier": bundle_id, "CFBundleName": CHANNELS[channel],
                     "CFBundleDisplayName": CHANNELS[channel], "UIDeviceFamily": [3],
                     "DusklightChannel": channel, "DusklightSourceCommit": args.commit.lower(),
                     "DusklightSourceRef": args.source_ref})
        with (staged_app / "Info.plist").open("wb") as destination:
            plistlib.dump(info, destination)
        if game_image:
            shutil.copy2(game_image, staged_app / ("game" + game_image.suffix.lower()))
        bundled_images = sorted(path.name for path in staged_app.iterdir()
                                if path.is_file() and path.suffix.lower() in DISC_SUFFIXES)
        image_path = ("game" + game_image.suffix.lower()) if game_image else (bundled_images[0] if len(bundled_images) == 1 else None)
        manifest = {
            "schemaVersion": 1, "channel": channel, "bundleIdentifier": bundle_id,
            "displayName": CHANNELS[channel], "commit": args.commit.lower(),
            "sourceRef": args.source_ref, "sourceApp": str(app),
            "sourceBundleIdentifier": source_bundle_id,
            "sourceIdentityVerification": "caller-provided; not verified by staging",
            "executable": executable, "executableSHA256": sha256(staged_app / executable),
            "signed": False, "gameImageIncluded": bool(bundled_images),
            "gameImageRelativePath": image_path, "gameImageFiles": bundled_images,
        }
        (temporary / "channel-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if target.exists():
            if not args.replace or target.is_symlink():
                raise ValueError("destination changed during staging; refusing replacement")
            current = load_plist(target / "Dusklight.app" / "Info.plist")
            if current.get("CFBundleIdentifier") != bundle_id or current.get("DusklightChannel") != channel:
                raise ValueError("replacement destination has the wrong channel identity")
            backup = temporary.with_name(temporary.name + "-previous")
            target.rename(backup)
            try:
                temporary.rename(target)
            except BaseException:
                backup.rename(target)
                raise
            shutil.rmtree(backup)
        else:
            temporary.rename(target)
        return target / "Dusklight.app"
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    command = commands.add_parser("stage", help="copy and identify an unsigned channel bundle")
    command.add_argument("--channel", choices=CHANNELS, required=True)
    command.add_argument("--app", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--commit", required=True)
    command.add_argument("--source-ref", required=True)
    command.add_argument("--bundle-prefix", default="dev.e0da.dusklight")
    command.add_argument("--game-image")
    command.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        print(stage(args))
    except (ValueError, OSError, plistlib.InvalidFileException, subprocess.CalledProcessError) as error:
        parser.exit(1, f"tvos-channels: {error}\n")


if __name__ == "__main__":
    main()
