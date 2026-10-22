"""Explicit TR public GET transport. No credentials, poller or order methods.

Reviewed from SOL PR #4; adds shared pacing, bounded retries and final-URL checks.
"""
import json
import random
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .domain import TradingError
from .public_contracts import TR_ORIGIN, request_spec, retry_delay


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TradingError("PUBLIC_REDIRECT_FORBIDDEN")


def _default_opener():
    return build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())


class RequestPacer:
    """One process-wide conservative cap (4 GET/s); reuse across consumers."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self._next = 0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            delay = self._next - self.clock()
            if delay > 0:
                self.sleep(delay)
            self._next = self.clock() + 0.25


_SHARED_PACER = RequestPacer()


class ReadOnlyTRClient:
    def __init__(self, *, opener=None, timeout=8, max_response_bytes=1_000_000,
                 pacer=None, sleep=time.sleep, jitter=None, max_catalog_bytes=None):
        if (type(timeout) not in (int, float) or not 1 <= timeout <= 15
                or type(max_response_bytes) is not int or not 4096 <= max_response_bytes <= 2_000_000):
            raise TradingError("INVALID_PUBLIC_HTTP_POLICY")
        if max_catalog_bytes is not None and (type(max_catalog_bytes) is not int or not 4096 <= max_catalog_bytes <= 8_000_000):
            raise TradingError("INVALID_PUBLIC_CATALOG_POLICY")
        self._default_transport = opener is None
        self._opener = opener if opener is not None else _default_opener()
        self._timeout, self._max_bytes = timeout, max_response_bytes
        # Explicit consumer opt-in; the generic client's previous budget remains.
        self._catalog_max_bytes = max_response_bytes if max_catalog_bytes is None else max_catalog_bytes
        self._pacer = pacer if pacer is not None else _SHARED_PACER
        self._sleep = sleep
        self._jitter = jitter if jitter is not None else lambda: random.uniform(0, 0.1)
        self.last_response = None

    def _observe_response(self, kind, spec, status, headers=None, raw=None):
        """Only enum/numeric transport facts; never retain headers or a body."""
        headers = headers if headers is not None else {}
        mime = str(headers.get("Content-Type", "")).lower().split(";", 1)[0].strip()
        length = str(headers.get("Content-Length", ""))
        first = raw[:256].lstrip() if raw is not None else b""
        self.last_response = {
            "kind": kind,
            "source_origin": TR_ORIGIN, "path": spec["path"],
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "content_type": "JSON" if mime == "application/json" or mime.endswith("+json") else "HTML" if mime == "text/html" else "OTHER_OR_MISSING",
            "content_length": int(length) if length.isascii() and length.isdigit() and len(length) <= 12 else None,
            "bytes_read": len(raw) if raw is not None else 0,
            "payload_class": "HTML" if first.startswith(b"<") else "JSON_OBJECT" if first.startswith(b"{") else "OTHER_OR_EMPTY",
        }

    def _retry(self, attempt, status, code="", retry_after=None):
        delay = retry_delay(attempt, status=status, api_code=code, retry_after=retry_after)
        if delay is None:
            return False
        jitter = self._jitter()
        if type(jitter) not in (int, float) or not 0 <= jitter <= 0.1:
            raise TradingError("INVALID_PUBLIC_JITTER")
        self._sleep(float(delay) + jitter)
        return True

    def fetch(self, kind, instrument=None, *, bar=None, after=None):
        spec = request_spec(kind, instrument, bar=bar)
        self.last_response = {"kind": kind, "source_origin": TR_ORIGIN, "path": spec["path"], "http_status": None,
                              "content_type": "NOT_RECEIVED", "bytes_read": 0, "payload_class": "NOT_RECEIVED"}
        if spec["method"] != "GET" or spec["origin"] != TR_ORIGIN:
            raise TradingError("PUBLIC_REQUEST_NOT_ALLOWED")
        if after is not None:
            if kind != "history_trades" or not isinstance(after, str) or not after.isascii() or not after.isdigit() or len(after)>40:
                raise TradingError("INVALID_PUBLIC_PAGINATION")
            spec["params"]["after"] = after
        query = urlencode(spec["params"])
        url = TR_ORIGIN + spec["path"] + ("?" + query if query else "")
        read_limit = self._catalog_max_bytes if kind == "instruments" else self._max_bytes
        for attempt in range(4):
            request = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "OKXTR-GridPaperReadOnly/1.0"})
            self._pacer.wait()
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    if response.geturl() != url:
                        raise TradingError("PUBLIC_REDIRECT_FORBIDDEN")
                    status = response.status
                    self._observe_response(kind, spec, status, getattr(response, "headers", None))
                    if status != 200:
                        if self._retry(attempt, status):
                            continue
                        raise TradingError("PUBLIC_HTTP_STATUS_ERROR")
                    raw = response.read(read_limit + 1)
                    self._observe_response(kind, spec, status, getattr(response, "headers", None), raw)
                    if self.last_response["payload_class"] == "HTML" or self.last_response["content_type"] == "HTML":
                        raise TradingError("PUBLIC_NON_JSON_RESPONSE")
                    if len(raw) > read_limit:
                        raise TradingError("PUBLIC_RESPONSE_TOO_LARGE")
            except TradingError:
                raise
            except HTTPError as error:
                try:
                    status = error.code
                    self._observe_response(kind, spec, status, error.headers)
                    wait = error.headers.get("Retry-After") if error.headers else None
                    if 300 <= status < 400:
                        raise TradingError("PUBLIC_REDIRECT_FORBIDDEN") from None
                    if self._retry(attempt, status, retry_after=wait):
                        continue
                    raise TradingError("PUBLIC_HTTP_UNAVAILABLE") from None
                finally:
                    error.close()
            except (URLError, TimeoutError, OSError) as error:
                if isinstance(getattr(error, "reason", error), ssl.SSLError):
                    raise TradingError("PUBLIC_TLS_ERROR") from None
                # Timeout outcome for GET can be retried; never applies to orders.
                if attempt < 3 and self._retry(attempt, 0):
                    continue
                raise TradingError("PUBLIC_HTTP_UNAVAILABLE") from None
            try:
                data = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise TradingError("PUBLIC_INVALID_JSON") from None
            if not isinstance(data, dict):
                raise TradingError("PUBLIC_RESPONSE_ERROR")
            if data.get("code") == "50011" and self._retry(attempt, 200, "50011"):
                continue
            if (data.get("code") != "0" or not isinstance(data.get("data"), list)
                    or not 1 <= len(data["data"]) <= 5000):
                raise TradingError("PUBLIC_RESPONSE_ERROR")
            return data
        raise TradingError("PUBLIC_RETRY_EXHAUSTED")
