import unittest
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ccxt

import exchange_handler
from exchange_handler import (
    ApiCircuitBreaker, CircuitOpenError, exchange_retry_delay, install_circuit_breaker,
)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.breaker = ApiCircuitBreaker(clock=lambda: self.now)

    def client(self, effects=None):
        transport = Mock(side_effect=effects)
        client = SimpleNamespace(fetch=transport, last_response_headers={})
        install_circuit_breaker(client, self.breaker)
        return client, transport

    def test_repeated_limits_trip_shared_gate_and_valid_probe_recovers(self):
        first, transport = self.client([
            ccxt.RateLimitExceeded("429 one"), {"ok": True},
            ccxt.DDoSProtection("418 two"), ccxt.RateLimitExceeded("429 three"),
            {"ok": True},
        ])
        second, second_transport = self.client()
        for expected in (ccxt.RateLimitExceeded, None, ccxt.DDoSProtection, ccxt.RateLimitExceeded):
            if expected:
                with self.assertRaises(expected):
                    first.fetch("https://example.test")
            else:
                first.fetch("https://example.test")
        self.assertEqual(self.breaker.retry_after(), 60)
        for elapsed in (0, 59.99):
            self.now = 100 + elapsed
            with self.assertRaises(CircuitOpenError):
                second.fetch("https://example.test")
        second_transport.assert_not_called()
        self.assertEqual(transport.call_count, 4)
        self.now = 160
        self.assertEqual(first.fetch("https://example.test"), {"ok": True})
        self.assertEqual(self.breaker.retry_after(), 0)
        second.fetch("https://example.test")
        second_transport.assert_called_once()

    def test_window_expires_and_only_one_recovery_probe_is_allowed(self):
        self.breaker.rate_limited()
        self.breaker.rate_limited()
        self.now += 61
        self.breaker.rate_limited()
        self.assertEqual(self.breaker.retry_after(), 0)
        self.breaker.rate_limited()
        self.breaker.rate_limited()
        self.now += 60
        probe = self.breaker.before_request()
        self.assertIsNotNone(probe)
        with self.assertRaises(CircuitOpenError):
            self.breaker.before_request()
        self.breaker.completed(probe, success=True)
        self.assertIsNone(self.breaker.before_request())

    def test_failed_probe_reopens_and_late_success_cannot_close_a_new_probe(self):
        for _ in range(3):
            self.breaker.rate_limited()
        self.now += 60
        old_probe = self.breaker.before_request()
        self.breaker.rate_limited()
        self.now += 60
        new_probe = self.breaker.before_request()
        self.breaker.completed(old_probe, success=True)
        with self.assertRaises(CircuitOpenError):
            self.breaker.before_request()
        self.breaker.completed(new_probe, success=False)
        self.assertEqual(self.breaker.retry_after(), 60)

    def test_retry_after_header_is_honored_and_main_waits_out_the_full_penalty(self):
        client, transport = self.client([ccxt.RateLimitExceeded("429")])
        client.last_response_headers = {"Retry-After": "120"}
        with self.assertRaises(ccxt.RateLimitExceeded) as caught:
            client.fetch("https://example.test")
        self.assertEqual(exchange_retry_delay(client, caught.exception, 1), 120)
        self.now += 60
        with self.assertRaises(CircuitOpenError) as caught:
            client.fetch("https://example.test")
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertEqual(transport.call_count, 1)

    def test_http_date_penalty_and_repeated_installation_are_supported(self):
        client, transport = self.client([ccxt.RateLimitExceeded("429")])
        wrapped = client.fetch
        install_circuit_breaker(client, self.breaker)
        self.assertIs(client.fetch, wrapped)
        client.last_response_headers = {"Retry-After": formatdate(1_700_000_120, usegmt=True)}
        with patch.object(exchange_handler.time, "time", return_value=1_700_000_000):
            with self.assertRaises(ccxt.RateLimitExceeded):
                client.fetch("https://example.test")
        self.assertEqual(self.breaker.retry_after(), 120)
        self.assertEqual(transport.call_count, 1)

    def test_factory_clients_share_the_gate_including_monitor_clients(self):
        first = SimpleNamespace(fetch=Mock(side_effect=ccxt.RateLimitExceeded("429")),
                                set_sandbox_mode=Mock(), last_response_headers={})
        second = SimpleNamespace(fetch=Mock(), set_sandbox_mode=Mock(), last_response_headers={})
        with patch.object(exchange_handler, "API_CIRCUIT_BREAKER", self.breaker), patch.object(
            exchange_handler.ccxt, "binance", side_effect=[first, second],
        ), patch.object(exchange_handler, "get_trading_settings", return_value=SimpleNamespace(
            api_key="fake", api_secret="fake", sandbox=True,
        )):
            one, two = exchange_handler.create_exchange(), exchange_handler.create_exchange()
        for _ in range(3):
            with self.assertRaises(ccxt.RateLimitExceeded):
                one.fetch("https://example.test")
        with self.assertRaises(CircuitOpenError):
            two.fetch("https://example.test")
        second.set_sandbox_mode.assert_called_once_with(True)

    def test_unrelated_network_failures_do_not_trip_a_closed_circuit(self):
        client, transport = self.client([ccxt.RequestTimeout("timeout")] * 4)
        for _ in range(4):
            with self.assertRaises(ccxt.RequestTimeout):
                client.fetch("https://example.test")
        self.assertEqual(self.breaker.retry_after(), 0)
        self.assertEqual(transport.call_count, 4)

    def test_real_ccxt_request_path_is_gated_before_transport(self):
        exchange = ccxt.binance({"enableRateLimit": False})
        transport = Mock(side_effect=ccxt.RateLimitExceeded("429"))
        exchange.fetch = transport
        install_circuit_breaker(exchange, self.breaker)
        for _ in range(3):
            with self.assertRaises(ccxt.RateLimitExceeded):
                exchange.public_get_ping()
        with self.assertRaises(CircuitOpenError):
            exchange.public_get_ping()
        self.assertEqual(transport.call_count, 3)


if __name__ == "__main__":
    unittest.main()
