"""Explicit OKX TR PUBLIC GET transport; never signs, authenticates or places orders.

This module is inert until fetch() is explicitly called. It has no worker,
scheduler, websocket, auth settings or POST support. Real TR endpoint smoke
tests are required in an authorized network environment before production use.
"""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (HTTPRedirectHandler, ProxyHandler, Request, build_opener)

from .domain import TradingError
from .public_contracts import TR_ORIGIN, request_spec


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TradingError("PUBLIC_REDIRECT_FORBIDDEN")


def _default_opener():
    # No proxy inheritance, cookie jar, Authorization or redirects.
    # Python's HTTPS handler validates server certificates by default.
    return build_opener(ProxyHandler({}), _NoRedirect())


class ReadOnlyTRClient:
    def __init__(self, *, opener=None, timeout=8, max_response_bytes=1_000_000):
        if (type(timeout) not in (int, float) or not 1 <= timeout <= 15
                or type(max_response_bytes) is not int
                or not 4096 <= max_response_bytes <= 2_000_000):
            raise TradingError("INVALID_PUBLIC_HTTP_POLICY")
        self._opener = opener if opener is not None else _default_opener()
        self._timeout = timeout
        self._max_bytes = max_response_bytes

    def fetch(self, kind, instrument, *, bar=None):
        # request_spec only admits the official origin, public GET paths, spot
        # USDT symbols and the supported candle periods.
        spec = request_spec(kind, instrument, bar=bar)
        if spec["method"] != "GET" or spec["origin"] != TR_ORIGIN:
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        url = TR_ORIGIN + spec["path"] + "?" + urlencode(spec["params"])
        request = Request(url, method="GET", headers={
            "Accept": "application/json",
            "User-Agent": "OKXTR-GridPaperReadOnly/1.0",
        })
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise TradingError("PUBLIC_HTTP_STATUS_ERROR")
                raw = response.read(self._max_bytes + 1)
                if len(raw) > self._max_bytes:
                    raise TradingError("PUBLIC_RESPONSE_TOO_LARGE")
        except TradingError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError):
            # Never reproduce remote response bodies, URLs or request headers.
            raise TradingError("PUBLIC_HTTP_UNAVAILABLE") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise TradingError("PUBLIC_INVALID_JSON") from None
        if (not isinstance(data, dict) or data.get("code") != "0"
                or not isinstance(data.get("data"), list)
                or not 1 <= len(data["data"]) <= 5000):
            raise TradingError("PUBLIC_RESPONSE_ERROR")
        return data
