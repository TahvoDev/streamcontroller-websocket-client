# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

import protocol


class ColorTests(unittest.TestCase):
    def test_hex_rgb_rgba_and_short_forms(self):
        self.assertEqual(protocol.parse_color("#ff5555"), [255, 85, 85, 255])
        self.assertEqual(protocol.parse_color("#30000080"), [48, 0, 0, 128])
        self.assertEqual(protocol.parse_color("0f8"), [0, 255, 136, 255])
        self.assertEqual(protocol.parse_color([1, 2, 3]), [1, 2, 3, 255])

    def test_invalid_colors_are_ignored(self):
        for value in ("red", "#12345", "#gggggg", [1, 2], [300, 0, 0], None, 42):
            self.assertIsNone(protocol.parse_color(value), value)


class TextTests(unittest.TestCase):
    def test_bare_string_is_center(self):
        self.assertEqual(protocol.normalize_text("Hello"), {"center": {"text": "Hello"}})

    def test_positions_and_colors(self):
        result = protocol.normalize_text({
            "top": "A",
            "center": {"text": "B", "color": "#112233"},
            "bottom": {"text": "C", "color": "nope"},
        })
        self.assertEqual(result["top"], {"text": "A"})
        self.assertEqual(result["center"], {"text": "B", "color": [17, 34, 51, 255]})
        self.assertEqual(result["bottom"], {"text": "C"})

    def test_garbage_returns_none(self):
        self.assertIsNone(protocol.normalize_text(None))
        self.assertIsNone(protocol.normalize_text(42))
        self.assertIsNone(protocol.normalize_text({}))


class MergeTests(unittest.TestCase):
    def test_merge_keeps_previous_text(self):
        state = protocol.empty_state()
        state = protocol.apply_patch(state, {"text": {"top": {"text": "A"}}}, merge=True)
        state = protocol.apply_patch(state, {"text": {"center": {"text": "B"}}}, merge=True)
        self.assertEqual(state["text"]["top"]["text"], "A")
        self.assertEqual(state["text"]["center"]["text"], "B")

    def test_replace_clears_first(self):
        state = protocol.apply_patch(protocol.empty_state(), {"text": {"top": {"text": "A"}}})
        state = protocol.apply_patch(state, {"text": {"center": {"text": "B"}}}, merge=False)
        self.assertEqual(state["text"]["top"]["text"], "")
        self.assertEqual(state["text"]["center"]["text"], "B")

    def test_background_null_clears_and_invalid_is_ignored(self):
        state = protocol.apply_patch(protocol.empty_state(), {"background": [1, 2, 3, 4]})
        self.assertEqual(state["background"], [1, 2, 3, 4])
        state = protocol.apply_patch(state, {"background": None})
        self.assertIsNone(state["background"])

    def test_image_and_overlay(self):
        frame = {"v": 1, "type": "set", "channel": "a", "image": {"url": "https://x/y.png", "size": 0.8}}
        message = protocol.normalize_server_message(frame)
        self.assertEqual(message["patch"]["image"]["url"], "https://x/y.png")
        state = protocol.apply_patch(protocol.empty_state(), message["patch"])
        self.assertEqual(state["image"]["size"], 0.8)
        state = protocol.apply_patch(state, {"image": None})
        self.assertIsNone(state["image"])

        frame = {"v": 1, "type": "set", "channel": "a", "overlay": "#ff000080"}
        overlay = protocol.normalize_server_message(frame)["patch"]["overlay"]
        self.assertEqual(overlay, {"color": [255, 0, 0, 128]})


class FrameTests(unittest.TestCase):
    def test_valid_frame(self):
        frame = protocol.parse_frame('{"v":1,"type":"ping"}')
        self.assertEqual(frame["type"], "ping")

    def test_version_rejection(self):
        self.assertIsNone(protocol.parse_frame('{"v":2,"type":"ping"}'))
        self.assertIsNone(protocol.parse_frame('{"v":true,"type":"ping"}'))
        self.assertIsNone(protocol.parse_frame('{"type":"ping"}'))

    def test_malformed_frames_never_raise(self):
        self.assertIsNone(protocol.parse_frame("not json"))
        self.assertIsNone(protocol.parse_frame(b"\xff\xfe"))
        self.assertIsNone(protocol.parse_frame("[]"))
        self.assertIsNone(protocol.parse_frame(None))

    def test_oversized_frame_is_dropped(self):
        payload = '{"v":1,"type":"ping","pad":"' + "x" * protocol.MAX_FRAME_BYTES + '"}'
        self.assertIsNone(protocol.parse_frame(payload))

    def test_instance_filtering(self):
        frame = {"v": 1, "type": "set", "channel": "a", "instance": "other", "text": "x"}
        self.assertIsNone(protocol.normalize_server_message(frame, instance="mine"))
        self.assertIsNotNone(protocol.normalize_server_message(frame, instance=None))

    def test_reset_requires_channel(self):
        self.assertIsNone(protocol.normalize_server_message({"v": 1, "type": "reset"}))
        message = protocol.normalize_server_message({"v": 1, "type": "reset", "channel": "a"})
        self.assertEqual(message, {"type": "reset", "channel": "a"})

    def test_builders(self):
        event = protocol.build_event("a", "i", "d", "p", "key", "0x1", 0, "short_up", {"x": 1}, 1.5)
        self.assertEqual(event["ts"], 1.5)
        self.assertEqual(event["event"], "short_up")
        self.assertEqual(protocol.build_channels([])["channels"], [])
        self.assertEqual(protocol.build_pong(2.0)["ts"], 2.0)
        with self.assertRaises(ValueError):
            protocol.build_event("a", "i", "d", "p", "key", "0x1", 0, "bogus")


if __name__ == "__main__":
    unittest.main()
