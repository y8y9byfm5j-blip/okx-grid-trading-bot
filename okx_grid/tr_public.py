"""Read-only OKX TR public reader for the live Telegram service.

Covers the market-wide endpoints the V7 snapshot transport does not need:
all spot tickers, daily/weekly candles and aggregated taker buy/sell volume.
GET only, fixed TR origin, bounded retries, shared pacing. No credentials.
"""
from decimal import Decimal, InvalidOperation
import json
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .domain import TradingError
from .public_http import RequestPacer, _SHARED_PACER

TR_ORIGIN = "https://tr.okx.com"
PATHS = {
    "tickers": "/api/v5/market/tickers",
    "ticker": "/api/v5/market/ticker",
    "candles": "/api/v5/market/candles",
    "history_candles": "/api/v5/market/history-candles",
    "taker_volume": "/api/v5/rubik/stat/taker-volume",
}
BARS = {"1H", "4H", "1D", "1W"}
STABLE = frozenset({"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDG", "PYUSD", "USDE", "USDD", "EURT",
                    "EUR", "TRY", "BRL", "AUD", "AED", "PLN", "SGD", "USD", "RLUSD", "USD1", "USDS"})
# rubik/stat is limited to 5 requests per 2 seconds per IP.
_RUBIK_PACER = RequestPacer()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TradingError("PUBLIC_REDIRECT_FORBIDDEN")


def urllib_transport(*, timeout=10, max_bytes=4_000_000):
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())

    def get(url):
        request = Request(url, method="GET", headers={"Accept": "application/json",
                                                      "User-Agent": "OKXTR-TelegramBot/1.0"})
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                status = response.status
        except HTTPError as error:
            status = error.code
            error.close()
            raise _Retryable(status) if status in {429, 500, 502, 503, 504} else TradingError("PUBLIC_HTTP_STATUS_ERROR")
        except (URLError, TimeoutError, OSError):
            raise _Retryable(0) from None
        if status != 200 or len(raw) > max_bytes:
            raise TradingError("PUBLIC_HTTP_STATUS_ERROR")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise TradingError("PUBLIC_INVALID_JSON") from None
    return get


class _Retryable(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def dec(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TradingError("INVALID_PUBLIC_DATA") from None
    if not result.is_finite():
        raise TradingError("INVALID_PUBLIC_DATA")
    return result


class TRPublic:
    def __init__(self, *, transport=None, sleep=time.sleep, pacer=None, rubik_pacer=None):
        self._get = transport if transport is not None else urllib_transport()
        self._sleep = sleep
        self._pacer = pacer if pacer is not None else _SHARED_PACER
        self._rubik = rubik_pacer if rubik_pacer is not None else _RUBIK_PACER

    def _fetch(self, kind, params):
        url = TR_ORIGIN + PATHS[kind] + "?" + urlencode(params)
        pacer = self._rubik if kind == "taker_volume" else self._pacer
        for attempt in range(3):
            pacer.wait()
            if kind == "taker_volume":
                pacer.wait()  # two slots: stays under 5 requests / 2 seconds
            try:
                data = self._get(url)
            except _Retryable:
                if attempt == 2:
                    raise TradingError("PUBLIC_HTTP_UNAVAILABLE") from None
                self._sleep(1.0 * (attempt + 1))
                continue
            if not isinstance(data, dict):
                raise TradingError("PUBLIC_RESPONSE_ERROR")
            if data.get("code") == "50011" and attempt < 2:
                self._sleep(2.0)
                continue
            if data.get("code") != "0" or not isinstance(data.get("data"), list):
                raise TradingError("PUBLIC_RESPONSE_ERROR")
            return data["data"]
        raise TradingError("PUBLIC_RETRY_EXHAUSTED")

    def spot_tickers(self):
        rows = []
        for item in self._fetch("tickers", {"instType": "SPOT"}):
            try:
                base, quote = item["instId"].split("-")
                last = dec(item["last"])
                if last <= 0:
                    continue
                rows.append({"instrument": item["instId"], "base": base, "quote": quote, "last": last,
                             "open_24h": dec(item["open24h"]) if item.get("open24h") else last,
                             "quote_volume_24h": dec(item.get("volCcy24h") or 0), "ts_ms": int(item["ts"])})
            except (KeyError, ValueError, TradingError):
                continue
        if not rows:
            raise TradingError("NO_SPOT_TICKERS")
        return rows

    def last_price(self, instrument):
        rows = self._fetch("ticker", {"instId": instrument})
        if len(rows) != 1:
            raise TradingError("PUBLIC_RESPONSE_ERROR")
        return dec(rows[0]["last"])

    def candles(self, instrument, bar, limit=100):
        """Oldest first; each row is (ts_ms, open, high, low, close, base_vol, quote_vol, confirmed)."""
        if bar not in BARS or type(limit) is not int or not 1 <= limit <= 300:
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        kind = "history_candles" if bar == "1W" else "candles"
        out = []
        for r in self._fetch(kind, {"instId": instrument, "bar": bar, "limit": str(limit)}):
            out.append((int(r[0]), dec(r[1]), dec(r[2]), dec(r[3]), dec(r[4]), dec(r[5]),
                        dec(r[7] if len(r) > 7 and r[7] != "" else r[6]), r[8] == "1" if len(r) > 8 else True))
        out.sort(key=lambda row: row[0])
        return out

    def taker_volume(self, coin, period="1H"):
        """Oldest first (ts_ms, sell_base, buy_base) aggregated over all OKX spot pairs of the coin."""
        if period not in {"5m", "1H", "1D"} or not coin.isalnum():
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        rows = [(int(r[0]), dec(r[1]), dec(r[2])) for r in
                self._fetch("taker_volume", {"ccy": coin, "instType": "SPOT", "period": period})]
        rows.sort(key=lambda row: row[0])
        return rows


def usdt_try_rate(tickers):
    for row in tickers:
        if row["instrument"] == "USDT-TRY":
            return row["last"]
    return None


def coin_volumes(tickers):
    """Per-coin 24h quote volume in USDT, split into USDT-quoted and TRY-quoted markets."""
    rate = usdt_try_rate(tickers)
    coins = {}
    for row in tickers:
        base, quote = row["base"], row["quote"]
        if base in STABLE or quote not in {"USDT", "TRY"}:
            continue
        if quote == "TRY" and not rate:
            continue
        volume = row["quote_volume_24h"] / (rate if quote == "TRY" else 1)
        price = row["last"] / (rate if quote == "TRY" else 1)
        entry = coins.setdefault(base, {"usdt_volume": Decimal(0), "try_volume": Decimal(0), "price": None,
                                        "change_24h": None})
        entry["usdt_volume" if quote == "USDT" else "try_volume"] += volume
        if quote == "USDT" or entry["price"] is None:
            entry["price"] = price
            if row["open_24h"] > 0:
                entry["change_24h"] = row["last"] / row["open_24h"] - 1
    return coins, rate
