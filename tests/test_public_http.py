"""Offline public GET transport tests. Every response is synthetic."""
import json
import unittest
from urllib.error import URLError
from unittest.mock import patch

from okx_grid.domain import TradingError
from okx_grid.public_http import ReadOnlyTRClient, _NoRedirect
from okx_grid.public_contracts import TR_ORIGIN


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, maximum):
        return self.payload[:maximum]


class FakeOpener:
    def __init__(self, response=None, exception=None):
        self.response = response or FakeResponse({"code": "0", "data": [{"ok": "1"}]})
        self.exception = exception
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.exception:
            raise self.exception
        return self.response


class SafePublicTransport(unittest.TestCase):
    def test_uses_only_official_https_get_without_credentials(self):
        opener = FakeOpener()
        reply = ReadOnlyTRClient(opener=opener).fetch("ticker", "SOL-USDT")
        self.assertEqual(reply["code"], "0")
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 8)
        self.assertEqual(request.full_url, TR_ORIGIN + "/api/v5/market/ticker?instId=SOL-USDT")
        self.assertTrue(request.full_url.startswith("https://tr.okx.com/"))
        self.assertNotIn("Authorization", request.headers)
        self.assertFalse(any(h.startswith("Ok-access") for h in request.headers))

    def test_get_book_is_public_and_size_limited_in_query(self):
        opener = FakeOpener()
        ReadOnlyTRClient(opener=opener).fetch("books", "BTC-USDT")
        request, _ = opener.calls[0]
        self.assertIn("/api/v5/market/books?", request.full_url)
        self.assertIn("sz=400", request.full_url)

    def test_candles_support_only_explicit_periods(self):
        opener = FakeOpener()
        client = ReadOnlyTRClient(opener=opener)
        client.fetch("candles", "BTC-USDT", bar="15m")
        self.assertIn("bar=15m", opener.calls[0][0].full_url)
        for bar in (None, "1D", "30m", "../trade"):
            with self.subTest(bar=bar), self.assertRaises(TradingError):
                client.fetch("candles", "BTC-USDT", bar=bar)

    def test_private_order_and_untrusted_symbol_rejected_before_network(self):
        opener = FakeOpener()
        client = ReadOnlyTRClient(opener=opener)
        for kind in ("trade", "order", "withdraw", "cancel", "account"):
            with self.subTest(kind=kind), self.assertRaisesRegex(TradingError, "PUBLIC_REQUEST_NOT_ALLOWED"):
                client.fetch(kind, "SOL-USDT")
        with self.assertRaises(TradingError):
            client.fetch("ticker", "SOL-USDT-SWAP")
        self.assertEqual(opener.calls, [])

    def test_nonzero_exchange_code_and_invalid_payload_fail_closed(self):
        for payload in ({"code": "50011", "data": []}, {"code": "0", "data": []},
                        {"code": "0", "data": "not a list"}, ["unexpected"]):
            with self.subTest(payload=payload), self.assertRaisesRegex(TradingError, "PUBLIC_RESPONSE_ERROR"):
                ReadOnlyTRClient(opener=FakeOpener(FakeResponse(payload))).fetch("ticker", "SOL-USDT")

    def test_too_large_and_invalid_json_rejected(self):
        too_large = FakeResponse(b"a" * 5000)
        with self.assertRaisesRegex(TradingError, "PUBLIC_RESPONSE_TOO_LARGE"):
            ReadOnlyTRClient(opener=FakeOpener(too_large), max_response_bytes=4096).fetch("ticker", "BTC-USDT")
        with self.assertRaisesRegex(TradingError, "PUBLIC_INVALID_JSON"):
            ReadOnlyTRClient(opener=FakeOpener(FakeResponse(b"{bad"))).fetch("ticker", "BTC-USDT")

    def test_network_timeout_or_http_status_fails_without_leaking_remote_body(self):
        error = URLError("http://host.example?secret=DO_NOT_PRINT")
        with self.assertRaisesRegex(TradingError, "PUBLIC_HTTP_UNAVAILABLE") as ctx:
            ReadOnlyTRClient(opener=FakeOpener(exception=error)).fetch("ticker", "BTC-USDT")
        self.assertNotIn("DO_NOT_PRINT", str(ctx.exception))
        with self.assertRaisesRegex(TradingError, "PUBLIC_HTTP_STATUS_ERROR"):
            ReadOnlyTRClient(opener=FakeOpener(FakeResponse({"code": "0", "data": [1]}, 503))).fetch("ticker", "BTC-USDT")

    def test_redirect_policy_rejects_every_redirect(self):
        with self.assertRaisesRegex(TradingError, "PUBLIC_REDIRECT_FORBIDDEN"):
            _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid/")

    def test_invalid_transport_limits_rejected(self):
        for timeout, size in ((0, 10000), (8, 10), (16, 10000), (8, 3000000)):
            with self.subTest(timeout=timeout, size=size), self.assertRaisesRegex(TradingError, "INVALID_PUBLIC_HTTP_POLICY"):
                ReadOnlyTRClient(timeout=timeout, max_response_bytes=size)

    def test_transport_never_auto_connects_on_init(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            ReadOnlyTRClient()


if __name__ == "__main__":
    unittest.main()
