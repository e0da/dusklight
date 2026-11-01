import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "release_channels", Path(__file__).resolve().parents[1] / "tools/release_channels.py"
)
releases = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(releases)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write(releases.POINTERS, {
            "schemaVersion": 1, "channels": {"play": None, "lab": None},
        })
        self.write(releases.CATALOG, {
            "schemaVersion": 1, "channels": {"play": {}, "lab": {}},
        })
        self.commit = self.save("initial")
        self.tag = "midna/v1.2.3"
        self.git("tag", "-a", self.tag, "-m", "Selected release")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL,
        ).decode().strip()

    def write(self, path, value):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))

    def save(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def prepare(self, metadata=None):
        return releases.prepare(self.repo, self.tag, self.commit, metadata)

    def test_prepare_is_idempotent_and_does_not_promote(self):
        refs = self.git("show-ref", "--tags")
        first = self.prepare({"platform": "tvos"})
        self.assertEqual(first, self.prepare({"platform": "tvos"}))
        self.assertEqual(releases.load_pointers(self.repo)["channels"], {"play": None, "lab": None})
        self.assertEqual(refs, self.git("show-ref", "--tags"))
        self.assertEqual(first["catalogBlobSha"], self.git("rev-parse", f"{self.commit}:{releases.CATALOG}"))
        self.assertEqual(releases.record_path(self.repo, self.tag).name, "midna%2Fv1.2.3.json")

    def test_same_version_cannot_change_metadata(self):
        self.prepare({"artifact": "one"})
        path = releases.record_path(self.repo, self.tag)
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            self.prepare({"artifact": "two"})
        self.assertEqual(original, path.read_bytes())

    def test_missing_tag_and_mismatched_or_abbreviated_commit(self):
        for tag, commit in (("midna/v9.9.9", self.commit),
                            (self.tag, "0" * 40), (self.tag, self.commit[:8])):
            with self.subTest(tag=tag, commit=commit), self.assertRaises(ValueError):
                releases.prepare(self.repo, tag, commit)
        self.assertFalse((self.repo / "releases/versions").exists())

    def test_rejects_malformed_tags_and_metadata(self):
        for tag in ("../escape", "midna/v01.0.0", "midna/v1.0", "midna/v1.0.0-01", "midna/v1.0.0\n"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                releases.record_path(self.repo, tag)
        for metadata in ([], "artifact"):
            with self.assertRaises(ValueError):
                self.prepare(metadata)
        releases.validate_tag("midna/v0.2.0-rc.1+build.42")

    def test_promotion_is_selective_and_uses_release_catalog(self):
        record = self.prepare()
        self.write(releases.CATALOG, {"schemaVersion": 1, "channels": {"other": {}}})
        self.save("later catalog removes lab")
        selected = releases.promote(self.repo, "lab", self.tag)
        self.assertIsNone(selected["channels"]["play"])
        self.assertEqual(selected["channels"]["lab"], {
            key: record[key] for key in ("tag", "commit", "catalogBlobSha")
        })
        self.assertEqual(selected, releases.promote(self.repo, "lab", self.tag))

    def test_channel_must_exist_at_tag_even_if_present_now(self):
        self.write(releases.CATALOG, {"schemaVersion": 1, "channels": {"play": {}}})
        self.commit = self.save("play only")
        self.tag = "midna/v2.0.0"
        self.git("tag", "-a", self.tag, "-m", "Play only")
        self.prepare()
        self.write(releases.CATALOG, {"schemaVersion": 1, "channels": {"play": {}, "lab": {}}})
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "lab", self.tag)
        self.assertIsNone(releases.load_pointers(self.repo)["channels"]["lab"])

    def test_catalog_can_add_a_third_channel_through_cli(self):
        self.write(releases.CATALOG, {
            "schemaVersion": 1, "channels": {"play": {}, "lab": {}, "preview_2": {}},
        })
        self.commit = self.save("add preview channel")
        self.tag = "midna/v2.1.0"
        self.git("tag", "-a", self.tag, "-m", "Preview release")
        record = self.prepare()
        before = releases.promote(self.repo, "play", self.tag)
        result = subprocess.check_output([
            "python3", str(Path(releases.__file__)), "--repo", str(self.repo),
            "promote", "--channel", "preview_2", "--tag", self.tag,
        ])
        pointers = json.loads(result)["channels"]
        self.assertEqual(pointers["play"], before["channels"]["play"])
        self.assertIsNone(pointers["lab"])
        self.assertEqual(pointers["preview_2"], {
            key: record[key] for key in ("tag", "commit", "catalogBlobSha")
        })
        self.assertEqual(releases.load_pointers(self.repo)["channels"], pointers)

    def test_unknown_or_unsafe_channel_cannot_change_pointers(self):
        self.prepare()
        before = (self.repo / releases.POINTERS).read_bytes()
        for channel in ("preview", "", "../play", "play lab", "play\n"):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                releases.promote(self.repo, channel, self.tag)
        self.assertEqual(before, (self.repo / releases.POINTERS).read_bytes())
        self.write(releases.POINTERS, {"schemaVersion": 1, "channels": {"": None}})
        with self.assertRaises(ValueError):
            releases.load_pointers(self.repo)

    def test_moved_tag_rejected_by_prepare_and_promote(self):
        self.prepare()
        self.write("another.json", {})
        newer = self.save("newer")
        self.git("tag", "-fa", self.tag, newer, "-m", "Moved")
        for operation in (
            lambda: self.prepare(),
            lambda: releases.prepare(self.repo, self.tag, newer),
            lambda: releases.promote(self.repo, "play", self.tag),
        ):
            with self.assertRaises(ValueError):
                operation()

    def test_reannotated_tag_same_commit_rejected(self):
        self.prepare()
        self.git("tag", "-fa", self.tag, self.commit, "-m", "Different annotation")
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "play", self.tag)

    def test_missing_record_deleted_tag_and_changed_catalog_hash_rejected(self):
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "play", self.tag)
        record = self.prepare()
        record["catalogBlobSha"] = "0" * 40
        self.write(str(releases.record_path(self.repo, self.tag).relative_to(self.repo)), record)
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "play", self.tag)
        self.git("tag", "-d", self.tag)
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "play", self.tag)

    def test_malformed_catalog_record_and_pointer_rejected(self):
        self.write(releases.CATALOG, {"schemaVersion": True, "channels": {}})
        self.commit = self.save("invalid catalog")
        self.tag = "midna/v3.0.0"
        self.git("tag", self.tag)
        with self.assertRaises(ValueError):
            self.prepare()
        self.write(str(releases.record_path(self.repo, self.tag).relative_to(self.repo)), [])
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "play", self.tag)
        self.write(releases.POINTERS, {"schemaVersion": 1, "channels": []})
        with self.assertRaises(ValueError):
            releases.load_pointers(self.repo)

    def test_malformed_existing_pointer_is_not_carried_into_promotion(self):
        self.prepare()
        self.write(releases.POINTERS, {
            "schemaVersion": 1,
            "channels": {"play": {"tag": "bad", "commit": 42, "catalogBlobSha": "bad"},
                         "lab": None},
        })
        before = (self.repo / releases.POINTERS).read_bytes()
        with self.assertRaises(ValueError):
            releases.promote(self.repo, "lab", self.tag)
        self.assertEqual(before, (self.repo / releases.POINTERS).read_bytes())

    def test_cli_status_and_missing_tag_leave_repository_unchanged(self):
        script = Path(releases.__file__)
        before = self.git("status", "--porcelain")
        command = ["python3", str(script), "--repo", str(self.repo)]
        status = subprocess.check_output([*command, "status"])
        self.assertEqual(json.loads(status)["channels"], {"play": None, "lab": None})
        rejected = subprocess.run(
            [*command, "prepare", "--tag", "midna/v99.0.0", "--commit", self.commit],
            capture_output=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(before, self.git("status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
