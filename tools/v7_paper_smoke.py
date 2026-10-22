"""Isolated real-TR paper safety acceptance; temporary ledger, no sender/orders.

This deliberately withholds notification health. It verifies fail-closed entry,
receipt replay, accounting and persistent halt, not successful position opening
or actual Telegram delivery. It never accepts an existing ledger path.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_grid.domain import TradingError, paper_only
from okx_grid.v7_core import V7Core
from okx_grid.v7_market import V7MarketData
from okx_grid.v7_paper import V7PaperTrader


def acceptance():
    paper_only()
    with tempfile.TemporaryDirectory(prefix="okx-v7-public-paper-") as folder:
        ledger = Path(folder) / "acceptance.sqlite3"
        with V7PaperTrader(ledger) as trader:
            market = V7MarketData(trader.policy)
            core = V7Core(trader, market)
            core.start(now=market._now() // 1000)
            decisions = []
            for name in ("BTC-USDT", "SOL-USDT"):
                decision = core.decision(name)
                duplicate = core.decision(name)
                if decision != duplicate:
                    raise TradingError("PAPER_ACCEPTANCE_DECISION_REPLAY_FAILED")
                decisions.append({key: decision[key] for key in
                    ("instrument", "snapshot_id", "decision_id", "action", "reasons", "planned_loss_usdt")})
            now = market._now() // 1000
            cycle = core.run_cycle(cycle_id="public_safety_acceptance", now=now, notification_healthy=False)
            if cycle["data_errors"] or cycle["data_mode"] != "VERIFIED_TR_PUBLIC":
                raise TradingError("PAPER_ACCEPTANCE_DATA_REJECTED")
            state = cycle["state"]
            portfolio = state["portfolio"]
            if (not state["paused"] or cycle["opened"] or state["bots"] or
                    Decimal(portfolio["initial_equity"]) != 600 or Decimal(portfolio["cash"]) != 600 or
                    Decimal(portfolio["reserved_capital"]) != 0 or Decimal(portfolio["reserved_planned_risk"]) != 0):
                raise TradingError("PAPER_ACCEPTANCE_NOTIFICATION_GATE_FAILED")
            replay = core.run_cycle(cycle_id="public_safety_acceptance", now=now, notification_healthy=False)
            if not replay["duplicate"] or replay["state"] != state:
                raise TradingError("PAPER_ACCEPTANCE_CYCLE_REPLAY_FAILED")
            trader.halt()
            stopped = trader.status()
        with V7PaperTrader(ledger) as restarted:
            if restarted.status() != stopped or not restarted.status()["halted"]:
                raise TradingError("PAPER_ACCEPTANCE_RESTART_FAILED")
            try:
                restarted.resume_entries()
            except TradingError as error:
                if error.code != "PAPER_HALTED":
                    raise
            else:
                raise TradingError("PAPER_ACCEPTANCE_HALT_BYPASSED")
        return {"ok": True, "data_status": "VERIFIED_TR_PUBLIC", "live_enabled": False,
                "scope": "ISOLATED_TEMPORARY_LEDGER_SAFETY_ACCEPTANCE", "profile": "v7-paper-600",
                "decisions": decisions, "notification_healthy": False, "entry_blocked": True,
                "decision_replay": True, "cycle_replay": True, "restart_reconciled": True,
                "halt_persisted": True, "initial_equity_usdt": "600", "cash_usdt": "600",
                "reserved_capital_usdt": "0", "positions_opened": 0,
                "telegram_verified": False, "production_activated": False,
                "two_position_live_data_acceptance": "NOT_TESTED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-public-network", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        result = acceptance()
    except TradingError as error:
        print(json.dumps({"ok": False, "data_status": "DATA_UNVERIFIED", "live_enabled": False,
                          "error": error.code}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
