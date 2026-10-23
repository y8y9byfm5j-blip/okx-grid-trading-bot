"""Identified, bounded TR REST captures for both V7 command and paper decisions.

Injected transports are always DATA_UNVERIFIED. Offline fixtures cannot promote
the real-data gate. This module creates no worker, bot, sender or private client.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import threading
import time

from .domain import TradingError, canonical, symbol
from .flow_contracts import discover, metric
from .market_data import book_metrics, market_dict
from .public_contracts import (PERIODS, TR_ORIGIN, _one, _rows, make_trend,
                               check_snapshot_freshness, milliseconds, normalize_market, normalize_return)
from .public_http import ReadOnlyTRClient


class V7MarketData:
    def __init__(self, policy, *, client=None, clock_ms=None):
        self.policy = policy
        self.client = client if client is not None else ReadOnlyTRClient(max_catalog_bytes=8_000_000)
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1000000
        self._cache, self._known, self._cursors = {}, {}, {}
        self._lock = threading.RLock()
        self.last_health = {"ok": False, "data_status": "DATA_UNVERIFIED", "reason": "NOT_CHECKED"}

    def _now(self):
        return milliseconds(self.clock_ms())

    @property
    def real_transport(self):
        return type(self.client) is ReadOnlyTRClient and self.client._default_transport is True

    def discover_symbols(self):
        pairs, _ = discover(self.client.fetch("instruments"))
        return sorted(p for p in pairs if p.endswith("-USDT") and pairs[p]["base"] != "USDT")

    def invalidate(self, reason="PUBLIC_CONNECTION_INTERRUPTED"):
        with self._lock:
            self._cache.clear()
            self._known.clear()
            self.last_health = {"ok": False, "data_status": "DATA_UNVERIFIED", "reason": reason,
                                "checked_at_ms": self._now()}

    def authentic(self, report):
        if not isinstance(report, dict):
            return False
        supplied = dict(report)
        sid = supplied.pop("snapshot_id", None)
        return (sid in self._known and self._known[sid] == supplied
                and hashlib.sha256(canonical(supplied).encode()).hexdigest() == sid)

    def capture(self, instrument, *, refresh=False):
        instrument = symbol(instrument)
        with self._lock:
            cached = self._cache.get(instrument)
            now = self._now()
            if cached and not refresh and 0 <= now - cached["as_of_ms"] <= 5000:
                try:
                    check_snapshot_freshness(cached, now_ms=now, policy=self.policy)
                    return deepcopy(cached)
                except TradingError:
                    pass  # Recapture at a candle boundary or expired source clock.
            try:
                result = self._capture(instrument)
                sid = hashlib.sha256(canonical(result).encode()).hexdigest()
                self._known[sid] = deepcopy(result)
                result["snapshot_id"] = sid
                self._cache[instrument] = deepcopy(result)
                # Bound process memory; pending paper plans live in the private ledger.
                self._known = dict(list(self._known.items())[-100:])
                if len(self._cache) > 100:
                    self._cache.pop(next(iter(self._cache)))
                self.last_health = {"ok": True, "data_status": result["data_status"],
                                    "checked_at_ms": result["as_of_ms"], "book_ts_ms": result["book_ts_ms"],
                                    "ticker_ts_ms": result["ticker_ts_ms"],
                                    "completed_bar_end_ms": deepcopy(result["completed_bar_end_ms"])}
                return result
            except TradingError as error:
                self.invalidate(error.code)
                raise
            except (KeyError, TypeError, ValueError, OverflowError):
                self.invalidate("INVALID_PUBLIC_DATA")
                raise TradingError("INVALID_PUBLIC_DATA") from None

    def _capture(self, instrument):
        start = self._now()
        server = milliseconds(_one(self.client.fetch("time"))["ts"])
        if abs(server - self._now()) > 2000:
            raise TradingError("PUBLIC_CLOCK_SKEW")
        instruments = self.client.fetch("instruments")
        pairs, _ = discover(instruments)
        if instrument not in pairs:
            raise TradingError("UNKNOWN_SYMBOL")
        if "BTC-USDT" not in pairs:
            raise TradingError("BTC_REGIME_UNAVAILABLE")
        replies = {b: self.client.fetch("grid_candles", instrument, bar=b) for b in PERIODS}
        btc_replies = ({b: replies[b] for b in ("1H", "4H")} if instrument == "BTC-USDT" else
                       {b: self.client.fetch("grid_candles", "BTC-USDT", bar=b) for b in ("1H", "4H")})
        tickers, books, capture = [], [], None
        for attempt in range(3):
            tickers.append(self.client.fetch("ticker", instrument))
            books.append(self.client.fetch("books", instrument))
            now = self._now()
            if not 0 <= now - start <= 10000:
                raise TradingError("PUBLIC_CAPTURE_TOO_SLOW")
            # Independent REST caches may interleave replies. Join only the
            # <=3 observations in this capture, with every original validator:
            # exact bid/ask, <=2s distance, freshness and prior cursor. No price
            # tolerance, global fallback, cross-call cache or extra GET budget.
            for book_index in reversed(range(len(books))):
                for ticker_index in reversed(range(len(tickers))):
                    ticker = tickers[ticker_index]
                    try:
                        capture = normalize_market(instrument, instruments, ticker, books[book_index],
                            source_origin=TR_ORIGIN, now_ms=now, policy=self.policy,
                            previous=self._cursors.get(instrument), enforce_policy=False)
                        alignment = {"attempts": attempt + 1, "ticker_observation": ticker_index + 1,
                                     "book_observation": book_index + 1}
                        break
                    except TradingError as error:
                        if error.code not in {"INCOHERENT_PUBLIC_TOP_OF_BOOK", "INCOHERENT_PUBLIC_SNAPSHOTS"}:
                            raise
                        alignment_error = error.code
                if capture is not None:
                    break
            if capture is not None:
                break
        if capture is None:
            raise TradingError(alignment_error)
        if len(capture.market.bids) != 400 or len(capture.market.asks) != 400:
            raise TradingError("INCOMPLETE_400_LEVEL_BOOK")
        returns, completed = {}, {}
        for bar, reply in replies.items():
            returns[bar] = normalize_return(instrument, bar, reply, source_origin=TR_ORIGIN, now_ms=now)
            rows = [r for r in _rows(reply) if r[8] == "1"]
            if len(rows) < 20 or any(int(a[0]) - int(b[0]) != PERIODS[bar] for a, b in zip(rows, rows[1:])):
                raise TradingError("INCOMPLETE_GRID_CANDLE_WINDOW")
            completed[bar] = rows[:20]
        btc = {b: normalize_return("BTC-USDT", b, r, source_origin=TR_ORIGIN, now_ms=now)
               for b, r in btc_replies.items()}
        trend = make_trend(returns["15m"], returns["1H"], btc["1H"], now_ms=now, policy=self.policy)
        trend.update(return_4h=str(returns["4H"].value), btc_return_4h=str(btc["4H"].value))
        lows = [Decimal(r[3]) for r in completed["15m"]]
        highs = [Decimal(r[2]) for r in completed["15m"]]
        previous_close, true_ranges = None, []
        for row in reversed(completed["15m"]):
            high, low, close = (Decimal(row[i]) for i in (2, 3, 4))
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)) if previous_close is not None else high - low)
            previous_close = close
        atr = sum(true_ranges) / len(true_ranges)
        self._cursors[instrument] = capture.cursor
        return {"source_origin": TR_ORIGIN, "exchange": "OKX_TR_SPOT", "transport": "REST_SNAPSHOT",
                "data_status": "VERIFIED_TR_PUBLIC" if self.real_transport else "DATA_UNVERIFIED",
                "integrity_validated": True, "live_enabled": False, "as_of_ms": now,
                "server_ts_ms": server, "ticker_ts_ms": milliseconds(_one(ticker)["ts"]),
                "book_ts_ms": capture.cursor.at_ms, "sequence": capture.cursor.sequence,
                "capture_alignment": alignment,
                "market": market_dict(capture.market), "trend": trend,
                "rules": {"tick_size": str(capture.rules.tick_size), "lot_size": str(capture.rules.lot_size),
                          "min_size": str(capture.rules.min_size)},
                "book": book_metrics(capture), "candles": completed,
                "volatility": {"atr_20_15m": metric(atr), "atr_fraction": metric(atr / capture.market.price),
                               "method": "SIMPLE_MEAN_TRUE_RANGE_20_COMPLETED_15M"},
                "completed_bar_end_ms": {b: r.end_ms for b, r in returns.items()},
                "range": {"support": str(min(lows)), "resistance": str(max(highs)),
                          "method": "PREVIOUS_20_COMPLETED_15M_EXTREMES"}}
