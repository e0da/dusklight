import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import channel_context, tvos_channels as channels



class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input" / "Midna.app"
        self.source.mkdir(parents=True)
        self.original = {
            "CFBundleExecutable": "Midna", "CFBundleIdentifier": "org.example.midna",
            "MinimumOSVersion": "17.0", "UIDeviceFamily": [1, 2],
            "CFBundleIcons": {"existing": "preserved", "CFBundlePrimaryIcon": "old"},
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        }
        self.write_info(self.source, self.original)
        (self.source / "Midna").write_bytes(b"compiled game executable\x00")
        (self.source / "_CodeSignature").mkdir()
        (self.source / "_CodeSignature" / "CodeResources").write_text("old signature")
        (self.source / "embedded.mobileprovision").write_text("private profile")
        (self.source / "channel-catalog.json").write_text(channel_context.DEFAULT_CATALOG.read_text())
        self.calls = []
        self.resolved = {
            "schemaVersion": 1, "defaultChannel": "play", "channels": {
                "play": {"app": {"displayName": "Dusklight Play", "bundleSuffix": "play", "iconStyle": "play"},
                         "features": {"diagnostics": {"liveControl": {"enabled": False}}}},
                "lab": {"app": {"displayName": "Dusklight Lab", "bundleSuffix": "lab", "iconStyle": "lab"},
                        "features": {"diagnostics": {"liveControl": {"enabled": True}}}},
            },
        }
        mocked = patch.object(channels.channel_context, "resolve_catalog", return_value=self.resolved)
        mocked.start()
        self.addCleanup(mocked.stop)

    def write_info(self, app, value):
        with (app / "Info.plist").open("wb") as destination:
            plistlib.dump(value, destination)

    def args(self, channel="lab", **changes):
        values = dict(channel=channel, app=self.source, output=self.root / "output",
                      commit="abc12345" * 5, source_ref="feature/work",
                      bundle_prefix="dev.e0da.dusklight", game_image=None, replace=False)
        values.update(changes)
        return argparse.Namespace(**values)

    def runner(self, command, check):
        self.assertTrue(check)
        self.calls.append(command)
        if command[0] == "swift":
            icons = Path(command[command.index("--output") + 1])
            (icons / "Assets.xcassets").mkdir(parents=True)
        elif command[:2] == ["xcrun", "actool"]:
            self.assertEqual(command[command.index("--app-icon") + 1], "AppIcon")
            self.assertTrue(Path(command[2]).is_dir())
            destination = Path(command[command.index("--compile") + 1])
            (destination / "Assets.car").write_bytes(b"compiled assets")
            partial = Path(command[command.index("--output-partial-info-plist") + 1])
            with partial.open("wb") as output:
                plistlib.dump({"CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconName": "AppIconSmall"}},
                              "TVTopShelfImage": {"TVTopShelfPrimaryImage": "TopShelf"}}, output)
        else:
            self.fail(f"unexpected tool: {command[0]}")

    def test_channels_have_separate_identity_and_preserve_compiled_provenance(self):
        for channel in ("play", "lab"):
            staged = channels.stage(self.args(channel), self.runner)
            info = channels.load_plist(staged / "Info.plist")
            self.assertEqual(info["CFBundleIdentifier"], "dev.e0da.dusklight." + channel)
            self.assertEqual(info["CFBundleDisplayName"], "Dusklight " + channel.title())
            self.assertEqual(info["UIDeviceFamily"], [3])
            manifest = json.loads((staged.parent / "channel-manifest.json").read_text())
            self.assertEqual(manifest["commit"], "abc12345" * 5)
            self.assertEqual(manifest["sourceRef"], "feature/work")
            self.assertEqual(manifest["sourceApp"], str(self.source.resolve()))
            self.assertEqual(manifest["sourceBundleIdentifier"], "org.example.midna")
            self.assertEqual(manifest["executableSHA256"], hashlib.sha256((self.source / "Midna").read_bytes()).hexdigest())
            self.assertIn("not verified", manifest["sourceIdentityVerification"])
            self.assertIn("not performed", manifest["signing"])
            self.assertTrue((staged / "_CodeSignature").exists())
            self.assertTrue((staged / "embedded.mobileprovision").exists())
            self.assertEqual(json.loads((staged / "channel-contexts.json").read_text()), self.resolved)
            self.assertEqual(info["DusklightOutputChannel"], channel)
            self.assertEqual(json.loads((staged / "channel-output.json").read_text()), {"channel": channel})
        self.assertEqual(channels.load_plist(self.source / "Info.plist"), self.original)
        self.assertTrue((self.source / "embedded.mobileprovision").exists())

    def test_generated_nested_icon_keys_override_template_and_preserve_neighbors(self):
        staged = channels.stage(self.args(), self.runner)
        info = channels.load_plist(staged / "Info.plist")
        self.assertEqual(info["CFBundleIcons"]["CFBundlePrimaryIcon"], {"CFBundleIconName": "AppIconSmall"})
        self.assertEqual(info["CFBundleIcons"]["existing"], "preserved")
        self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsLocalNetworking"])
        self.assertIn("TVTopShelfImage", info)

    def test_no_overwrite_and_explicit_replacement_preserves_other_channel(self):
        play = channels.stage(self.args("play"), self.runner)
        staged = channels.stage(self.args(), self.runner)
        marker = staged / "old-marker"
        marker.write_text("old")
        with self.assertRaisesRegex(ValueError, "--replace"):
            channels.stage(self.args(), self.runner)
        self.assertTrue(marker.exists())
        channels.stage(self.args(replace=True), self.runner)
        self.assertFalse(marker.exists())
        self.assertTrue(play.exists())
        info = channels.load_plist(staged / "Info.plist")
        info["DusklightOutputChannel"] = "play"
        self.write_info(staged, info)
        with self.assertRaisesRegex(ValueError, "wrong channel"):
            channels.stage(self.args(replace=True), self.runner)

    def test_failed_tools_do_not_replace_working_channel(self):
        staged = channels.stage(self.args(), self.runner)
        original = (staged / "Info.plist").read_bytes()
        def fail(command, check):
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            channels.stage(self.args(replace=True), fail)
        self.assertEqual((staged / "Info.plist").read_bytes(), original)
        self.assertEqual(list(staged.parent.parent.glob(".lab-stage-*")), [])

    def test_overlap_ref_and_reserved_channel_guards_run_before_tools(self):
        for args in (self.args(output=self.source / "output"), self.args(output=self.root),
                     self.args(channel="unknown"), self.args(commit="abc1234"),
                     self.args(bundle_prefix="dev.e0da.dusklight.play")):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    channels.stage(args, self.runner)
        self.assertEqual(self.calls, [])

    def test_private_image_name_is_stable_and_manifest_records_it(self):
        image = self.root / "private-original.ISO"
        image.write_bytes(b"private image fixture")
        staged = channels.stage(self.args(game_image=image), self.runner)
        self.assertEqual((staged / "game.iso").read_bytes(), image.read_bytes())
        manifest = json.loads((staged.parent / "channel-manifest.json").read_text())
        self.assertEqual(manifest["gameImageRelativePath"], "game.iso")
        self.assertNotIn(str(image), json.dumps(manifest))

    def test_game_image_output_in_unignored_repository_is_rejected(self):
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
        image = self.root / "private.rvz"
        image.write_bytes(b"fixture")
        with self.assertRaisesRegex(ValueError, "Git-ignored"):
            channels.stage(self.args(output=repository / "output", game_image=image), self.runner)
        (repository / ".gitignore").write_text("output/\n")
        staged = channels.stage(self.args(output=repository / "output", game_image=image), self.runner)
        self.assertTrue((staged / "game.rvz").exists())

    def test_bundled_game_image_also_requires_ignored_repository_output(self):
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
        (self.source / "game.rvz").write_bytes(b"already bundled fixture")
        with self.assertRaisesRegex(ValueError, "Git-ignored"):
            channels.stage(self.args(output=repository / "output"), self.runner)
        self.assertEqual(self.calls, [])

    def test_symlink_alias_cannot_bypass_source_overlap(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "overlap"):
            channels.stage(self.args(output=alias / "stage"), self.runner)
        self.assertEqual(self.calls, [])

    def test_catalog_default_identity_icon_style_and_version_metadata_are_independent(self):
        self.resolved["defaultChannel"] = "lab"
        self.resolved["channels"]["lab"]["app"].update({"bundleSuffix": "experiment", "displayName": "Catalog Lab", "iconStyle": "play"})
        staged = channels.stage(self.args(channel=None, source_ref="any/build/ref", version_tag="v1.2.3"), self.runner)
        info = channels.load_plist(staged / "Info.plist")
        self.assertEqual(info["CFBundleIdentifier"], "dev.e0da.dusklight.experiment")
        self.assertEqual(info["CFBundleDisplayName"], "Catalog Lab")
        self.assertEqual(info["DusklightVersionTag"], "v1.2.3")
        self.assertEqual(info["DusklightOutputChannel"], "lab")
        self.assertEqual(self.calls[0][self.calls[0].index("--channel") + 1], "play")
        manifest = json.loads((staged.parent / "channel-manifest.json").read_text())
        self.assertEqual(manifest["versionTag"], "v1.2.3")
        self.assertEqual(manifest["sourceRef"], "any/build/ref")

    def test_requires_artifact_catalog_and_rejects_explicit_mismatch(self):
        artifact = self.source / "channel-catalog.json"
        value = json.loads(artifact.read_text())
        supplied = self.root / "explicit.json"
        supplied.write_text(json.dumps(value, separators=(",", ":")))
        staged = channels.stage(self.args(catalog=supplied), self.runner)
        manifest = json.loads((staged.parent / "channel-manifest.json").read_text())
        self.assertEqual(manifest["artifactCatalogSHA256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
        value["defaultChannel"] = "lab"
        supplied.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "differs"):
            channels.stage(self.args(catalog=supplied, replace=True), self.runner)
        artifact.unlink()
        with self.assertRaisesRegex(ValueError, "context-aware"):
            channels.stage(self.args(replace=True), self.runner)


if __name__ == "__main__":
    unittest.main()
