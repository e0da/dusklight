#!/usr/bin/env python3
"""Stage tvOS channel contexts without signing, installing, or promoting versions."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile

if __package__:
    from . import channel_context
else:
    import channel_context


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
    app = Path(args.app).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve()
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("app must be an existing .app directory")
    if contains(app, output) or contains(output, app):
        raise ValueError("output and source app must not overlap")
    catalog_path = app / "channel-catalog.json"
    if not catalog_path.is_file():
        raise ValueError("source app lacks channel-catalog.json; rebuild a context-aware executable")
    raw_catalog = channel_context.load_catalog(catalog_path)
    requested_catalog = getattr(args, "catalog", None)
    if requested_catalog:
        supplied = channel_context.load_catalog(Path(requested_catalog))
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if canonical(supplied) != canonical(raw_catalog):
            raise ValueError("supplied catalog differs from the executable's artifact catalog")
    catalog = channel_context.resolve_catalog(raw_catalog)
    channel = args.channel or catalog["defaultChannel"]
    if channel not in catalog["channels"] or not re.fullmatch(r"[A-Za-z0-9_-]+", channel):
        raise ValueError("unknown or unsafe channel name")
    context = catalog["channels"][channel]
    app_context = context["app"]
    display_name = app_context["displayName"]
    suffix = app_context["bundleSuffix"]
    icon_style = app_context["iconStyle"]
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*", suffix):
        raise ValueError("catalog app.bundleSuffix is not a safe bundle suffix")
    if icon_style not in ("play", "lab"):
        raise ValueError("catalog app.iconStyle must be play or lab")
    version_tag = getattr(args, "version_tag", None)
    if version_tag is not None and (not isinstance(version_tag, str) or not version_tag or any(c.isspace() for c in version_tag)):
        raise ValueError("version-tag must be nonempty metadata without whitespace")
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", args.commit):
        raise ValueError("commit must be a full 40- or 64-digit hexadecimal object ID")
    if not args.source_ref or any(c.isspace() for c in args.source_ref):
        raise ValueError("source-ref must be a nonempty ref without whitespace")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", args.bundle_prefix):
        raise ValueError("bundle-prefix must be a reverse-DNS bundle identifier")
    if any(args.bundle_prefix.endswith("." + value["app"]["bundleSuffix"]) for value in catalog["channels"].values()):
        raise ValueError("bundle-prefix cannot contain a reserved channel suffix")
    target = output / channel
    bundle_id = f"{args.bundle_prefix}.{suffix}"
    info = load_plist(app / "Info.plist")
    source_bundle_id = info.get("CFBundleIdentifier")
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
        if old_info.get("CFBundleIdentifier") != bundle_id or old_info.get("DusklightOutputChannel") != channel:
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
        shutil.copytree(app, staged_app, symlinks=True)
        icons = temporary / "icons"
        runner(["swift", str(Path(__file__).resolve().with_name("tvos_icons.swift")),
                "--channel", icon_style, "--output", str(icons)], check=True)
        partial = temporary / "asset-info.plist"
        runner(["xcrun", "actool", str(icons / "Assets.xcassets"),
                "--compile", str(staged_app), "--platform", "appletvos",
                "--target-device", "tv", "--minimum-deployment-target", str(info.get("MinimumOSVersion", "17.0")),
                "--app-icon", "AppIcon", "--output-partial-info-plist", str(partial)], check=True)
        if not (staged_app / "Assets.car").is_file():
            raise ValueError("actool did not produce Assets.car")
        merge_plist(info, load_plist(partial))
        info.update({"CFBundleIdentifier": bundle_id, "CFBundleName": display_name,
                     "CFBundleDisplayName": display_name, "UIDeviceFamily": [3],
                     "DusklightOutputChannel": channel, "DusklightSourceCommit": args.commit.lower(),
                     "DusklightSourceRef": args.source_ref})
        if version_tag is not None:
            info["DusklightVersionTag"] = version_tag
        (staged_app / "channel-contexts.json").write_text(json.dumps(catalog, indent=2) + "\n")
        (staged_app / "channel-output.json").write_text(json.dumps({"channel": channel}) + "\n")
        with (staged_app / "Info.plist").open("wb") as destination:
            plistlib.dump(info, destination)
        if game_image:
            shutil.copy2(game_image, staged_app / ("game" + game_image.suffix.lower()))
        bundled_images = sorted(path.name for path in staged_app.iterdir()
                                if path.is_file() and path.suffix.lower() in DISC_SUFFIXES)
        image_path = ("game" + game_image.suffix.lower()) if game_image else (bundled_images[0] if len(bundled_images) == 1 else None)
        manifest = {
            "schemaVersion": 1, "channel": channel, "bundleIdentifier": bundle_id,
            "displayName": display_name, "commit": args.commit.lower(),
            "sourceRef": args.source_ref, "sourceApp": str(app),
            "sourceBundleIdentifier": source_bundle_id,
            "sourceIdentityVerification": "caller-provided; not verified by staging",
            "executable": executable, "executableSHA256": sha256(staged_app / executable),
            "signing": "not performed; copied source signatures may be stale",
            "versionTag": version_tag, "catalogSHA256": sha256(catalog_path),
            "artifactCatalogSHA256": sha256(catalog_path),
            "context": context, "gameImageIncluded": bool(bundled_images),
            "gameImageRelativePath": image_path, "gameImageFiles": bundled_images,
        }
        (temporary / "channel-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if target.exists():
            if not args.replace or target.is_symlink():
                raise ValueError("destination changed during staging; refusing replacement")
            current = load_plist(target / "Dusklight.app" / "Info.plist")
            if current.get("CFBundleIdentifier") != bundle_id or current.get("DusklightOutputChannel") != channel:
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
    command = commands.add_parser("stage", help="stage a complete build with an initial channel context")
    command.add_argument("--channel", help="defaults to the tracked catalog defaultChannel")
    command.add_argument("--catalog", type=Path, help="optional catalog; must match the app artifact catalog")
    command.add_argument("--version-tag", help="record version metadata; creates no Git tag")
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
