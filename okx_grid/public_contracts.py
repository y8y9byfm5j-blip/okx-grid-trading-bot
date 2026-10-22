"""Offline contracts for verified OKX TR public REST formats; NO HTTP transport.

These functions normalize supplied responses. A source_origin argument is
required provenance metadata, not proof that a live request actually happened.
REST books are full snapshots: sequence gaps are not WS continuity failures.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
import hashlib

from .domain import Market, TradingError, canonical, number, positive, precise, symbol

TR_ORIGIN = "https://tr.okx.com"
PUBLIC_PATHS = {
    "instruments": "/api/v5/public/instruments",
    "tickers": "/api/v5/market/tickers",
    "time": "/api/v5/public/time",
    "ticker": "/api/v5/market/ticker",
    "books": "/api/v5/market/books",
    "candles": "/api/v5/market/candles",
}
PERIODS = {"15m": 900000, "1H": 3600000, "4H": 14400000}


def request_spec(kind, instrument=None, *, bar=None):
    """Return only an allowlisted GET contract. Never sign, send, or accept URLs."""
    if kind == "grid_candles":
        instrument = symbol(instrument)
        if bar not in PERIODS:
            raise TradingError("UNSUPPORTED_BAR")
        return {"method": "GET", "origin": TR_ORIGIN, "path": PUBLIC_PATHS["candles"],
                "params": {"instId": instrument, "bar": bar, "limit": "22"}}
    if kind in {"flow_candles", "trades", "history_trades", "flow_books", "flow_ticker"}:
        from .flow_contracts import BARS, spot_symbol
        instrument = spot_symbol(instrument)
        if (kind == "flow_candles") != (bar is not None) or bar is not None and bar not in BARS:
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        path = {"flow_candles":"candles", "trades":"trades", "history_trades":"history-trades", "flow_books":"books", "flow_ticker":"ticker"}[kind]
        params = {"instId":instrument}
        if kind == "flow_candles":
            params.update(bar=bar, limit="22")
        elif kind == "flow_books":
            params["sz"]="400"
        elif kind == "trades":
            params["limit"]="500"
        elif kind == "history_trades":
            params.update(limit="100", type="1")
        return {"method":"GET", "origin":TR_ORIGIN, "path":"/api/v5/market/"+path, "params":params}
    if instrument is not None:
        instrument = symbol(instrument)
    if kind not in PUBLIC_PATHS or (kind == "candles") != (bar is not None):
        raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
    if bar is not None and bar not in PERIODS:
        raise TradingError("UNSUPPORTED_BAR")
    if kind == "time":
        if instrument is not None:
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        params = {}
    elif kind == "tickers":
        if instrument is not None:
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        params = {"instType": "SPOT"}
    else:
        if instrument is None and kind != "instruments":
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        params = {"instId": instrument} if instrument is not None else {}
    if kind == "instruments":
        params["instType"] = "SPOT"
    elif kind == "books":
        params["sz"] = "400"
    elif kind == "candles":
        params.update(bar=bar, limit="3")
    return {"method": "GET", "origin": TR_ORIGIN, "path": PUBLIC_PATHS[kind], "params": params}


def retry_delay(attempt, *, status, api_code="", retry_after=None):
    """Bounded retry scheduling model only; does not sleep or make a request."""
    if type(attempt) is not int or attempt < 0:
        raise TradingError("INVALID_RETRY_ATTEMPT")
    if status not in {0, 429, 500, 502, 503, 504} and api_code != "50011":
        return None
    if attempt >= 3:
        raise TradingError("PUBLIC_RETRY_EXHAUSTED")
    delay = Decimal(2 ** attempt)
    if retry_after is not None:
        wait = number(retry_after)
        if not 0 <= wait <= 30:
            raise TradingError("PUBLIC_COOLDOWN_REQUIRED")
        delay = max(delay, wait)
    return delay


def milliseconds(value):
    if type(value) is int and 0 <= value <= 10**15:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 16:
        return milliseconds(int(value))
    raise TradingError("INVALID_EXCHANGE_TIMESTAMP")


def _fresh(at, now, policy):
    if now - at > policy.max_market_age * 1000:
        raise TradingError("STALE_PUBLIC_DATA")
    if at - now > policy.max_future_seconds * 1000:
        raise TradingError("FUTURE_PUBLIC_DATA")


def _rows(reply):
    if not isinstance(reply, dict) or reply.get("code") != "0":
        raise TradingError("PUBLIC_RESPONSE_ERROR")
    rows = reply.get("data")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise TradingError("EMPTY_OR_INVALID_PUBLIC_DATA")
    return rows


def _one(reply):
    rows = _rows(reply)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise TradingError("AMBIGUOUS_PUBLIC_DATA")
    return rows[0]


@dataclass(frozen=True)
class InstrumentRules:
    instrument: str
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal

    def check_plan(self, plan):
        if (plan.instrument != self.instrument or plan.tick_size != self.tick_size
                or plan.lot_size != self.lot_size
                or any(plan.quantity(p) < self.min_size for p in plan.levels[:-1])):
            raise TradingError("INSTRUMENT_RULE_MISMATCH")


@dataclass(frozen=True)
class BookCursor:
    instrument: str
    sequence: int
    at_ms: int
    digest: str


@dataclass(frozen=True)
class MarketCapture:
    market: Market
    rules: InstrumentRules
    cursor: BookCursor
    duplicate_book: bool


@precise
def normalize_market(instrument, instruments_reply, ticker_reply, books_reply, *, source_origin, now_ms, policy, previous=None, enforce_policy=True):
    if source_origin != TR_ORIGIN:
        raise TradingError("UNVERIFIED_TR_ORIGIN")
    instrument, now_ms = symbol(instrument), milliseconds(now_ms)
    try:
        rows = [r for r in _rows(instruments_reply) if isinstance(r, dict) and r.get("instId") == instrument]
        if len(rows) != 1:
            raise TradingError("INSTRUMENT_NOT_AVAILABLE_IN_TR_CAPTURE")
        info = rows[0]
        if (info.get("instType") != "SPOT" or info.get("state") != "live"
                or info.get("quoteCcy") != "USDT" or info.get("baseCcy") + "-USDT" != instrument):
            raise TradingError("INSTRUMENT_NOT_LIVE_SPOT")
        rules = InstrumentRules(instrument, positive(info["tickSz"]), positive(info["lotSz"]), positive(info["minSz"]))
        ticker, book = _one(ticker_reply), _one(books_reply)
        if ticker.get("instId") != instrument or ticker.get("instType") != "SPOT":
            raise TradingError("PUBLIC_INSTRUMENT_MISMATCH")
        ticker_at, book_at = milliseconds(ticker["ts"]), milliseconds(book["ts"])
        _fresh(ticker_at, now_ms, policy)
        _fresh(book_at, now_ms, policy)
        if abs(ticker_at - book_at) > 2000:
            raise TradingError("INCOHERENT_PUBLIC_SNAPSHOTS")
        def levels(key):
            rows = book[key]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 400:
                raise TradingError("INVALID_ORDER_BOOK")
            for row in rows:
                if (not isinstance(row, list) or len(row) != 4 or row[2] != "0"
                        or not isinstance(row[3], str) or not row[3].isascii() or not row[3].isdigit()):
                    raise TradingError("INVALID_ORDER_BOOK")
            return [[r[0], r[1]] for r in rows]
        bids, asks = levels("bids"), levels("asks")
        market = Market.from_dict({"instrument": instrument, "price": ticker["last"],
                                   "bid": ticker["bidPx"], "ask": ticker["askPx"],
                                   "quote_volume_24h": ticker["volCcy24h"],
                                   "observed_at": min(ticker_at, book_at) // 1000, "bids": bids, "asks": asks})
        if type(enforce_policy) is not bool:
            raise TradingError("INVALID_POLICY_FLAG")
        if enforce_policy:
            policy.check_market(market, now_ms // 1000, Decimal(0))
        sequence = book["seqId"]
        if type(sequence) is not int or sequence < 0:
            raise TradingError("INVALID_BOOK_SEQUENCE")
        digest = hashlib.sha256(canonical({"instrument": instrument, "bids": bids, "asks": asks}).encode()).hexdigest()
        cursor = BookCursor(instrument, sequence, book_at, digest)
        if previous is not None:
            if (not isinstance(previous, BookCursor) or previous.instrument != instrument
                    or sequence < previous.sequence or book_at < previous.at_ms):
                raise TradingError("OUT_OF_ORDER_BOOK")
            if sequence == previous.sequence and digest != previous.digest:
                raise TradingError("CONFLICTING_BOOK_SEQUENCE")
        return MarketCapture(market, rules, cursor, bool(previous and previous.sequence == sequence))
    except (KeyError, TypeError):
        raise TradingError("INVALID_PUBLIC_DATA") from None


@dataclass(frozen=True)
class CandleReturn:
    instrument: str
    bar: str
    value: Decimal
    end_ms: int
    evaluated_at_ms: int


@precise
def normalize_return(instrument, bar, reply, *, source_origin, now_ms):
    if source_origin != TR_ORIGIN:
        raise TradingError("UNVERIFIED_TR_ORIGIN")
    instrument, now_ms = symbol(instrument), milliseconds(now_ms)
    if bar not in PERIODS:
        raise TradingError("UNSUPPORTED_BAR")
    period, previous, completed = PERIODS[bar], None, []
    for row in _rows(reply):
        if not isinstance(row, list) or len(row) != 9:
            raise TradingError("INVALID_CANDLE")
        at = milliseconds(row[0])
        if at % period or at > now_ms or previous is not None and at >= previous:
            raise TradingError("OUT_OF_ORDER_CANDLE")
        previous = at
        opening, high, low, close = map(positive, row[1:5])
        if low > min(opening, close) or high < max(opening, close) or low > high or any(number(v) < 0 for v in row[5:8]):
            raise TradingError("INVALID_CANDLE")
        if not isinstance(row[8], str) or row[8] not in {"0", "1"} or (row[8] == "1") != (at + period <= now_ms):
            raise TradingError("INVALID_CANDLE_STATE")
        if row[8] == "1":
            value = (close / opening - 1).quantize(Decimal("0.000000000001"), rounding=ROUND_UP)
            if abs(value) > 1:
                raise TradingError("INVALID_CANDLE_RETURN")
            completed.append((at, value))
    if not completed or completed[0][0] != now_ms // period * period - period:
        raise TradingError("STALE_OR_MISSING_COMPLETED_CANDLE")
    return CandleReturn(instrument, bar, completed[0][1], completed[0][0] + period, now_ms)


def make_trend(short, hour, btc, *, now_ms, policy):
    now_ms = milliseconds(now_ms)
    if (not all(isinstance(r, CandleReturn) for r in (short, hour, btc))
            or short.bar != "15m" or hour.bar != "1H" or btc.bar != "1H"
            or short.instrument != hour.instrument or btc.instrument != "BTC-USDT"):
        raise TradingError("TREND_CONTRACT_MISMATCH")
    for value in (short, hour, btc):
        _fresh(value.evaluated_at_ms, now_ms, policy)
        if value.end_ms != now_ms // PERIODS[value.bar] * PERIODS[value.bar]:
            raise TradingError("STALE_TREND_WINDOW")
    return {"return_15m": str(short.value), "return_1h": str(hour.value), "btc_return_1h": str(btc.value),
            "observed_at": min(r.evaluated_at_ms for r in (short, hour, btc)) // 1000}
