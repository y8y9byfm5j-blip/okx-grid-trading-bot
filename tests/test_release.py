import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.release import MANIFEST, REQUIRED, build, read_archive, safe_path, stage


def fixture_archive(root, extra=None, mode="paper", link=False, duplicate=False):
    files = {name: b'"""Synthetic release fixture."""\n' for name in REQUIRED}
    files.update(extra or {})
    manifest = {"format": 1, "commit": "a" * 40, "mode": mode, "activation": "disabled",
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    entries = [*files.items(), (MANIFEST, json.dumps(manifest).encode())]
    if duplicate:
        entries.append(entries[0])
    output = root / "fixture.tar.gz"
    with tarfile.open(output, "w:gz") as archive:
        for name, content in entries:
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            if link and name == next(iter(files)):
                entry.type, entry.linkname, entry.size = tarfile.SYMTYPE, "/etc/passwd", 0
                archive.addfile(entry)
            else:
                archive.addfile(entry, io.BytesIO(content))
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


class ReleaseSafety(unittest.TestCase):
    def test_member_path_validation(self):
        for value in ("../evil.py", "/tmp/evil.py", "okx_grid/../../evil.py", "okx_grid//a.py", "./a.py", "a\\b.py"):
            with self.subTest(value=value):
                self.assertFalse(safe_path(value))

    def test_valid_archive_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory))
            manifest, files = read_archive(archive, digest)
            self.assertEqual(manifest["mode"], "paper")
            self.assertTrue(REQUIRED <= files.keys())

    def test_wrong_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = fixture_archive(Path(directory))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                read_archive(archive, "0" * 64)

    def test_path_traversal_archive_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory), {"../escape.py": b"pass\n"})
            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                read_archive(archive, digest)

    def test_symlink_archive_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory), link=True)
            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                read_archive(archive, digest)

    def test_duplicate_member_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory), duplicate=True)
            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                read_archive(archive, digest)

    def test_live_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory), mode="live")
            with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                read_archive(archive, digest)

    def test_private_runtime_file_never_enters_release(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory), {"tests/orders.csv": b"private records"})
            with self.assertRaisesRegex(ValueError, "unsafe archive content"):
                read_archive(archive, digest)

    def test_protected_vps_tree_is_never_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, digest = fixture_archive(Path(directory))
            for root in ("/opt/okx-grid-demo", "/opt/okx-grid-demo/v5", "/opt"):
                with self.subTest(root=root), self.assertRaisesRegex(ValueError, "protected live path"):
                    stage(archive, digest, Path(root))

    def test_failed_health_check_leaves_no_release_or_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest = fixture_archive(root)
            target = root / "staging"
            with patch("tools.release.subprocess.run", side_effect=subprocess.CalledProcessError(1, "health")):
                with self.assertRaises(subprocess.CalledProcessError):
                    stage(archive, digest, target)
            self.assertFalse((target / ".stage.lock").exists())
            self.assertEqual(list((target / "releases").iterdir()), [])

    def test_stage_never_activates_or_replaces_existing_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest = fixture_archive(root)
            target = root / "staging"
            with patch("tools.release.subprocess.run") as run:
                result = stage(archive, digest, target)
            self.assertTrue(result["staged"])
            self.assertFalse(result["activated"])
            self.assertEqual(run.call_count, 3)
            self.assertFalse((target / "current").exists())
            self.assertTrue(all("systemctl" not in str(call) for call in run.call_args_list))
            with self.assertRaisesRegex(ValueError, "already staged"):
                stage(archive, digest, target)

    def test_staging_parent_symlink_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest = fixture_archive(root)
            (root / "alias").symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                stage(archive, digest, root / "alias" / "staging")

    def test_clean_commit_build_contains_no_untracked_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            for name in REQUIRED:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('"""Synthetic build fixture."""\n')
            (root / ".gitignore").write_text("state/\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], cwd=root, check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
            (root / "state").mkdir()
            (root / "state" / "orders.csv").write_text("private local records")
            archive = Path(directory) / "release.tar.gz"
            result = build(commit, archive, root=root, validate=False)
            _, files = read_archive(archive, result["sha256"])
            self.assertNotIn("state/orders.csv", files)
            (root / "untracked.py").write_text("pass\n")
            with self.assertRaisesRegex(ValueError, "clean checkout"):
                build(commit, Path(directory) / "dirty.tar.gz", root=root, validate=False)

    def test_non_head_commit_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as directory, patch("tools.release.git", return_value=b"b" * 40 + b"\n"):
            with self.assertRaisesRegex(ValueError, "exact HEAD"):
                build("a" * 40, Path(directory) / "out.tar.gz", validate=False)
