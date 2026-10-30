"""Offline API transport tests; no request ever reaches Riot."""

import unittest
from unittest.mock import Mock, patch

import requests

from utils import riot_api


class RiotAPITests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(riot_api.requests, "get"),
            patch.object(riot_api.limiter, "wait"),
            patch.object(riot_api.time, "sleep"),
            patch.object(riot_api, "logger"),
        ]
        self.get, self.wait, self.sleep, self.logger = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    @staticmethod
    def response(status=200, data=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = {} if data is None else data
        return response

    def test_success_uses_shared_headers_and_limiter(self):
        self.get.return_value = self.response(data={"info": {}})
        self.assertEqual(riot_api.make_request("https://example.test"), {"info": {}})
        self.wait.assert_called_once_with()
        self.get.assert_called_once_with("https://example.test", headers=riot_api.HEADERS, timeout=10)
        self.sleep.assert_not_called()

    def test_legacy_call_returns_none_on_forbidden(self):
        self.get.return_value = self.response(status=403)
        self.assertIsNone(riot_api.make_request("https://example.test"))
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_auth_and_missing_fail_fast_with_status(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.get.reset_mock()
                self.get.return_value = self.response(status=status)
                with self.assertRaises(riot_api.RiotAPIError) as caught:
                    riot_api.make_request("https://example.test", raise_on_error=True)
                self.assertEqual(caught.exception.status_code, status)
                self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_429_respects_decimal_retry_after(self):
        self.get.side_effect = [
            self.response(status=429, headers={"Retry-After": "1.5"}),
            self.response(data={"ok": True}),
        ]
        self.assertEqual(riot_api.make_request("https://example.test"), {"ok": True})
        self.sleep.assert_called_once_with(1.5)
        self.assertEqual(self.wait.call_count, 2)

    def test_exhausted_429_still_respects_server_cooldown(self):
        self.get.return_value = self.response(429, headers={"Retry-After": "2"})
        with self.assertRaises(riot_api.RiotAPIError) as caught:
            riot_api.make_request("https://example.test", max_retries=1, raise_on_error=True)
        self.assertEqual(caught.exception.status_code, 429)
        self.sleep.assert_called_once_with(2.0)

    def test_429_retry_after_http_date(self):
        with patch.object(riot_api.time, "time", return_value=0):
            self.assertEqual(riot_api._retry_after("Thu, 01 Jan 1970 00:00:09 GMT"), 9.0)
        self.assertEqual(riot_api._retry_after("malformed"), 10.0)
        self.assertEqual(riot_api._retry_after("-2"), 0.0)
        self.assertEqual(riot_api._retry_after("nan"), 10.0)
        self.assertEqual(riot_api._retry_after("inf"), 10.0)

    def test_server_failure_is_retried(self):
        self.get.side_effect = [self.response(503), self.response(data=[])]
        self.assertEqual(riot_api.make_request("https://example.test"), [])
        self.sleep.assert_called_once_with(5.0)
        self.assertEqual(self.wait.call_count, 2)

    def test_exhausted_server_failure_keeps_status(self):
        self.get.return_value = self.response(500)
        with self.assertRaises(riot_api.RiotAPIError) as caught:
            riot_api.make_request("https://example.test", raise_on_error=True)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_timeout_and_connection_error_retry_without_sensitive_logs(self):
        for error_type in (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            with self.subTest(error_type=error_type):
                self.get.reset_mock()
                self.sleep.reset_mock()
                self.logger.reset_mock()
                self.get.side_effect = error_type("RGAPI-secret-header")
                with self.assertRaises(riot_api.RiotAPIError) as caught:
                    riot_api.make_request("https://example.test", raise_on_error=True)
                self.assertIsNone(caught.exception.status_code)
                self.assertNotIn("RGAPI-secret-header", str(caught.exception))
                self.assertNotIn("RGAPI-secret-header", str(self.logger.mock_calls))
                self.assertEqual(self.get.call_count, 3)
                self.assertEqual(self.sleep.call_count, 2)

    def test_invalid_json_is_classified(self):
        self.get.return_value = self.response()
        self.get.return_value.json.side_effect = ValueError("response contains secret")
        with self.assertRaisesRegex(riot_api.RiotAPIError, "invalid JSON"):
            riot_api.make_request("https://example.test", raise_on_error=True)
        self.get.assert_called_once()
        self.assertNotIn("response contains secret", str(self.logger.mock_calls))

    def test_scalar_json_is_invalid(self):
        self.get.return_value = self.response(data="unexpected")
        with self.assertRaises(riot_api.RiotAPIError):
            riot_api.make_request("https://example.test", raise_on_error=True)

    def test_timeline_endpoint_uses_same_transport_strictly(self):
        with patch.object(riot_api, "make_request", return_value={}) as transport:
            riot_api.get_match_timeline("americas", "LA2_123")
        transport.assert_called_once_with(
            "https://americas.api.riotgames.com/lol/match/v5/matches/LA2_123/timeline",
            raise_on_error=True,
        )

    def test_details_legacy_and_strict_signatures(self):
        with patch.object(riot_api, "make_request", return_value={}) as transport:
            riot_api.get_match_details("asia", "KR_123")
            self.assertFalse(transport.call_args.kwargs["raise_on_error"])
            riot_api.get_match_details("asia", "KR_123", raise_on_error=True)
            self.assertTrue(transport.call_args.kwargs["raise_on_error"])


if __name__ == "__main__":
    unittest.main()
