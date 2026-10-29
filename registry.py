# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Channel registry, display-state merge and dispatch coalescing.

The registry is intentionally free of GTK, StreamController and ``globals``
imports.  Callers inject the dispatch function (which is responsible for
marshalling to the main thread) and optionally a state cache and clock.  This
keeps all routing and coalescing logic unit-testable outside the app.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from loguru import logger as log

from protocol import (VERSION, apply_patch, build_channel_entry, empty_state,
                      normalize_server_message)

DispatchFn = Callable[[Any, dict[str, Any] | None, int], None]


def _default_schedule(delay: float, callback: Callable[[], None]):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


class Registry:
    def __init__(self, instance_id: str = "", *, dispatch: DispatchFn | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 schedule: Callable[[float, Callable[[], None]], Any] | None = None,
                 coalesce: float = 0.025, cache: Any = None,
                 max_channels: int = 500,
                 on_channels_changed: Callable[[], None] | None = None):
        self.instance_id = instance_id
        self._dispatch = dispatch or (lambda action, state, revision: None)
        self._clock = clock
        self._wall_clock = wall_clock
        self._schedule = schedule or _default_schedule
        self._coalesce = coalesce
        self._cache = cache
        self._max_channels = max_channels
        self._on_channels_changed = on_channels_changed

        self._lock = threading.RLock()
        self._actions: dict[str, list[Any]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._revisions: dict[str, int] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._timers: dict[str, Any] = {}

    # ----------------------------------------------------------- persistence
    def load_cache(self) -> None:
        if self._cache is None:
            return
        try:
            loaded = self._cache.load()
        except Exception as error:
            log.warning(f"WebSocket Client: could not load display cache: {error}")
            return
        if not isinstance(loaded, dict):
            return
        with self._lock:
            for channel, entry in list(loaded.items())[:self._max_channels]:
                if not isinstance(channel, str) or not isinstance(entry, dict):
                    continue
                state = entry.get("state")
                if isinstance(state, dict):
                    self._states[channel] = copy.deepcopy(state)
                meta = entry.get("meta")
                if isinstance(meta, dict):
                    self._meta[channel] = copy.deepcopy(meta)

    # ---------------------------------------------------------- registration
    def register(self, channel: str, action: Any, meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Register a live action and return its cached state, if any."""
        if not isinstance(channel, str) or not channel.strip():
            return None
        channel = channel.strip()
        with self._lock:
            actions = self._actions.get(channel)
            if actions is not None and action in actions:
                if meta:
                    self._meta[channel] = copy.deepcopy(meta)
                return copy.deepcopy(self._states.get(channel))
            if actions is None and channel not in self._states and channel not in self._meta \
                    and len(self._actions) + len(self._states) >= self._max_channels:
                log.warning(f"WebSocket Client: channel limit reached, ignoring {channel}")
                return None
            if actions is None:
                actions = []
                self._actions[channel] = actions
            elif actions:
                log.info(f"WebSocket Client: channel {channel!r} is registered by {len(actions) + 1} actions; broadcasting")
            actions.append(action)
            if meta:
                self._meta[channel] = copy.deepcopy(meta)
            state = copy.deepcopy(self._states.get(channel))
        self._notify_channels_changed()
        return state

    def unregister(self, channel: str, action: Any) -> None:
        if not channel:
            return
        with self._lock:
            actions = self._actions.get(channel)
            if not actions:
                return
            self._actions[channel] = [item for item in actions if item is not action]
            if not self._actions[channel]:
                self._actions.pop(channel, None)
        self._notify_channels_changed()

    def set_meta(self, channel: str, meta: dict[str, Any]) -> None:
        if not channel or not isinstance(meta, dict):
            return
        with self._lock:
            self._meta[channel] = copy.deepcopy(meta)

    def is_registered(self, channel: str, action: Any) -> bool:
        with self._lock:
            return action in self._actions.get(channel, [])

    def all_actions(self) -> list[Any]:
        with self._lock:
            seen: list[Any] = []
            for actions in self._actions.values():
                for action in actions:
                    if action not in seen:
                        seen.append(action)
            return seen

    def channel_count(self) -> int:
        with self._lock:
            return len(set(self._actions) | set(self._states) | set(self._meta))

    # -------------------------------------------------------------- messages
    def handle_frame(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one parsed server frame and return an optional reply frame."""
        if isinstance(frame, dict) and frame.get("v") not in (VERSION, None):
            return None
        message = normalize_server_message(frame, instance=self.instance_id or None)
        if message is None:
            return None

        if message["type"] == "ping":
            return {"v": VERSION, "type": "pong", "ts": self._wall_clock()}
        if message["type"] == "hello_ack":
            log.info(f"WebSocket Client: server hello_ack ({message.get('server')!r})")
            return None

        channel = message["channel"]
        with self._lock:
            known = channel in self._actions or channel in self._states or channel in self._meta
            if not known:
                log.debug(f"WebSocket Client: ignoring message for unknown channel {channel!r}")
                return None

            pending = self._pending.get(channel)
            if pending is not None:
                base = copy.deepcopy(pending["state"])
            else:
                base = copy.deepcopy(self._states.get(channel))

            if message["type"] == "reset":
                new_state = None
                reset = True
            else:
                new_state = apply_patch(base, message["patch"], message["merge"])
                reset = False

            revision = self._revisions.get(channel, 0) + 1
            self._revisions[channel] = revision
            deadline = pending["deadline"] if pending else self._clock() + self._coalesce
            self._pending[channel] = {
                "state": new_state,
                "reset": reset,
                "revision": revision,
                "deadline": deadline,
            }
            if pending is None:
                self._arm_timer_locked(channel, self._coalesce)
        return None

    def handle_raw(self, raw: bytes | str) -> dict[str, Any] | None:
        from protocol import parse_frame

        frame = parse_frame(raw)
        if frame is None:
            return None
        return self.handle_frame(frame)

    def flush_due(self, now: float | None = None) -> None:
        """Flush every pending channel whose coalescing window has elapsed."""
        moment = self._clock() if now is None else now
        with self._lock:
            channels = [
                channel for channel, pending in self._pending.items()
                if pending["deadline"] <= moment
            ]
        for channel in channels:
            self.flush_channel(channel)

    def flush_all(self) -> None:
        with self._lock:
            channels = list(self._pending)
        for channel in channels:
            self.flush_channel(channel)

    def flush_channel(self, channel: str) -> None:
        with self._lock:
            pending = self._pending.pop(channel, None)
            if pending is None:
                return
            timer = self._timers.pop(channel, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

            revision = pending["revision"]
            reset = pending["reset"]
            state = pending["state"]

            if reset or state is None:
                self._states.pop(channel, None)
                if self._cache is not None:
                    try:
                        self._cache.delete_state(channel)
                    except Exception as error:
                        log.debug(f"WebSocket Client: cache delete failed: {error}")
            else:
                self._states[channel] = copy.deepcopy(state)
                if self._cache is not None:
                    try:
                        self._cache.set_state(channel, state, self._meta.get(channel, {}))
                    except Exception as error:
                        log.debug(f"WebSocket Client: cache write failed: {error}")

            actions = list(self._actions.get(channel, []))

        dispatch_state = None if reset else copy.deepcopy(state)
        if actions:
            self._notify_channels_changed()
        for action in actions:
            try:
                self._dispatch(action, dispatch_state, revision)
            except Exception as error:
                log.warning(f"WebSocket Client: dispatch failed for {channel!r}: {error}")

    # ------------------------------------------------------------- revisions
    def revision(self, channel: str) -> int:
        with self._lock:
            return self._revisions.get(channel, 0)

    def is_current(self, channel: str, revision: int) -> bool:
        """True when a delayed result still belongs to the newest update."""
        with self._lock:
            return revision >= self._revisions.get(channel, 0)

    # ---------------------------------------------------------- hello output
    def channel_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            channels = sorted(set(self._actions) | set(self._states) | set(self._meta))
            entries = []
            for channel in channels:
                meta = self._meta.get(channel, {})
                entries.append(build_channel_entry(
                    channel=channel,
                    deck=str(meta.get("deck", "")),
                    page=str(meta.get("page", "")),
                    input_type=str(meta.get("input", "key")),
                    identifier=str(meta.get("identifier", "")),
                    state=int(meta.get("state", 0) or 0),
                ))
            return entries

    def cached_state(self, channel: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(channel)
            return copy.deepcopy(state) if state is not None else None

    def _notify_channels_changed(self) -> None:
        if self._on_channels_changed is None:
            return
        try:
            self._on_channels_changed()
        except Exception as error:
            log.debug(f"WebSocket Client: channels-changed callback failed: {error}")

    def _arm_timer_locked(self, channel: str, delay: float) -> None:
        try:
            self._timers[channel] = self._schedule(delay, lambda: self.flush_channel(channel))
        except Exception as error:  # pragma: no cover - defensive
            log.debug(f"WebSocket Client: coalescing timer could not start: {error}")
            self.flush_channel(channel)

    def _current_state(self, channel: str) -> dict[str, Any]:
        state = self._states.get(channel)
        return copy.deepcopy(state) if state is not None else empty_state()
