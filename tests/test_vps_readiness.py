"""Synthetic host evidence; never inspect the user's actual VPS."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from okx_grid.domain import TradingError
from tools.inspect_vps_source import KNOWN
from tools.vps_readiness import UNITS, main, readiness, unit_status


class VPSReadinessTests(unittest.TestCase):
    def test_completed_oneshot_is_not_a_failure_and_only_show_is_called(self):
        response = SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=inactive\nSubState=dead\nType=oneshot\nResult=success\nExecMainStatus=0\nMainPID=0\nUnitFileState=static\n")
        with patch("tools.vps_readiness.subprocess.run", return_value=response) as run:
            result = unit_status("okx-grid-v5.service")
        self.assertTrue(result["completed_oneshot"])
        self.assertEqual(run.call_args.args[0][:3], ["systemctl", "show", "okx-grid-v5.service"])
        self.assertNotIn("ExecStart", str(run.call_args))
        self.assertNotIn("Environment", str(run.call_args))
        with self.assertRaises(ValueError):
            unit_status("unrelated.service")

    def test_errors_and_untrusted_properties_never_echo_raw_values(self):
        for exception in (OSError("private value"), subprocess.TimeoutExpired("private value", 5)):
            with patch("tools.vps_readiness.subprocess.run", side_effect=exception):
                self.assertEqual(unit_status(UNITS[0]), {"observed": False, "reason": "SYSTEMD_STATUS_UNAVAILABLE"})
        response = SimpleNamespace(returncode=0, stdout="ActiveState=private value\nMainPID=1\nExecStart=private value\n")
        with patch("tools.vps_readiness.subprocess.run", return_value=response), patch("tools.vps_readiness.os.readlink", return_value="/private/value"):
            result = unit_status(UNITS[0])
        self.assertNotIn("private", str(result))
        self.assertNotIn("MainPID", result)

    def test_inspector_python_and_running_python_are_not_confused(self):
        response = SimpleNamespace(returncode=0, stdout="ActiveState=active\nSubState=running\nMainPID=123\n")
        with patch("tools.vps_readiness.subprocess.run", return_value=response), patch("tools.vps_readiness.os.readlink", return_value="/usr/bin/python3.12"):
            result = unit_status(UNITS[0])
        self.assertEqual(result["python_executable"], "python3.12")
        self.assertFalse(result["completed_oneshot"])

    def test_default_offline_does_not_import_source_or_read_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for name in KNOWN:
                (directory / name).write_text("#!/bin/bash\nexit 0\n" if name.endswith(".sh") else "VALUE = 1\n")
            (directory / "telegram_commands_v5.py").write_text("raise RuntimeError('must not execute')\n")
            (directory / ".env").write_text("private environment value")
            (directory / "old.sqlite3").write_text("private ledger value")
            with patch("tools.vps_readiness.unit_status", return_value={"observed": False}), patch("tools.vps_readiness.V7MarketData", side_effect=AssertionError("no network by default")):
                result = readiness(directory)
            self.assertTrue(result["source_review_ready"])
            self.assertTrue(result["source"]["stable_between_reads"])
            self.assertEqual(result["tr_public"]["data_status"], "DATA_UNVERIFIED")
            self.assertEqual(result["host_scope"], "EXECUTION_HOST_ONLY")
            self.assertFalse(result["production_ready"])
            self.assertFalse(result["activated"])
            self.assertEqual(result["live_flag_scope"], "INSPECTOR_PROCESS_ONLY")
            self.assertEqual(result["production_live_trading"], "NOT_INSPECTED")
            self.assertNotIn("private environment value", str(result))
            self.assertNotIn("private ledger value", str(result))

    def test_changing_source_inventory_blocks_review(self):
        first = {"files": [{"path": "run_v5.sh", "sha256": "a" * 64}], "ready_for_review": True}
        second = {"files": [{"path": "run_v5.sh", "sha256": "b" * 64}], "ready_for_review": True}
        with patch("tools.vps_readiness.unit_status", return_value={"observed": False}), patch("tools.vps_readiness.inspect_source", side_effect=[(first, {}), (second, {})]):
            result = readiness(Path("/synthetic/source"))
        self.assertFalse(result["source_review_ready"])
        self.assertFalse(result["source"]["stable_between_reads"])

    def test_missing_source_has_no_paths_or_raw_exception(self):
        with patch("tools.vps_readiness.unit_status", return_value={"observed": False}), patch("tools.vps_readiness.inspect_source", side_effect=OSError("private source location")):
            result = readiness(Path("/synthetic/source"))
        self.assertEqual(result["source"]["reason"], "SOURCE_INSPECTION_FAILED")
        self.assertNotIn("private source location", str(result))

    def test_fixture_cannot_claim_real_tr_from_its_label(self):
        source = SimpleNamespace(real_transport=False, capture=lambda name: {"data_status": "VERIFIED_TR_PUBLIC"}, client=SimpleNamespace(last_response=None))
        with patch("tools.vps_readiness.unit_status", return_value={"observed": False}), patch("tools.vps_readiness.inspect_source", return_value=({"ready_for_review": True}, {})), patch("tools.vps_readiness.V7MarketData", return_value=source):
            result = readiness(Path("/synthetic/source"), allow_public_network=True)
        self.assertEqual(result["tr_public"]["data_status"], "DATA_UNVERIFIED")
        self.assertEqual(result["tr_public"]["source_origin"], "https://tr.okx.com")
        self.assertEqual(result["tr_public"]["reason"], "PUBLIC_TR_CAPTURE_FAILED")
        self.assertFalse(result["telegram_e2e_verified"])

    def test_public_failure_only_contains_safe_diagnostics(self):
        source = SimpleNamespace(capture=lambda name: (_ for _ in ()).throw(TradingError("SYNTHETIC_PRIVATE_ERROR")),
            client=SimpleNamespace(last_response={"source_origin": "https://tr.okx.com", "http_status": 403}))
        with patch("tools.vps_readiness.unit_status", return_value={"observed": False}), patch("tools.vps_readiness.inspect_source", return_value=({"ready_for_review": True}, {})), patch("tools.vps_readiness.V7MarketData", return_value=source):
            result = readiness(Path("/synthetic/source"), allow_public_network=True)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", str(result))
        self.assertEqual(result["tr_public"]["request_diagnostics"]["http_status"], 403)
        self.assertEqual(result["positions_opened"], 0)

    def test_cli_requires_source_and_rejects_missing_evidence(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            main([])
        with patch("tools.vps_readiness.unit_status", return_value={"observed": False}), patch("tools.vps_readiness.inspect_source", side_effect=OSError), patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(main(["--source", "/synthetic/source"]), 2)
        self.assertFalse(json.loads(output.getvalue())["production_ready"])

    def test_live_flag_blocks_runtime_and_network_inspection(self):
        with patch.dict("os.environ", {"LIVE_TRADING": "true"}), patch("tools.vps_readiness.unit_status", side_effect=AssertionError("no runtime read")), patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(main(["--source", "/synthetic/source", "--allow-public-network"]), 2)
        self.assertFalse(json.loads(output.getvalue())["activated"])


if __name__ == "__main__":
    unittest.main()
