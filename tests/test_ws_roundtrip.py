# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import time
import unittest

import protocol
from tests.fake_server import FakeServer
from ws_client import WebSocketClient


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer().start()
        self.received = []
        self.statuses = []
        self.status_lock = threading.Lock()
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.shutdown()
        self.server.stop()

    def make_client(self):
        def on_status(status, detail):
            with self.status_lock:
                self.statuses.append((status, detail))

        def on_frame(frame):
            self.received.append(frame)
            if frame.get("type") == "ping":
                self.client.send_json(protocol.build_pong())

        self.client = WebSocketClient(
            url=f"ws://127.0.0.1:{self.server.port}",
            instance="test-instance",
            hello_provider=lambda: protocol.build_hello(
                instance="test-instance", plugin_version="0.1.0",
                decks=[], pages=["Main"],
                channels=[protocol.build_channel_entry("media.mute", "ABC", "Main", "key", "0x2", 0)],
            ),
            on_frame=on_frame,
            on_status=on_status,
            reconnect_interval=0.2,
            ping_interval=5,
            ping_timeout=2,
        )
        self.assertTrue(self.client.start())
        self.assertTrue(wait_until(lambda: self.client.connected))
        self.assertTrue(self.server.wait_for(
            lambda messages: any(item.get("type") == "hello" for item in messages)
        ))

    def latest(self, message_type):
        messages = self.server.messages_of_type(message_type)
        return messages[-1] if messages else None

    def test_hello_channels_event_set_reset(self):
        self.make_client()

        hello = self.latest("hello")
        self.assertEqual(hello["v"], 1)
        self.assertEqual(hello["instance"], "test-instance")
        self.assertEqual(hello["pages"], ["Main"])
        self.assertEqual(hello["channels"][0]["channel"], "media.mute")

        self.client.send_json(protocol.build_channels([
            protocol.build_channel_entry("media.mute", "ABC", "Main", "key", "0x2", 0),
        ]))
        self.assertTrue(self.server.wait_for(
            lambda messages: any(item.get("type") == "channels" for item in messages)
        ))

        self.client.send_json(protocol.build_event(
            "media.mute", "test-instance", "ABC", "Main", "key", "0x2", 0, "short_up",
            {"room": "office"}, timestamp=123.5,
        ))
        self.assertTrue(self.server.wait_for(
            lambda messages: any(item.get("type") == "event" for item in messages)
        ))
        event = self.latest("event")
        self.assertEqual(event["event"], "short_up")
        self.assertEqual(event["extra"], {"room": "office"})
        self.assertEqual(event["ts"], 123.5)

        self.server.send({
            "v": 1, "type": "set", "channel": "media.mute",
            "text": {"center": {"text": "MUTED", "color": "#ff5555"}},
            "background": "#300000",
        })
        self.assertTrue(wait_until(lambda: any(item["type"] == "set" for item in self.received)))
        message = protocol.normalize_server_message(self.received[-1], "test-instance")
        self.assertEqual(message["patch"]["background"], [48, 0, 0, 255])

        self.server.send({
            "v": 1, "type": "set", "channel": "media.mute",
            "image": {"url": "https://example.invalid/mute.png"},
        })
        self.assertTrue(wait_until(lambda: self.received[-1].get("image") is not None))

        self.server.send({"v": 1, "type": "reset", "channel": "media.mute"})
        self.assertTrue(wait_until(lambda: self.received[-1].get("type") == "reset"))

    def test_ping_gets_pong(self):
        self.make_client()
        self.server.send({"v": 1, "type": "ping"})
        self.assertTrue(self.server.wait_for(
            lambda messages: any(item.get("type") == "pong" for item in messages)
        ))

    def test_invalid_frames_are_ignored(self):
        self.make_client()
        self.server.send("this is not json")
        self.server.send({"v": 2, "type": "set", "channel": "media.mute", "text": "bad"})
        self.server.send('{"v":1,"type":"set","channel":"media.mute","pad":"' + "x" * (1024 * 1024) + '"}')
        self.server.send({"v": 1, "type": "set", "channel": "media.mute", "text": "good"})
        self.assertTrue(wait_until(
            lambda: any(item.get("text") == "good" for item in self.received), timeout=8.0,
        ))
        self.assertTrue(self.client.connected)

    def test_server_restart_reconnects(self):
        self.make_client()
        port = self.server.port
        self.server.stop()
        self.assertTrue(wait_until(
            lambda: any(status == "reconnecting" for status, _detail in self.statuses), timeout=8.0,
        ))
        self.server = FakeServer(port=port).start()
        self.assertTrue(wait_until(
            lambda: sum(1 for status, _detail in self.statuses if status == "connected") >= 2,
            timeout=10.0,
        ))
        self.assertTrue(self.server.wait_for(
            lambda messages: any(item.get("type") == "hello" for item in messages), timeout=5.0,
        ))

    def test_test_connection(self):
        ok, detail = WebSocketClient.test_connection(
            f"ws://127.0.0.1:{self.server.port}", timeout=3.0,
            hello={"v": 1, "type": "hello", "instance": "probe"},
        )
        self.assertTrue(ok, detail)
        self.assertIn("Connected", detail)


if __name__ == "__main__":
    unittest.main()
