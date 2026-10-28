"""One offline validation command for developer machines and CI."""
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    required = ["tools/check_repo_safety.py", "tools/run_tests.py", "tests/__init__.py",
                "okx_grid/config/v7-paper-600.toml", "okx_grid/commands.py", "okx_grid/v7_market.py",
                "okx_grid/v7_decision.py", "okx_grid/v7_paper.py", "okx_grid/v7_core.py", "okx_grid/telegram_v7.py", "okx_grid/v5_owner_bridge.py", "docs/V5_OWNER_BRIDGE.md", "tools/v7_smoke.py", "tools/v7_paper_smoke.py",
                "tests/test_v5_owner_bridge.py", "tests/test_v7_owner_acceptance.py", "tests/test_paper_acceptance.py", "tests/test_v7_flow_tracking.py", "docs/V7_LIVE_FLOW.md",
                "okx_grid/domain.py", "okx_grid/paper.py", "okx_grid/radar.py", "okx_grid/profiles.py",
                "okx_grid/config/v5-paper-500.toml", "okx_grid/public_contracts.py", "okx_grid/public_http.py", "okx_grid/market_data.py", "okx_grid/flow_contracts.py", "okx_grid/money_flow.py", "okx_grid/telegram_flow.py", "okx_grid/flow_replay.py", "tools/flow_smoke.py", "okx_grid/telegram_market.py", "tools/public_smoke.py", "okx_grid/automation.py", "okx_grid/control.py", "okx_grid/cycle.py", "okx_grid/telegram_control.py", "V5_COMPATIBILITY.md"]
    required += ["okx_grid/v5_owner_adapter.py", "tests/test_v5_owner_adapter.py",
                 "tools/vps_readiness.py", "tests/test_vps_readiness.py", "okx_grid/v5_setup.py",
                 "tests/test_manual_setup.py", "tools/v5_observer_smoke.py", "docs/MANUAL_OBSERVER_SETUP.md"]
    required += ["tests/test_source_risk_regressions.py"]
    required += ["okx_grid/live_bot.py", "okx_grid/grid_manager.py", "okx_grid/tr_private.py", "okx_grid/tr_public.py",
                 "okx_grid/rotation.py", "okx_grid/early_long.py", "okx_grid/live_reports.py", "tests/test_live_bot.py",
                 "deploy/install.sh", "deploy/okx-tr-bot.service", "docs/LIVE_BOT.md",
                 "deploy/kur.sh", "okx_grid/setup_helper.py", "go",
                 "okx_grid/actions_run.py", ".github/workflows/telegram-bot.yml", "docs/GITHUB_BOT.md", "token", "teslim", "okx_grid/delivery.py", "okx_grid/range_grid.py"]
    if any(not (ROOT / p).is_file() for p in required):
        print("Required validation or application file missing.", file=sys.stderr)
        return 1
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    try:
        with tempfile.TemporaryDirectory(prefix="okx-compile-") as temp:
            for i, path in enumerate(filter(None, tracked)):
                if path.endswith(".py"):
                    py_compile.compile(str(ROOT / path), cfile=str(Path(temp) / f"{i}.pyc"), doraise=True)
                elif path.endswith(".sh"):
                    subprocess.run(["bash", "-n", str(ROOT / path)], check=True)
        subprocess.run([sys.executable, "tools/check_repo_safety.py", "--worktree"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "tools/run_tests.py"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "okx_grid", "demo"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    except (OSError, py_compile.PyCompileError, subprocess.CalledProcessError):
        print("Validation failed.", file=sys.stderr)
        return 1
    print("Offline validation passed; no broker or Telegram calls were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
