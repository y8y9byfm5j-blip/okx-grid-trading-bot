"""Explicit TR public-only V7 capture probe. No state, Telegram or private API."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from okx_grid.domain import TradingError, paper_only
from okx_grid.profiles import load_profile
from okx_grid.v7_market import V7MarketData
from okx_grid.v7_decision import PlanningPolicy, decide


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-public-network", action="store_true", required=True)
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--include-plan", action="store_true", help="Evaluate the shared 300 USDT paper plan without opening positions")
    args = parser.parse_args(argv)
    try:
        paper_only()
        service = V7MarketData(load_profile("v7-paper-600").policy)
        report = service.capture(args.symbol)
        if report["data_status"] != "VERIFIED_TR_PUBLIC":
            raise TradingError("DATA_UNVERIFIED")
        summary = ({k: report[k] for k in ("source_origin", "exchange", "transport", "data_status",
            "snapshot_id", "as_of_ms", "server_ts_ms", "ticker_ts_ms", "book_ts_ms", "sequence", "completed_bar_end_ms", "capture_alignment")} |
            {"ok": True, "live_enabled": False, "bid_levels": report["book"]["bid_levels"],
             "ask_levels": report["book"]["ask_levels"], "book_age_ms": report["as_of_ms"] - report["book_ts_ms"]})
        if args.include_plan:
            decision = decide(report, load_profile("v7-paper-600"), PlanningPolicy(), now_ms=service._now())
            summary["decision"] = {k: decision[k] for k in ("decision_id", "snapshot_id", "configuration_id", "action",
                "eligible", "scoring", "reasons", "net_grid_returns", "planned_loss_usdt", "risk_caps", "fee_model", "stop_note")}
            summary["positions_opened"] = 0
        print(json.dumps(summary, sort_keys=True))
        return 0
    except TradingError as error:
        diagnostics = getattr(getattr(locals().get("service"), "client", None), "last_response", None)
        summary = {"ok": False, "data_status": "DATA_UNVERIFIED", "live_enabled": False, "error": error.code}
        if isinstance(diagnostics, dict):
            summary["request_diagnostics"] = diagnostics
        print(json.dumps(summary))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
