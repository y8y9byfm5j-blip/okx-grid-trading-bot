"""Explicit one-shot public GET smoke; no Telegram or trading. No response dumps."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from okx_grid.domain import TradingError, paper_only
from okx_grid.market_data import TRMarketData
from okx_grid.profiles import load_profile


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit live read-only OKX TR REST smoke")
    parser.add_argument("--allow-public-network", action="store_true", required=True)
    parser.add_argument("--symbol", action="append", choices=["BTC-USDT", "SOL-USDT"])
    args = parser.parse_args(argv)
    try:
        paper_only()
        service = TRMarketData(load_profile("v5-paper-500").policy)
        for instrument in args.symbol or ["BTC-USDT", "SOL-USDT"]:
            report = service.analyze(instrument)
            market = report["market"]
            print(json.dumps({k: report[k] for k in ("source_origin", "transport", "as_of_ms", "server_ts_ms", "ticker_ts_ms", "book_ts_ms", "book_age_ms", "sequence", "spread_bps", "trend", "completed_bar_end_ms", "rules", "filter_reasons")} |
                             {"ok": True, "live_enabled": False, "instrument": instrument,
                              "price": market["price"], "bid": market["bid"], "ask": market["ask"],
                              "quote_volume_24h_usdt": market["quote_volume_24h"],
                              "depth": {k: report["book"][k] for k in ("bid_levels", "ask_levels", "0.5pct", "1pct")}}, sort_keys=True))
        return 0
    except TradingError as error:
        print(json.dumps({"ok": False, "live_enabled": False, "error": error.code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
