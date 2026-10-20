"""Fail closed when a test directory, required suite, or test discovery is missing."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"test_domain.py", "test_paper.py", "test_safety.py", "test_release.py", "test_cli.py", "test_radar.py",
            "test_v5_profile.py", "test_public_contracts.py", "test_automation.py", "test_control.py"}


def main():
    sys.path.insert(0, str(ROOT))
    missing = REQUIRED - {p.name for p in (ROOT / "tests").glob("test_*.py")}
    if missing:
        print("Missing required suites: " + ", ".join(sorted(missing)), file=sys.stderr)
        return 1
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    if suite.countTestCases() == 0:
        print("No tests discovered.", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
