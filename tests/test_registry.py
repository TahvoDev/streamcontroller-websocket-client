# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from registry import Registry


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeCache:
    def __init__(self, initial=None):
        self.data = initial or {}
        self.deleted = []
        self.saved = []

    def load(self):
        return self.data

    def set_state(self, channel, state, meta=None):
        self.data[channel] = {"state": state, "meta": meta or {}}
        self.saved.append(channel)

    def delete_state(self, channel):
        self.data.pop(channel, None)
        self.deleted.append(channel)


class FakeAction:
    def __init__(self, name="action"):
        self.name = name
        self.calls = []

    def __repr__(self):
        return f"<FakeAction {self.name}>"

    def apply_state(self, state, revision):
        self.calls.append((state, revision))


def set_frame(channel, **fields):
    frame = {"v": 1, "type": "set", "channel": channel}
    frame.update(fields)
    return frame


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.timers = []
        self.dispatched = []

        def schedule(delay, callback):
            timer = FakeTimer()
            self.timers.append((self.clock() + delay, callback, timer))
            return timer

        def dispatch(action, state, revision):
            action.apply_state(state, revision)
            self.dispatched.append((action, state, revision))

        self.registry = Registry(
            instance_id="test-instance",
            dispatch=dispatch,
            clock=self.clock,
            schedule=schedule,
            coalesce=0.025,
            cache=FakeCache(),
        )

    def flush_due(self):
        self.registry.flush_due(self.clock())

    def run_timers(self):
        due = [item for item in self.timers if item[0] <= self.clock()]
        self.timers = [item for item in self.timers if item[0] > self.clock()]
        for _when, callback, _timer in due:
            callback()

    def test_register_duplicate_channel_broadcasts(self):
        one, two = FakeAction("one"), FakeAction("two")
        self.registry.register("media.mute", one, {"deck": "D1"})
        self.registry.register("media.mute", two, {"deck": "D1"})
        self.registry.handle_frame(set_frame("media.mute", text="MUTED"))
        self.clock.advance(0.03)
        self.run_timers()
        self.assertEqual(len(one.calls), 1)
        self.assertEqual(len(two.calls), 1)
        self.assertEqual(one.calls[0][0]["text"]["center"]["text"], "MUTED")
        self.assertEqual(self.registry.channel_count(), 1)

    def test_register_is_idempotent_for_same_action(self):
        action = FakeAction()
        self.registry.register("a", action, {})
        self.registry.register("a", action, {})
        self.registry.handle_frame(set_frame("a", text="x"))
        self.clock.advance(0.03)
        self.run_timers()
        self.assertEqual(len(action.calls), 1)

    def test_cache_state_is_returned_on_ready(self):
        cache = FakeCache({"cached": {"state": {"text": {"top": {"text": "T", "color": None},
                                                          "center": {"text": "C", "color": None},
                                                          "bottom": {"text": "B", "color": None}},
                                             "image": None, "background": [1, 2, 3, 255],
                                             "overlay": None, "error": False},
                                  "meta": {"deck": "ABC"}}})
        registry = Registry(dispatch=lambda *args: None, cache=cache)
        registry.load_cache()
        state = registry.register("cached", FakeAction(), {"deck": "ABC"})
        self.assertEqual(state["text"]["center"]["text"], "C")
        self.assertEqual(registry.channel_entries()[0]["deck"], "ABC")

    def test_coalesces_updates_within_window(self):
        action = FakeAction()
        self.registry.register("a", action, {})
        self.registry.handle_frame(set_frame("a", text="one"))
        self.clock.advance(0.010)
        self.registry.handle_frame(set_frame("a", background="#112233"))
        self.clock.advance(0.010)
        self.registry.handle_frame(set_frame("a", image={"url": "https://example/x.png"}))
        self.assertEqual(self.dispatched, [])
        self.clock.advance(0.010)
        self.flush_due()
        self.assertEqual(len(action.calls), 1)
        state = action.calls[0][0]
        self.assertEqual(state["text"]["center"]["text"], "one")
        self.assertEqual(state["background"], [17, 34, 51, 255])
        self.assertEqual(state["image"]["url"], "https://example/x.png")

    def test_reset_clears_cache_and_dispatches_none(self):
        cache = self.registry._cache
        action = FakeAction()
        self.registry.register("a", action, {})
        self.registry.handle_frame(set_frame("a", text="hello"))
        self.clock.advance(0.03)
        self.run_timers()
        self.assertIn("a", cache.data)
        self.registry.handle_frame({"v": 1, "type": "reset", "channel": "a"})
        self.clock.advance(0.03)
        self.run_timers()
        self.assertIn("a", cache.deleted)
        self.assertIsNone(action.calls[-1][0])

    def test_unknown_channel_is_ignored(self):
        self.registry.handle_frame(set_frame("nope", text="x"))
        self.clock.advance(0.03)
        self.run_timers()
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.registry.channel_count(), 0)

    def test_instance_mismatch_is_ignored(self):
        action = FakeAction()
        self.registry.register("a", action, {})
        self.registry.handle_frame(set_frame("a", instance="other", text="x"))
        self.clock.advance(0.03)
        self.run_timers()
        self.assertEqual(action.calls, [])

    def test_ping_returns_pong(self):
        reply = self.registry.handle_frame({"v": 1, "type": "ping"})
        self.assertEqual(reply["type"], "pong")
        self.assertIn("ts", reply)

    def test_stale_revisions_are_rejected(self):
        action = FakeAction()
        self.registry.register("a", action, {})
        self.registry.handle_frame(set_frame("a", image={"url": "https://example/a.png"}))
        self.clock.advance(0.03)
        self.run_timers()
        first_revision = action.calls[-1][1]
        self.clock.advance(0.03)
        self.registry.handle_frame(set_frame("a", image={"url": "https://example/b.png"}))
        self.clock.advance(0.03)
        self.run_timers()
        second_revision = action.calls[-1][1]
        self.assertGreater(second_revision, first_revision)
        self.assertFalse(self.registry.is_current("a", first_revision))
        self.assertTrue(self.registry.is_current("a", second_revision))

    def test_channel_entries_include_cached_and_live(self):
        self.registry.register("live", FakeAction(), {
            "deck": "ABC", "page": "Main", "input": "key", "identifier": "0x1", "state": 0,
        })
        self.registry.handle_frame(set_frame("live", text="x"))
        self.clock.advance(0.03)
        self.run_timers()
        cached = self.registry.cached_state("live")
        self.assertEqual(cached["text"]["center"]["text"], "x")
        entries = self.registry.channel_entries()
        self.assertEqual(entries[0]["channel"], "live")
        self.assertEqual(entries[0]["identifier"], "0x1")


if __name__ == "__main__":
    unittest.main()
