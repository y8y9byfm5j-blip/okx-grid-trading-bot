import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.__main__ import main
from tests.helpers import market_dict, proposal
from okx_grid.scenarios import sample_plan


class CommandLine(unittest.TestCase):
    def call(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(arguments)
        return result, out.getvalue(), err.getvalue()

    def test_demo_runs_without_network(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            code, output, error = self.call(["demo"])
        self.assertEqual(code, 0)
        self.assertFalse(error)
        self.assertEqual(json.loads(output)["bots"][0]["realized_pnl"], "1.817499")

    def test_health_does_not_create_state(self):
        code, output, _ = self.call(["health"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["ledger_checked"])

    def test_explicit_500_profile_is_visible_and_demo_uses_one_portfolio(self):
        code, output, error = self.call(["--profile", "v5-paper-500", "demo"])
        self.assertEqual(code, 0, error)
        values = json.loads(output)
        self.assertEqual(values["configuration"]["policy"]["equity"], "500")
        self.assertEqual(values["profile"]["name"], "v5-paper-500")
        self.assertEqual(values["portfolio"]["initial_equity"], "500")
        self.assertLess(float(values["bots"][0]["planned_stop_loss"]), 5)

    def test_profile_file_reloaded_and_revision_drift_fails_closed(self):
        from okx_grid.profiles import __file__ as profile_module
        source = Path(profile_module).parent / "config" / "v5-paper-500.toml"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = root / "profile.toml"
            configuration.write_text(source.read_text())
            prefix = ["--profile-file", str(configuration), "--state", str(root / "paper.sqlite3")]
            self.assertEqual(self.call(prefix + ["status"])[0], 0)
            configuration.write_text(source.read_text().replace("revision = 1", "revision = 2"))
            code, _, error = self.call(prefix + ["status"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(error)["error"], "PROFILE_MISMATCH")

    def test_incomplete_custom_profile_is_not_silently_a_2000_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "profile.toml"
            file.write_text('name="v5-paper-500"\n')
            code, output, error = self.call(["--profile-file", str(file), "health"])
            self.assertEqual(code, 2)
            self.assertFalse(output)
            self.assertEqual(json.loads(error)["error"], "INCOMPLETE_PAPER_PROFILE")

    def test_live_environment_cannot_enable_orders(self):
        with patch.dict("os.environ", {"TRADING_MODE": "live"}):
            code, _, error = self.call(["demo"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"], "LIVE_TRADING_DISABLED")

    def test_state_path_is_required(self):
        code, _, error = self.call(["status"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"], "STATE_PATH_REQUIRED")

    def test_invalid_input_reports_only_stable_code(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "input.json"
            file.write_text("private untrusted text")
            code, _, error = self.call(["radar", str(file)])
        self.assertEqual(code, 2)
        self.assertNotIn("private untrusted text", error)
        self.assertNotIn("Traceback", error)

    def test_radar_and_plan_from_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal()]))
            (root / "plan.json").write_text(json.dumps(sample_plan().to_dict()))
            (root / "market.json").write_text(json.dumps(market_dict()))
            with patch("okx_grid.__main__.time.time", return_value=1000):
                code, output, _ = self.call(["radar", str(root / "proposals.json")])
                self.assertEqual(code, 0)
                self.assertEqual(len(json.loads(output)["accepted"]), 1)
                code, output, _ = self.call(["plan", str(root / "plan.json"), str(root / "market.json")])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["levels"], ["98", "99", "100", "101", "102"])

    def test_persistent_open_tick_status_and_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plan.json").write_text(json.dumps(sample_plan().to_dict()))
            (root / "market.json").write_text(json.dumps(market_dict()))
            prefix = ["--state", str(root / "paper.sqlite3")]
            with patch("okx_grid.__main__.time.time", return_value=1000):
                self.assertEqual(self.call(prefix + ["open", "one", str(root / "plan.json"), str(root / "market.json")])[0], 0)
            with patch("okx_grid.__main__.time.time", return_value=1001):
                self.assertEqual(self.call(prefix + ["tick", "one", "buy", "98", "1001"])[0], 0)
            self.assertEqual(self.call(prefix + ["status"])[0], 0)
            code, output, _ = self.call(prefix + ["halt"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output)["halted"])
