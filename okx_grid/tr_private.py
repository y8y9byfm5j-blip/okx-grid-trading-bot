"""Signed OKX TR client limited to account balance and spot GRID bot endpoints.

Credentials are read by the caller from the VPS environment and are never
logged, returned or stored. POST requests are never retried blindly: a lost
reply is resolved by looking up the client-supplied algo ID first.
"""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .domain import TradingError

TR_ORIGIN = "https://tr.okx.com"
ALLOWED = {
    ("GET", "/api/v5/account/balance"),
    ("GET", "/api/v5/tradingBot/grid/orders-algo-pending"),
    ("GET", "/api/v5/tradingBot/grid/orders-algo-history"),
    ("GET", "/api/v5/tradingBot/grid/orders-algo-details"),
    ("POST", "/api/v5/tradingBot/grid/order-algo"),
    ("POST", "/api/v5/tradingBot/grid/stop-order-algo"),
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TradingError("PRIVATE_REDIRECT_FORBIDDEN")


class OutcomeUnknown(TradingError):
    """The request may or may not have reached OKX; reconcile before acting again."""
    def __init__(self):
        super().__init__("PRIVATE_OUTCOME_UNKNOWN")


def urllib_transport(*, timeout=10):
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())

    def send(method, url, headers, body):
        request = Request(url, method=method, headers=headers, data=body.encode() if body else None)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(2_000_001)
        except HTTPError as error:
            try:
                return error.code, error.read(65536)
            finally:
                error.close()
        except (URLError, TimeoutError, OSError):
            raise OutcomeUnknown() from None
    return send


class OKXTRPrivate:
    def __init__(self, access, signing, phrase, *, transport=None, clock=None):
        if not all(isinstance(v, str) and v.strip() not in {"", "CHANGE_ME"} for v in (access, signing, phrase)):
            raise TradingError("OKX_ACCESS_NOT_CONFIGURED")
        self._access, self._signing, self._phrase = access.strip(), signing.strip(), phrase.strip()
        self._send = transport if transport is not None else urllib_transport()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def __repr__(self):
        return "OKXTRPrivate(<redacted>)"

    def _request(self, method, path, params=None, body=None):
        if (method, path) not in ALLOWED:
            raise TradingError("PRIVATE_REQUEST_NOT_ALLOWED")
        query = "?" + urlencode(params) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        now = self._clock()
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        digest = hmac.new(self._signing.encode(), (stamp + method + path + query + payload).encode(), hashlib.sha256)
        headers = {"OK-ACCESS-KEY": self._access, "OK-ACCESS-SIGN": base64.b64encode(digest.digest()).decode(),
                   "OK-ACCESS-TIMESTAMP": stamp, "OK-ACCESS-PASSPHRASE": self._phrase,
                   "Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "OKXTR-TelegramBot/1.0"}
        status, raw = self._send(method, TR_ORIGIN + path + query, headers, payload)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            if method == "POST" and status >= 500:
                raise OutcomeUnknown() from None
            raise TradingError("PRIVATE_INVALID_RESPONSE") from None
        if not isinstance(data, dict):
            raise TradingError("PRIVATE_INVALID_RESPONSE")
        code = str(data.get("code"))
        if status != 200 or code != "0":
            if method == "POST" and status >= 500:
                raise OutcomeUnknown()
            # Only OKX's numeric code is kept; messages may echo request details.
            raise TradingError("OKX_ERROR_" + (code if code.isdigit() else "UNKNOWN"))
        rows = data.get("data")
        if not isinstance(rows, list):
            raise TradingError("PRIVATE_INVALID_RESPONSE")
        return rows

    def usdt_available(self):
        for account in self._request("GET", "/api/v5/account/balance", {"ccy": "USDT"}):
            for detail in account.get("details", []):
                if detail.get("ccy") == "USDT":
                    return detail.get("availBal") or detail.get("cashBal") or "0"
        return "0"

    def pending_grids(self):
        return self._request("GET", "/api/v5/tradingBot/grid/orders-algo-pending", {"algoOrdType": "grid"})

    def grid_history(self):
        return self._request("GET", "/api/v5/tradingBot/grid/orders-algo-history", {"algoOrdType": "grid"})

    def grid_details(self, algo_id):
        rows = self._request("GET", "/api/v5/tradingBot/grid/orders-algo-details",
                             {"algoOrdType": "grid", "algoId": str(algo_id)})
        return rows[0] if rows else None

    def place_spot_grid(self, *, instrument, lower, upper, grids, quote_size, stop, take_profit, client_id):
        body = {"instId": instrument, "algoOrdType": "grid", "minPx": str(lower), "maxPx": str(upper),
                "gridNum": str(grids), "runType": "1", "quoteSz": str(quote_size),
                "slTriggerPx": str(stop), "tpTriggerPx": str(take_profit), "algoClOrdId": client_id,
                "tag": "tgbot"}
        rows = self._request("POST", "/api/v5/tradingBot/grid/order-algo", body=body)
        if len(rows) != 1 or str(rows[0].get("sCode")) != "0" or not rows[0].get("algoId"):
            code = str(rows[0].get("sCode")) if rows else "EMPTY"
            raise TradingError("OKX_GRID_REJECTED_" + (code if code.isdigit() else "UNKNOWN"))
        return rows[0]["algoId"]

    def stop_spot_grid(self, algo_id, instrument, *, sell_base=True):
        rows = self._request("POST", "/api/v5/tradingBot/grid/stop-order-algo",
                             body=[{"algoId": str(algo_id), "instId": instrument, "algoOrdType": "grid",
                                    "stopType": "1" if sell_base else "2"}])
        if len(rows) != 1 or str(rows[0].get("sCode")) != "0":
            code = str(rows[0].get("sCode")) if rows else "EMPTY"
            raise TradingError("OKX_GRID_STOP_REJECTED_" + (code if code.isdigit() else "UNKNOWN"))
        return True
