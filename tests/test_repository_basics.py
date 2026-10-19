"""Baseline checks. Trading logic tests will be added after source review."""
import unittest
from pathlib import Path


class RepositoryBasics(unittest.TestCase):
    def test_live_trading_is_not_enabled_by_repository(self):
        # A repository checkout must never ship live-trading credentials.
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / ".env").exists())
        self.assertFalse((root / "state").exists())
        self.assertFalse((root / "data").exists())


if __name__ == "__main__":
    unittest.main()
