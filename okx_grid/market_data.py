"""TR REST analysis pipeline; inert until called, no ledger mutations or orders."""
from dataclasses import asdict
from decimal import Decimal
import threading
import time

from .domain import TradingError, build_plan, number, positive, precise, symbol
from .public_contracts import (TR_ORIGIN, _fresh, _one, _rows, make_trend, milliseconds,
                               normalize_market, normalize_return)
from .public_http import ReadOnlyTRClient
from .radar import screen

LABEL = "CANLI PİYASA VERİSİ — SANAL İŞLEM (PAPER)"


def market_dict(market):
    data = asdict(market)
    for key in ("price", "bid", "ask", "quote_volume_24h"):
        data[key] = str(data[key])
    for key in ("bids", "asks"):
        data[key] = [[str(p), str(q)] for p, q in data[key]]
    return data


@precise
def book_metrics(capture):
    market = capture.market
    mid = (market.bid + market.ask) / 2
    result = {"requested_levels_per_side": 400, "bid_levels": len(market.bids), "ask_levels": len(market.asks),
              "reference_mid": str(mid), "unit": "USDT notional", "depth_is_truncated": True}
    for name, distance in (("0.5pct", Decimal("0.005")), ("1pct", Decimal("0.01"))):
        bid = sum((p * q for p, q in market.bids if p >= mid * (1 - distance)), Decimal(0))
        ask = sum((p * q for p, q in market.asks if p <= mid * (1 + distance)), Decimal(0))
        result[name] = {"bid": str(bid), "ask": str(ask), "imbalance": str((bid - ask) / (bid + ask)) if bid + ask else None,
                        "bid_band_covered": market.bids[-1][0] <= mid * (1 - distance),
                        "ask_band_covered": market.asks[-1][0] >= mid * (1 + distance)}
    result["top5"] = {side: {"levels": [[str(p), str(q)] for p, q in levels[:5]],
                             "notional": str(sum((p * q for p, q in levels[:5]), Decimal(0)))}
                      for side, levels in (("bids", market.bids), ("asks", market.asks))}
    result["observed_walls"] = {side: [{"price": str(p), "quantity_base": str(q), "notional_usdt": str(p * q)}
                                      for p, q in sorted(levels, key=lambda row: row[0] * row[1], reverse=True)[:3]]
                                for side, levels in (("bids", market.bids), ("asks", market.asks))}
    return result


class TRMarketData:
    """One owner should reuse this object; cursor monotonicity is process-local.

    No parallel scheduler is created. V5 integration requires verified source
    contracts and must invoke this from its existing single consumer.
    """
    def __init__(self, policy, *, client=None, clock_ms=None):
        self.policy = policy
        self.client = client if client is not None else ReadOnlyTRClient(max_catalog_bytes=8_000_000)
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        self.cursors = {}
        self.last_health = {"ok": False, "reason": "NOT_CHECKED", "live_enabled": False}
        self._lock = threading.RLock()

    def _now(self):
        return milliseconds(self.clock_ms())

    def analyze(self, instrument):
        instrument = symbol(instrument)
        with self._lock:
            try:
                result = self._analyze(instrument)
                self.last_health = {"ok": True, "instrument": instrument, "book_ts_ms": result["book_ts_ms"],
                                    "checked_at_ms": result["as_of_ms"], "live_enabled": False}
                return result
            except TradingError as error:
                self.last_health = {"ok": False, "reason": error.code, "checked_at_ms": self._now(), "live_enabled": False}
                raise
            except (KeyError, TypeError, ValueError, OverflowError):
                self.last_health = {"ok": False, "reason": "INVALID_PUBLIC_DATA", "checked_at_ms": self._now(), "live_enabled": False}
                raise TradingError("INVALID_PUBLIC_DATA") from None

    def _analyze(self, instrument):
        started = self._now()
        server = milliseconds(_one(self.client.fetch("time"))["ts"])
        checked = self._now()
        if checked < started or checked - started > 2000 or not started - 2000 <= server <= checked + 2000:
            raise TradingError("PUBLIC_CLOCK_SKEW")
        instruments = self.client.fetch("instruments")
        for required in {instrument, "BTC-USDT"}:
            matches = [r for r in _rows(instruments) if isinstance(r, dict) and r.get("instId") == required]
            if len(matches) != 1 or matches[0].get("state") != "live" or matches[0].get("instType") != "SPOT" or matches[0].get("quoteCcy") != "USDT":
                raise TradingError("INSTRUMENT_NOT_AVAILABLE_IN_TR_CAPTURE")
        short_reply = self.client.fetch("candles", instrument, bar="15m")
        hour_reply = self.client.fetch("candles", instrument, bar="1H")
        btc_reply = hour_reply if instrument == "BTC-USDT" else self.client.fetch("candles", "BTC-USDT", bar="1H")
        ticker = self.client.fetch("ticker", instrument)
        books = self.client.fetch("books", instrument)
        now = self._now()
        if now < checked or now - started > 10000:
            raise TradingError("PUBLIC_CAPTURE_TOO_SLOW")
        capture = normalize_market(instrument, instruments, ticker, books, source_origin=TR_ORIGIN, now_ms=now,
                                   policy=self.policy, previous=self.cursors.get(instrument), enforce_policy=False)
        short = normalize_return(instrument, "15m", short_reply, source_origin=TR_ORIGIN, now_ms=now)
        hour = normalize_return(instrument, "1H", hour_reply, source_origin=TR_ORIGIN, now_ms=now)
        btc = hour if instrument == "BTC-USDT" else normalize_return("BTC-USDT", "1H", btc_reply, source_origin=TR_ORIGIN, now_ms=now)
        trend = make_trend(short, hour, btc, now_ms=now, policy=self.policy)
        reasons = []
        try:
            self.policy.check_market(capture.market, now // 1000, Decimal(0))
        except TradingError as error:
            reasons.append(error.code)
        if short.value < 0 or hour.value < 0:
            reasons.append("DOWNTREND")
        if btc.value <= Decimal("-0.02"):
            reasons.append("BTC_RISK_OFF")
        if short.value > Decimal("0.05"):
            reasons.append("EXCESSIVE_SHORT_MOMENTUM")
        self.cursors[instrument] = capture.cursor
        return {"label": LABEL, "source_origin": TR_ORIGIN, "transport": "REST_SNAPSHOT", "live_enabled": False,
                "as_of_ms": now, "server_ts_ms": server, "ticker_ts_ms": milliseconds(_one(ticker)["ts"]),
                "book_ts_ms": capture.cursor.at_ms, "book_age_ms": now - capture.cursor.at_ms,
                "sequence": capture.cursor.sequence, "duplicate_book": capture.duplicate_book,
                "market": market_dict(capture.market), "spread_bps": str(capture.market.spread_bps),
                "rules": {"tick_size": str(capture.rules.tick_size), "lot_size": str(capture.rules.lot_size), "min_size": str(capture.rules.min_size)},
                "trend": trend, "completed_bar_end_ms": {"15m": short.end_ms, "1H": hour.end_ms, "BTC_1H": btc.end_ms},
                "book": book_metrics(capture), "filter_reasons": reasons,
                "warnings": ["Anlık REST snapshot; streaming değildir.", "Duvarlar iptal edilebilir; fiyat hedefi değildir.",
                             "Derinlik yalnız dönen en fazla 400 seviyeyi kapsar; bant kapsaması ayrıca belirtilir."]}

    def radar(self, instruments):
        if not isinstance(instruments, (list, tuple)) or not 1 <= len(instruments) <= 10:
            raise TradingError("RADAR_UNIVERSE_REQUIRED_MAX_10")
        instruments = [symbol(s) for s in instruments]
        if len(set(instruments)) != len(instruments):
            raise TradingError("DUPLICATE_INSTRUMENT")
        accepted, rejected = [], []
        with self._lock:
            try:
                tickers = _rows(self.client.fetch("tickers"))
            except TradingError as error:
                self.last_health = {"ok": False, "reason": error.code, "checked_at_ms": self._now(), "live_enabled": False}
                return {"label": LABEL, "live_enabled": False, "scope": "CONFIGURED_UNIVERSE", "as_of_ms": self._now(),
                        "accepted": [], "rejected": [{"instrument": s, "reasons": [error.code]} for s in instruments]}
            for instrument in instruments:
                try:
                    matches = [r for r in tickers if isinstance(r, dict) and r.get("instId") == instrument]
                    if len(matches) != 1 or matches[0].get("instType") != "SPOT":
                        raise TradingError("PUBLIC_INSTRUMENT_MISMATCH")
                    quote = matches[0]
                    try:
                        _fresh(milliseconds(quote["ts"]), self._now(), self.policy)
                        if number(quote["volCcy24h"]) < self.policy.min_quote_volume:
                            raise TradingError("LOW_QUOTE_VOLUME")
                        bid, ask = positive(quote["bidPx"]), positive(quote["askPx"])
                        if bid >= ask:
                            raise TradingError("INVALID_ORDER_BOOK")
                        if (ask-bid) / ((bid+ask)/2) * 10000 > self.policy.max_spread_bps:
                            raise TradingError("SPREAD_TOO_WIDE")
                    except (KeyError, TypeError):
                        raise TradingError("INVALID_PUBLIC_DATA") from None
                    report = self.analyze(instrument)
                    if report["filter_reasons"]:
                        rejected.append({"instrument": instrument, "reasons": report["filter_reasons"]})
                    else:
                        accepted.append(report)
                except TradingError as error:
                    rejected.append({"instrument": instrument, "reasons": [error.code]})
            now = self._now()
            fresh = []
            for report in accepted:
                if now - report["book_ts_ms"] > self.policy.max_market_age * 1000:
                    rejected.append({"instrument": report["market"]["instrument"], "reasons": ["STALE_PUBLIC_DATA"]})
                else:
                    fresh.append(report)
            return {"label": LABEL, "live_enabled": False, "scope": "CONFIGURED_UNIVERSE", "as_of_ms": now,
                    "accepted": fresh, "rejected": rejected}

    def grid(self, template):
        """Validate a caller-reviewed paper plan against freshly fetched data.

        No price bands, fee tier or slippage assumptions are invented here.
        This is a proposal, never a reservation, bot opening or actual order.
        """
        plan = build_plan(template)
        report = self.analyze(plan.instrument)
        from .public_contracts import InstrumentRules
        rules = report["rules"]
        InstrumentRules(plan.instrument, *(Decimal(rules[k]) for k in ("tick_size", "lot_size", "min_size"))).check_plan(plan)
        screened = screen([{"market": report["market"], "plan": plan.to_dict(), "trend": report["trend"]}],
                          self.policy, self._now() // 1000)
        if not screened["accepted"]:
            raise TradingError(screened["rejected"][0]["reason"])
        net = [str((sell * (1 - plan.fee_rate) * (1 - plan.slippage_rate) /
                    (buy * (1 + plan.fee_rate) * (1 + plan.slippage_rate)) - 1)) for buy, sell in zip(plan.levels, plan.levels[1:])]
        if any(Decimal(r) <= 0 for r in net):
            raise TradingError("GRID_NOT_PROFITABLE_AFTER_SLIPPAGE")
        return {"label": LABEL, "live_enabled": False, "action": "PROPOSAL_ONLY_NO_RESERVATION", "analysis": report,
                "plan": plan.to_dict(), "arithmetic_levels": [str(p) for p in plan.levels], "planned_loss_usdt": str(plan.planned_loss),
                "net_grid_returns": net, "fee_assumption": "Caller supplied; no authenticated fee lookup",
                "bot_risk_cap_usdt": str(self.policy.equity * self.policy.risk_fraction),
                "total_risk_cap_usdt": str(self.policy.equity * self.policy.total_risk_fraction)}
