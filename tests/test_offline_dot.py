# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline/waiting dot behaviour, unit level and against the fake server."""

from __future__ import annotations

import threading
import time
import unittest

from PIL import Image

from tests import sc_stubs

sc_stubs.install()

import protocol  # noqa: E402
from actions.WebSocketKey import WebSocketKey  # noqa: E402
from tests.fake_server import FakeServer  # noqa: E402
from ws_client import WebSocketClient  # noqa: E402

AMBER = (245, 166, 35)
RED = (225, 40, 40)


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


def shown_image(key: WebSocketKey) -> Image.Image | None:
    media = calls(key, "set_media")
    if not media:
        return None
    return media[-1][2].get("image")


def dot_center(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    margin = max(2, round(min(width, height) * 0.04))
    diameter = max(6, round(min(width, height) * 0.18))
    return min(width - 1, width - margin - diameter // 2), min(height - 1, height - margin - diameter // 2)


def dot_colour(key: WebSocketKey) -> tuple[int, int, int] | None:
    image = shown_image(key)
    if image is None:
        return None
    red, green, blue, alpha = image.getpixel(dot_center(image))
    if alpha < 200:
        return None
    colour = (red, green, blue)
    if close_to(colour, RED) or close_to(colour, AMBER):
        return colour
    return None


def close_to(colour, expected, tolerance: int = 12) -> bool:
    return colour is not None and all(
        abs(component - target) <= tolerance
        for component, target in zip(colour, expected)
    )


def dot_pixel_count(key: WebSocketKey, expected=RED) -> int:
    image = shown_image(key)
    if image is None:
        return 0
    count = 0
    for red, green, blue, alpha in image.getdata():
        if alpha > 200 and close_to((red, green, blue), expected):
            count += 1
    return count


class OfflineDotTests(unittest.TestCase):
    def test_dot_is_composited_into_the_key_image(self):
        key = make_key()
        key.set_connected(False)

        self.assertEqual(len(calls(key, "show_overlay")), 0)
        self.assertEqual(len(calls(key, "set_media")), 1)
        image = shown_image(key)
        self.assertIsNotNone(image)
        self.assertEqual(image.getpixel((8, 8))[3], 0)
        self.assertTrue(close_to(dot_colour(key), RED))

    def test_waiting_is_amber_and_offline_is_red(self):
        key = make_key()
        key.set_connection_state("waiting")
        self.assertTrue(close_to(dot_colour(key), AMBER))
        key.set_connection_state("offline")
        self.assertTrue(close_to(dot_colour(key), RED))
        key.set_connection_state("connected")
        self.assertIsNone(dot_colour(key))

    def test_dot_survives_full_redraws_while_offline(self):
        key = make_key()
        key.set_connected(False)
        key._server_overlay_active = False

        # StreamController's hide_error() calls hide_overlay(); that used to
        # wipe the dot while the owner flag prevented it from coming back.
        key.apply_state(None)
        self.assertTrue(close_to(dot_colour(key), RED))
        key.apply_state({"text": {"center": {"text": "STILL HERE"}}}, 0)
        self.assertTrue(close_to(dot_colour(key), RED))
        self.assertEqual(len(calls(key, "show_overlay")), 0)
        self.assertEqual(calls(key, "set_center_label")[-1][1][0], "STILL HERE")

    def test_dot_survives_full_redraws_while_waiting(self):
        key = make_key()
        key.set_connection_state("waiting")
        key.apply_state(None)
        self.assertTrue(close_to(dot_colour(key), AMBER))
        key.apply_state({"text": {"center": {"text": "WAITING"}}}, 0)
        self.assertTrue(close_to(dot_colour(key), AMBER))

    def test_dot_does_not_replace_the_base_image(self):
        key = make_key()
        key._base_image = Image.new("RGBA", (144, 144), (0, 128, 0, 255))
        key.set_connected(False)

        image = shown_image(key)
        self.assertEqual(image.getpixel((8, 8)), (0, 128, 0, 255))
        self.assertTrue(close_to(dot_colour(key), RED))

        key.set_connected(True)
        image = shown_image(key)
        self.assertEqual(image.getpixel((8, 8)), (0, 128, 0, 255))
        self.assertIsNone(dot_colour(key))

    def test_dot_disappears_on_reconnect_and_stays_gone(self):
        key = make_key()
        key.set_connected(False)
        self.assertTrue(close_to(dot_colour(key), RED))
        key.set_connected(True)
        self.assertIsNone(dot_colour(key))

        key.set_connected(True)
        key.apply_state({"text": {"center": {"text": "MUTED"}}}, 0)
        self.assertIsNone(dot_colour(key))

    def test_repeated_updates_do_not_stack_dots(self):
        key = make_key()
        key.set_connected(False)
        expected = dot_pixel_count(key)
        self.assertGreater(expected, 50)
        for _ in range(4):
            key.set_connected(False)
            key.apply_state({"text": {"center": {"text": "DOWN"}}}, 0)
        self.assertEqual(dot_pixel_count(key), expected)

    def test_disabled_setting_never_shows_the_dot(self):
        key = make_key(show_offline_dot=False)
        key.set_connected(False)
        key.set_connection_state("waiting")
        self.assertIsNone(dot_colour(key))

    def test_server_overlay_takes_precedence(self):
        key = make_key()
        key.set_connected(False)
        key.apply_state({"overlay": {"color": "#112233"}}, 0)
        self.assertEqual(key._overlay_owner, "server")
        self.assertEqual(len(calls(key, "show_overlay")), 1)
        self.assertIsNone(dot_colour(key))
        solid = calls(key, "show_overlay")[-1][1][0]
        self.assertEqual(solid.getpixel((8, 8))[3], 255)

        # Reconnecting must not tear down a server-owned overlay.
        key.set_connected(True)
        self.assertEqual(key._overlay_owner, "server")
        self.assertEqual(len(calls(key, "hide_overlay")), 0)

        # Clearing the overlay while offline restores the composited dot.
        key.set_connected(False)
        key.apply_state({"overlay": None}, 0)
        self.assertGreaterEqual(len(calls(key, "hide_overlay")), 1)
        self.assertIsNone(key._overlay_owner)
        self.assertTrue(close_to(dot_colour(key), RED))

    def test_dot_size_does_not_shrink_with_server_image_size(self):
        key = make_key()
        key._base_image = Image.new("RGBA", (144, 144), (0, 0, 255, 255))
        key._base_layout = {}
        key.set_connected(False)
        full_size_count = dot_pixel_count(key)

        key._base_layout = {"size": 0.5, "valign": 0, "halign": 0}
        key.set_connected(False)
        self.assertEqual(dot_pixel_count(key), full_size_count)
        self.assertTrue(close_to(dot_colour(key), RED))
        # Top-left is outside the half-sized server image.
        self.assertEqual(shown_image(key).getpixel((8, 8))[3], 0)


class OfflineDotReconnectTests(unittest.TestCase):
    def setUp(self):
        self.key = make_key()
        self.key.set_connected(False)  # start offline, like plugin start-up
        # No automatic ack, so the waiting (amber) state is observable and
        # the test decides when the server proves it speaks the protocol.
        self.server = FakeServer(auto_hello_ack=False).start()
        self.client = None
        self.statuses: list[tuple[str, str]] = []
        self.status_lock = threading.Lock()

    def tearDown(self):
        if self.client is not None:
            self.client.shutdown()
        self.server.stop()

    def _on_status(self, status: str, detail: str) -> None:
        with self.status_lock:
            self.statuses.append((status, detail))
        state = "connected" if status == "connected" else ("waiting" if status == "waiting" else "offline")
        self.key.set_connection_state(state)

    def make_client(self, verify_timeout: float | None = 5.0) -> None:
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
            verify_timeout=verify_timeout,
            reconnect_interval=0.2,
            ping_interval=5,
            ping_timeout=2,
        )
        self.assertTrue(self.client.start())

    def prove_protocol(self) -> None:
        self.server.send({"v": 1, "type": "ping"})
        self.assertTrue(wait_until(lambda: self.client.healthy, timeout=5))
        self.assertTrue(wait_until(lambda: dot_colour(self.key) is None, timeout=5))

    def test_dot_follows_waiting_connected_and_offline(self):
        self.assertTrue(close_to(dot_colour(self.key), RED))

        self.make_client()
        self.assertTrue(wait_until(lambda: self.client.connected))
        self.assertTrue(wait_until(lambda: close_to(dot_colour(self.key), AMBER), timeout=3))
        self.prove_protocol()
        self.assertIsNone(dot_colour(self.key))

        # Short drop: the listener stays up and the client reconnects by itself.
        self.assertEqual(self.server.drop_clients(), 1)
        self.assertTrue(wait_until(lambda: self.client.connected is False))
        self.assertTrue(wait_until(lambda: close_to(dot_colour(self.key), RED), timeout=3))
        self.assertTrue(wait_until(lambda: self.client.connected, timeout=5))
        self.assertTrue(wait_until(lambda: close_to(dot_colour(self.key), AMBER), timeout=3))
        self.prove_protocol()
        self.assertIsNone(dot_colour(self.key))

        # Longer outage: the whole listener goes away, so the dot must persist
        # through every failed retry, not just flash once.
        port = self.server.port
        self.server.stop()
        self.assertTrue(wait_until(lambda: close_to(dot_colour(self.key), RED), timeout=5))
        time.sleep(0.6)
        self.assertFalse(self.client.connected)
        self.assertTrue(close_to(dot_colour(self.key), RED))

        self.server = FakeServer(port=port, auto_hello_ack=False).start()
        self.assertTrue(wait_until(lambda: self.client.connected, timeout=15))
        self.assertTrue(wait_until(lambda: close_to(dot_colour(self.key), AMBER), timeout=3))
        self.prove_protocol()
        self.assertIsNone(dot_colour(self.key))

        # Server content arriving after recovery must not resurrect the dot.
        self.key.apply_state({"text": {"center": {"text": "MUTED"}}}, 0)
        self.assertIsNone(dot_colour(self.key))


class VerifyTimeoutDotTests(unittest.TestCase):
    def test_waiting_dot_persists_after_timeout_and_clears_on_frame(self):
        key = make_key()
        key.set_connection_state("offline")
        server = FakeServer(auto_hello_ack=False).start()
        self.addCleanup(server.stop)
        statuses: list[tuple[str, str]] = []
        client = None
        try:
            def on_status(status: str, detail: str) -> None:
                statuses.append((status, detail))
                state = "connected" if status == "connected" else (
                    "waiting" if status == "waiting" else "offline")
                key.set_connection_state(state)

            client = WebSocketClient(
                url=f"ws://127.0.0.1:{server.port}",
                instance="verify-test",
                hello_provider=lambda: protocol.build_hello(
                    instance="verify-test", plugin_version="0.1.0",
                    decks=[], pages=["Main"], channels=[],
                ),
                on_status=on_status,
                verify_timeout=0.3,
                reconnect_interval=0.5,
                ping_interval=5,
                ping_timeout=2,
            )
            self.assertTrue(client.start())
            self.assertTrue(wait_until(lambda: client.connected))
            self.assertTrue(wait_until(lambda: close_to(dot_colour(key), AMBER), timeout=3))

            # Timeout does not reconnect or relabel the state as offline.
            self.assertTrue(wait_until(
                lambda: any(status == "waiting" and detail == "No response from server"
                            for status, detail in statuses),
                timeout=3,
            ))
            self.assertFalse(client.healthy)
            self.assertTrue(close_to(dot_colour(key), AMBER))
            self.assertFalse(any(status == "reconnecting" for status, _detail in statuses))
            self.assertTrue(client.connected)

            # Any valid SC/WS frame proves the server is actually speaking
            # the protocol, so the dot clears without a reconnect.
            server.send({"v": 1, "type": "ping"})
            self.assertTrue(wait_until(lambda: client.healthy, timeout=3))
            self.assertTrue(wait_until(lambda: dot_colour(key) is None, timeout=3))
        finally:
            if client is not None:
                client.shutdown()


if __name__ == "__main__":
    unittest.main()
