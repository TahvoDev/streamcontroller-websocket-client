# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline-dot behaviour, unit level and against the fake server."""

from __future__ import annotations

import threading
import time
import unittest

from tests import sc_stubs

sc_stubs.install()

import protocol  # noqa: E402
from actions.WebSocketKey import WebSocketKey  # noqa: E402
from tests.fake_server import FakeServer  # noqa: E402
from ws_client import WebSocketClient  # noqa: E402


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make_key(**overrides) -> WebSocketKey:
    key = WebSocketKey()
    key.plugin_base = None
    key._ready = True
    key.on_ready_called = True
    key._registered_channel = "media.mute"
    settings = {"channel": "media.mute", "show_offline_dot": True}
    settings.update(overrides)
    key.set_settings(settings)
    return key


def calls(key: WebSocketKey, name: str):
    return [call for call in key.render_calls if call[0] == name]


class OfflineDotTests(unittest.TestCase):
    def test_dot_appears_and_is_transparent_away_from_the_corner(self):
        key = make_key()
        key.set_connected(False)

        self.assertEqual(len(calls(key, "show_overlay")), 1)
        self.assertEqual(len(calls(key, "hide_overlay")), 0)
        self.assertEqual(key._overlay_owner, "offline")

        # The dot is an overlay, not a replacement for the key image.
        self.assertEqual(len(calls(key, "set_media")), 0)
        image = calls(key, "show_overlay")[0][1][0]
        self.assertEqual(image.getpixel((8, 8))[3], 0)
        self.assertEqual(image.getpixel((20, 120))[3], 0)
        self.assertEqual(image.getpixel((124, 124))[3], 255)

    def test_dot_is_not_stacked_by_repeated_updates(self):
        key = make_key()
        key.set_connected(False)
        key.set_connected(False)
        key.set_connected(False)

        # A full redraw while offline must not add a second overlay either.
        key.apply_state(None)
        key.apply_state({"text": {"center": {"text": "STILL HERE"}}}, 0)

        self.assertEqual(len(calls(key, "show_overlay")), 1)
        self.assertEqual(len(calls(key, "hide_overlay")), 0)
        self.assertEqual(key._overlay_owner, "offline")

    def test_dot_disappears_on_reconnect_and_stays_gone(self):
        key = make_key()
        key.set_connected(False)
        key.set_connected(True)
        self.assertEqual(len(calls(key, "hide_overlay")), 1)
        self.assertIsNone(key._overlay_owner)

        # Repeated "connected" statuses must not keep calling hide_overlay.
        key.set_connected(True)
        self.assertEqual(len(calls(key, "hide_overlay")), 1)

        # A later redraw after reconnecting must not bring the dot back.
        key.apply_state({"text": {"center": {"text": "MUTED"}}}, 0)
        self.assertEqual(len(calls(key, "show_overlay")), 1)

    def test_disabled_setting_never_shows_the_dot(self):
        key = make_key(show_offline_dot=False)
        key.set_connected(False)
        key.set_connected(True)
        self.assertEqual(len(calls(key, "show_overlay")), 0)
        self.assertEqual(len(calls(key, "hide_overlay")), 0)

    def test_dot_does_not_stack_on_a_server_overlay(self):
        key = make_key()
        key.set_connected(False)
        self.assertEqual(key._overlay_owner, "offline")

        key.apply_state({"overlay": {"color": "#112233"}}, 0)
        self.assertEqual(key._overlay_owner, "server")
        self.assertEqual(len(calls(key, "show_overlay")), 2)
        solid = calls(key, "show_overlay")[-1][1][0]
        self.assertEqual(solid.getpixel((8, 8))[3], 255)

        # Reconnecting must not tear down a server-owned overlay.
        key.set_connected(True)
        self.assertEqual(key._overlay_owner, "server")
        self.assertEqual(len(calls(key, "hide_overlay")), 0)

        # Still offline, server clears its overlay -> the dot returns.
        key.set_connected(False)
        self.assertEqual(len(calls(key, "show_overlay")), 2)
        key.apply_state({"overlay": None}, 0)
        self.assertEqual(len(calls(key, "hide_overlay")), 1)
        self.assertEqual(key._overlay_owner, "offline")
        self.assertEqual(len(calls(key, "show_overlay")), 3)


class OfflineDotReconnectTests(unittest.TestCase):
    def setUp(self):
        self.key = make_key()
        self.key.set_connected(False)  # start offline, like plugin start-up
        self.server = FakeServer().start()
        self.client = None
        self.statuses: list[str] = []
        self.status_lock = threading.Lock()

    def tearDown(self):
        if self.client is not None:
            self.client.shutdown()
        self.server.stop()

    def _on_status(self, status: str, detail: str) -> None:
        with self.status_lock:
            self.statuses.append(status)
        self.key.set_connected(status == "connected")

    def make_client(self) -> None:
        self.client = WebSocketClient(
            url=f"ws://127.0.0.1:{self.server.port}",
            instance="offline-dot-test",
            hello_provider=lambda: protocol.build_hello(
                instance="offline-dot-test",
                plugin_version="0.1.0",
                decks=[],
                pages=["Main"],
                channels=[protocol.build_channel_entry(
                    "media.mute", "ABC", "Main", "key", "0x2", 0,
                )],
            ),
            on_status=self._on_status,
            reconnect_interval=0.5,
            ping_interval=5,
            ping_timeout=2,
        )
        self.assertTrue(self.client.start())

    def test_dot_follows_real_disconnect_and_reconnect(self):
        self.assertEqual(len(calls(self.key, "show_overlay")), 1)

        self.make_client()
        self.assertTrue(wait_until(lambda: self.client.connected))
        self.assertTrue(wait_until(lambda: self.key._overlay_owner is None))
        self.assertEqual(len(calls(self.key, "hide_overlay")), 1)

        # Short drop: the listener stays up and the client reconnects by itself.
        self.assertEqual(self.server.drop_clients(), 1)
        self.assertTrue(wait_until(lambda: self.client.connected is False))
        self.assertTrue(wait_until(lambda: self.key._overlay_owner == "offline", timeout=3))
        self.assertEqual(len(calls(self.key, "show_overlay")), 2)
        self.assertTrue(wait_until(lambda: self.client.connected, timeout=5))
        self.assertTrue(wait_until(lambda: self.key._overlay_owner is None))
        self.assertEqual(len(calls(self.key, "hide_overlay")), 2)
        self.assertEqual(len(calls(self.key, "show_overlay")), 2)

        # Longer outage: the whole listener goes away, so the dot must persist
        # through every failed retry, not just flash once.
        port = self.server.port
        self.server.stop()
        self.assertTrue(wait_until(lambda: self.key._overlay_owner == "offline", timeout=5))
        self.assertEqual(len(calls(self.key, "show_overlay")), 3)
        time.sleep(0.9)
        self.assertFalse(self.client.connected)
        self.assertEqual(len(calls(self.key, "show_overlay")), 3)
        self.assertEqual(self.key._overlay_owner, "offline")

        self.server = FakeServer(port=port).start()
        self.assertTrue(wait_until(lambda: self.client.connected, timeout=10))
        self.assertTrue(wait_until(lambda: self.key._overlay_owner is None))
        self.assertEqual(len(calls(self.key, "hide_overlay")), 3)
        self.assertEqual(len(calls(self.key, "show_overlay")), 3)

        # Server content arriving after recovery must not resurrect the dot.
        self.key.apply_state({"text": {"center": {"text": "MUTED"}}}, 0)
        self.assertIsNone(self.key._overlay_owner)
        self.assertEqual(len(calls(self.key, "show_overlay")), 3)


if __name__ == "__main__":
    unittest.main()
