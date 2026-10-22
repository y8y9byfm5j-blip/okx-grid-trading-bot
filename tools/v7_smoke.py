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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-public-network", action="store_true", required=True)
    parser.add_argument("--symbol", default="BTC-USDT")
    args = parser.parse_args(argv)
    try:
        paper_only()
        service = V7MarketData(load_profile("v7-paper-600").policy)
        report = service.capture(args.symbol)
        if report["data_status"] != "VERIFIED_TR_PUBLIC":
            raise TradingError("DATA_UNVERIFIED")
        print(json.dumps({k: report[k] for k in ("source_origin", "exchange", "transport", "data_status",
            "snapshot_id", "as_of_ms", "server_ts_ms", "ticker_ts_ms", "book_ts_ms", "sequence", "completed_bar_end_ms")} |
            {"ok": True, "live_enabled": False, "bid_levels": report["book"]["bid_levels"],
             "ask_levels": report["book"]["ask_levels"], "book_age_ms": report["as_of_ms"] - report["book_ts_ms"]}, sort_keys=True))
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
