"""Opt-in TR public capture through isolated V5 setup; never contact Telegram.

The Telegram update is an explicitly synthetic envelope. Market data must pass
the normal real TR transport gate. No sender, tokens, private API or VPS access.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_grid.control import receipt
from okx_grid.domain import TradingError, paper_only
from okx_grid.v5_setup import create_observer


def acceptance(symbol="SOL-USDT", *, client=None, clock_ms=None):
    paper_only()
    with tempfile.TemporaryDirectory(prefix="okx-v5-public-observer-") as folder:
        with create_observer(ledger=Path(folder) / "observer.sqlite3", allowed_chat=42, allowed_user=43,
                             initial_offset=0, client=client, clock_ms=clock_ms) as runtime:
            criteria = runtime.core.criteria()
            now = runtime.core.market._now() // 1000
            update = {"update_id": 1, "message": {"date": now, "text": "/grid " + symbol,
                "chat": {"id": 42, "type": "private"}, "from": {"id": 43, "is_bot": False}}}
            runtime.adapter.consume_updates({"ok": True, "result": [update]})
            entry = receipt(runtime.trader, "v7_command:1", "v7_command")
            result = entry["result"] if entry else {}
            if not result.get("ok"):
                raise TradingError(result.get("reason", "OBSERVER_COMMAND_FAILED"))
            value = result["value"]
            if not runtime.core.market.real_transport or value["data_status"] != "VERIFIED_TR_PUBLIC":
                raise TradingError("DATA_UNVERIFIED")
            blocked = 0
            for call in (lambda: runtime.core.start(now=now),
                         lambda: runtime.core.run_cycle(cycle_id="observer_guard", now=now),
                         lambda: runtime.trader.open("observer_guard", None, None, now)):
                try:
                    call()
                except TradingError as error:
                    if error.code != "OBSERVATION_ONLY_EXECUTION_DISABLED":
                        raise
                    blocked += 1
            state = runtime.core.status()
            portfolio = state["paper"]["portfolio"]
            if (blocked != 3 or state["engine_armed"] or state["paper"]["bots"] or
                    runtime.trader._meta("v7_pending_plans") or
                    Decimal(portfolio["cash"]) != 600 or Decimal(portfolio["reserved_capital"]) != 0):
                raise TradingError("OBSERVER_EXECUTION_GUARD_FAILED")
            market = value["market_summary"]
            return {"ok": True, "scope": "ISOLATED_TEMPORARY_OBSERVER_PUBLIC_ACCEPTANCE",
                "native_update_envelope": "SYNTHETIC_TEST_INPUT", "sender_called": False,
                "telegram_verified": False, "production_activated": False, "live_enabled": False,
                "data_status": value["data_status"], "source_origin": criteria["source_origin"],
                "transport": criteria["transport"], "execution_stage": criteria["execution_stage"],
                "instrument": value["instrument"], "snapshot_id": value["snapshot_id"],
                "decision_id": value["decision_id"], "criteria_id": criteria["criteria_id"],
                "as_of_ms": value["as_of_ms"], "ticker_ts_ms": market["ticker_ts_ms"],
                "book_ts_ms": market["book_ts_ms"], "book_levels": market["book_levels"],
                "decision": {key: value[key] for key in ("action", "reasons", "planned_loss_usdt", "net_grid_returns")},
                "min_net_grid_return_percent": criteria["grid"]["min_net_grid_return_percent"],
                "command_cycle_direct_open_blocked": True, "cash_usdt": "600",
                "reserved_capital_usdt": "0", "positions_opened": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-public-network", action="store_true", required=True)
    parser.add_argument("--symbol", default="SOL-USDT", choices=("SOL-USDT", "BTC-USDT"))
    args = parser.parse_args(argv)
    try:
        result = acceptance(args.symbol)
    except TradingError as error:
        print(json.dumps({"ok": False, "data_status": "DATA_UNVERIFIED", "error": error.code,
                          "live_enabled": False, "telegram_verified": False, "production_activated": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
