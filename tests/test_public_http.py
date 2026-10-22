"""Offline public GET transport tests. Every response is synthetic."""
import json
import unittest
from urllib.error import URLError
from unittest.mock import patch

from okx_grid.domain import TradingError
from okx_grid.public_http import ReadOnlyTRClient, _NoRedirect, RequestPacer


class InstantPacer:
    def wait(self):
        pass


def make_client(**kwargs):
    return ReadOnlyTRClient(pacer=InstantPacer(), sleep=lambda _: None, jitter=lambda: 0, **kwargs)
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
        self.response.geturl = lambda: request.full_url
        if self.exception:
            raise self.exception
        return self.response


class SafePublicTransport(unittest.TestCase):
    def test_uses_only_official_https_get_without_credentials(self):
        opener = FakeOpener()
        reply = make_client(opener=opener).fetch("ticker", "SOL-USDT")
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
        make_client(opener=opener).fetch("books", "BTC-USDT")
        request, _ = opener.calls[0]
        self.assertIn("/api/v5/market/books?", request.full_url)
        self.assertIn("sz=400", request.full_url)

    def test_candles_support_only_explicit_periods(self):
        opener = FakeOpener()
        client = make_client(opener=opener)
        client.fetch("candles", "BTC-USDT", bar="15m")
        self.assertIn("bar=15m", opener.calls[0][0].full_url)
        for bar in (None, "1D", "30m", "../trade"):
            with self.subTest(bar=bar), self.assertRaises(TradingError):
                client.fetch("candles", "BTC-USDT", bar=bar)

    def test_private_order_and_untrusted_symbol_rejected_before_network(self):
        opener = FakeOpener()
        client = make_client(opener=opener)
        for kind in ("trade", "order", "withdraw", "cancel", "account"):
            with self.subTest(kind=kind), self.assertRaisesRegex(TradingError, "PUBLIC_REQUEST_NOT_ALLOWED"):
                client.fetch(kind, "SOL-USDT")
        with self.assertRaises(TradingError):
            client.fetch("ticker", "SOL-USDT-SWAP")
        self.assertEqual(opener.calls, [])

    def test_nonzero_exchange_code_and_invalid_payload_fail_closed(self):
        for payload in ({"code": "50011", "data": []}, {"code": "0", "data": []},
                        {"code": "0", "data": "not a list"}, ["unexpected"]):
            with self.subTest(payload=payload), self.assertRaisesRegex(TradingError, "PUBLIC_RESPONSE_ERROR|PUBLIC_RETRY_EXHAUSTED"):
                make_client(opener=FakeOpener(FakeResponse(payload))).fetch("ticker", "SOL-USDT")

    def test_too_large_and_invalid_json_rejected(self):
        too_large = FakeResponse(b"a" * 5000)
        with self.assertRaisesRegex(TradingError, "PUBLIC_RESPONSE_TOO_LARGE"):
            make_client(opener=FakeOpener(too_large), max_response_bytes=4096).fetch("ticker", "BTC-USDT")
        with self.assertRaisesRegex(TradingError, "PUBLIC_INVALID_JSON"):
            make_client(opener=FakeOpener(FakeResponse(b"{bad"))).fetch("ticker", "BTC-USDT")

    def test_network_timeout_or_http_status_fails_without_leaking_remote_body(self):
        error = URLError("http://host.example?secret=DO_NOT_PRINT")
        with self.assertRaisesRegex(TradingError, "PUBLIC_HTTP_UNAVAILABLE") as ctx:
            make_client(opener=FakeOpener(exception=error)).fetch("ticker", "BTC-USDT")
        self.assertNotIn("DO_NOT_PRINT", str(ctx.exception))
        with self.assertRaisesRegex(TradingError, "PUBLIC_RETRY_EXHAUSTED"):
            make_client(opener=FakeOpener(FakeResponse({"code": "0", "data": [1]}, 503))).fetch("ticker", "BTC-USDT")

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

    def test_html_time_response_is_rejected_with_content_free_diagnostics(self):
        response=FakeResponse(b'<html><body>PRIVATE_VALUE_MUST_NOT_BE_RECORDED</body></html>')
        response.headers={'Content-Type':'text/html; charset=utf-8','Content-Length':'70','Set-Cookie':'DO_NOT_RECORD'}
        client=make_client(opener=FakeOpener(response))
        with self.assertRaisesRegex(TradingError,'PUBLIC_NON_JSON_RESPONSE'):
            client.fetch('time')
        self.assertEqual(client.last_response['kind'],'time')
        self.assertEqual(client.last_response['http_status'],200)
        self.assertEqual(client.last_response['payload_class'],'HTML')
        self.assertNotIn('PRIVATE_VALUE',json.dumps(client.last_response))
        self.assertNotIn('DO_NOT_RECORD',json.dumps(client.last_response))

    def test_oversized_body_remains_bounded_and_reports_safe_size_only(self):
        client=make_client(opener=FakeOpener(FakeResponse(b'{'+b'a'*6000)),max_response_bytes=4096)
        with self.assertRaisesRegex(TradingError,'PUBLIC_RESPONSE_TOO_LARGE'):
            client.fetch('time')
        self.assertEqual(client.last_response['bytes_read'],4097)
        self.assertEqual(client.last_response['payload_class'],'JSON_OBJECT')

    def test_valid_public_json_still_passes_with_missing_content_type(self):
        client=make_client(opener=FakeOpener())
        self.assertEqual(client.fetch('time')['code'],'0')
        self.assertEqual(client.last_response['content_type'],'OTHER_OR_MISSING')
        self.assertEqual(client.last_response['payload_class'],'JSON_OBJECT')

    def test_network_error_diagnostics_never_copy_exception_text(self):
        client=make_client(opener=FakeOpener(exception=URLError('PRIVATE_MESSAGE_DO_NOT_RECORD')))
        with self.assertRaisesRegex(TradingError,'PUBLIC_HTTP_UNAVAILABLE'):
            client.fetch('time')
        self.assertEqual(client.last_response['content_type'],'NOT_RECEIVED')
        self.assertNotIn('PRIVATE_MESSAGE',json.dumps(client.last_response))


if __name__ == "__main__":
    unittest.main()
