# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Server-response verification (socket open is not the same as healthy)."""

from __future__ import annotations

import threading
import time
import unittest

import protocol
from tests.fake_server import FakeServer
from ws_client import WebSocketClient


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class VerificationTests(unittest.TestCase):
    def _start(self, auto_hello_ack: bool, verify_timeout: float | None,
               reconnect_interval: float = 0.5) -> None:
        self.server = FakeServer(auto_hello_ack=auto_hello_ack).start()
        self.addCleanup(self.server.stop)
        self.statuses: list[tuple[str, str]] = []
        self.status_lock = threading.Lock()
        self.client = WebSocketClient(
            url=f"ws://127.0.0.1:{self.server.port}",
            instance="verify-test",
            hello_provider=lambda: protocol.build_hello(
                instance="verify-test", plugin_version="1.0.0",
                decks=[], pages=["Main"], channels=[],
            ),
            on_status=self._on_status,
            verify_timeout=verify_timeout,
            reconnect_interval=reconnect_interval,
            ping_interval=5,
            ping_timeout=2,
        )
        self.addCleanup(self.client.shutdown)
        self.assertTrue(self.client.start())

    def _on_status(self, status: str, detail: str) -> None:
        with self.status_lock:
            self.statuses.append((status, detail))

    def _has_status(self, wanted: str, detail: str | None = None) -> bool:
        with self.status_lock:
            return any(status == wanted and (detail is None or text == detail)
                       for status, text in self.statuses)

    def test_ack_makes_the_connection_healthy(self):
        self._start(auto_hello_ack=True, verify_timeout=2.0)
        self.assertTrue(wait_until(lambda: self.client.healthy))
        self.assertTrue(self._has_status("connected"))

    def test_missing_ack_reports_waiting_and_stays_unhealthy(self):
        self._start(auto_hello_ack=False, verify_timeout=0.3)
        self.assertTrue(wait_until(lambda: self.client.connected))
        self.assertTrue(wait_until(lambda: self._has_status("waiting", "No response from server"), timeout=3))
        self.assertFalse(self.client.healthy)
        # A timeout must not close the socket or restart the reconnect loop.
        self.assertTrue(self.client.connected)
        self.assertFalse(self._has_status("reconnecting"))

        # A later valid frame promotes the connection without reconnecting.
        self.server.send({"v": 1, "type": "ping"})
        self.assertTrue(wait_until(lambda: self.client.healthy, timeout=3))
        self.assertTrue(self._has_status("connected"))

    def test_verification_disabled_connects_on_handshake(self):
        self._start(auto_hello_ack=False, verify_timeout=None)
        self.assertTrue(wait_until(lambda: self.client.healthy))
        self.assertFalse(self._has_status("waiting"))

    def test_verify_timer_is_cancelled_on_close_and_shutdown(self):
        self._start(auto_hello_ack=False, verify_timeout=30.0, reconnect_interval=30.0)
        self.assertTrue(wait_until(lambda: self.client.connected))
        self.assertIsNotNone(self.client._verify_timer)

        self.server.drop_clients()
        self.assertTrue(wait_until(lambda: self.client.connected is False))
        self.assertIsNone(self.client._verify_timer)

        self.client.shutdown()
        self.assertIsNone(self.client._verify_timer)

    def test_test_connection_can_require_a_response(self):
        server = FakeServer(auto_hello_ack=False).start()
        try:
            url = f"ws://127.0.0.1:{server.port}"
            ok, _detail = WebSocketClient.test_connection(url, timeout=1.0)
            self.assertTrue(ok)
            ok, detail = WebSocketClient.test_connection(url, timeout=1.0, require_response=True)
            self.assertFalse(ok)
            self.assertIn("no server response", detail)
        finally:
            server.stop()

        server = FakeServer(auto_hello_ack=True).start()
        try:
            url = f"ws://127.0.0.1:{server.port}"
            ok, _detail = WebSocketClient.test_connection(
                url,
                hello=protocol.build_hello(
                    instance="verify-test", plugin_version="1.0.0",
                    decks=[], pages=["Main"], channels=[],
                ),
                require_response=True,
                timeout=2.0,
            )
            self.assertTrue(ok)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
