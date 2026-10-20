"""One offline validation command for developer machines and CI."""
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    required = ["tools/check_repo_safety.py", "tools/run_tests.py", "tests/__init__.py",
                "okx_grid/domain.py", "okx_grid/paper.py", "okx_grid/radar.py", "okx_grid/profiles.py",
                "okx_grid/config/v5-paper-500.toml", "okx_grid/public_contracts.py", "okx_grid/automation.py", "okx_grid/telegram_preview.py", "V5_COMPATIBILITY.md"]
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
