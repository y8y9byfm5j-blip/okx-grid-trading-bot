import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.check_repo_safety import scan_content, scan_repository
from tools.inspect_vps_source import KNOWN, export_source, inspect_source, main


class RepositorySafety(unittest.TestCase):
    def test_private_names_and_runtime_records_are_rejected(self):
        paths = [".env", ".env.production", "state/ledger.txt", "logs/output.txt", "a/API.key", "a/x.sqlite3",
                 "runtime/orders.json", "secrets/a.py", "trades.csv", "orders.json", "a.tar.gz", "backup.bak", "id_ed25519"]
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(scan_content(path, b""))

    def test_only_explicit_synthetic_json_fixture_directory_is_allowed(self):
        self.assertFalse(scan_content("tests/fixtures/market.json", b"{}"))
        self.assertTrue(scan_content("somewhere/market.json", b"{}"))

    def test_safe_environment_template_is_allowed(self):
        self.assertFalse(scan_content(".env.example", b"TRADING_MODE=paper\nAPI_KEY=<set-locally>\n"))

    def test_embedded_assignment_detected_without_echoing_value(self):
        value = "invented-" + "credential-value"
        blob = 'API_' + 'KEY = "' + value + '"\n'
        issues = scan_content("candidate.py", blob)
        self.assertTrue(issues)
        self.assertNotIn(value, " ".join(issues))

    def test_credentials_in_python_mapping(self):
        blob = 'settings = {"api_' + 'key": "' + "invented-value-long" + '"}\n'
        self.assertTrue(scan_content("candidate.py", blob))

    def test_credentials_in_other_text_formats(self):
        blob = 'TELEGRAM_' + 'TOKEN: ' + "invented-value-long" + '\n'
        self.assertTrue(scan_content("settings.yml", blob))

    def test_private_key_header_is_detected(self):
        blob = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        self.assertTrue(scan_content("notes.txt", blob))

    def test_telegram_and_github_patterns_detected(self):
        for blob in ("123456789:" + "a" * 35, "ghp" + "_" + "a" * 36):
            self.assertTrue(scan_content("notes.md", blob))

    def test_invalid_python_and_binary_fail_closed(self):
        self.assertTrue(scan_content("bad.py", "def :"))
        self.assertTrue(scan_content("file.bin", b"\xff\xfe"))

    def test_staged_blobs_are_checked_even_if_worktree_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            file = root / "settings.txt"
            file.write_text('API_' + 'KEY=' + 'invented-value-long\n')
            subprocess.run(["git", "add", "settings.txt"], cwd=root, check=True)
            file.write_text("safe\n")
            self.assertTrue(scan_repository(root)[1])
            self.assertFalse(scan_repository(root, worktree=True)[1])

    def test_symlink_in_git_index_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            (root / "link.py").symlink_to("/etc/passwd")
            subprocess.run(["git", "add", "link.py"], cwd=root, check=True)
            self.assertTrue(scan_repository(root)[1])


class SourceIntake(unittest.TestCase):
    def seed(self, root):
        for name in KNOWN:
            (root / name).write_text("#!/bin/bash\nexit 0\n" if name.endswith(".sh") else "VALUE = 1\n")

    def test_inspection_does_not_read_env_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            (root / ".env").write_text("must stay local")
            (root / "orders.csv").write_text("private transaction records")
            report, blobs = inspect_source(root)
            self.assertTrue(report["ready_for_review"])
            self.assertEqual(set(blobs), set(KNOWN))
            self.assertNotIn("must stay local", str(report))
            self.assertTrue(all("sha256" in f for f in report["files"]))

    def test_source_is_parsed_without_executing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            (root / KNOWN[0]).write_text('raise RuntimeError("must never execute")\n')
            report, _ = inspect_source(root)
            self.assertTrue(report["ready_for_review"])

    def test_missing_sources_block_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, blobs = inspect_source(root)
            self.assertFalse(report["ready_for_review"])
            with self.assertRaises(ValueError):
                export_source(root / "export", report, blobs)

    def test_embedded_credentials_block_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            (root / KNOWN[0]).write_text('API_' + 'KEY="' + 'invented-value-long' + '"\n')
            report, blobs = inspect_source(root)
            self.assertTrue(report["issues"])
            with self.assertRaises(ValueError):
                export_source(root / "export", report, blobs)

    def test_symlink_and_hardlink_sources_are_refused(self):
        for linked in ("symlink", "hardlink"):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "outside.py").write_text("VALUE = 1\n")
                if linked == "symlink":
                    (root / KNOWN[0]).symlink_to(root / "outside.py")
                else:
                    (root / KNOWN[0]).hardlink_to(root / "outside.py")
                with self.assertRaises((ValueError, OSError)):
                    inspect_source(root)

    def test_export_is_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            report, blobs = inspect_source(root)
            destination = root / "export"
            export_source(destination, report, blobs)
            self.assertEqual(set(p.name for p in destination.iterdir()), set(KNOWN) | {"SOURCE_REVIEW.txt"})
            self.assertEqual((destination / KNOWN[0]).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                export_source(destination, report, blobs)

    def test_cli_errors_do_not_include_file_content(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as out:
            result = main([str(Path(directory) / "missing")])
            self.assertEqual(result, 2)
            self.assertEqual(out.getvalue().strip(), '{"error":"SOURCE_INSPECTION_FAILED"}')
