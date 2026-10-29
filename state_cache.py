# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Debounced, atomic persistence for the display-state cache."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from typing import Any, Callable

from loguru import logger as log

try:  # Inside StreamController this is the required atomic writer.
    from src.backend.Utils.AtomicSaveUtils import atomic_save_json as _sc_atomic_save_json
except Exception:  # pragma: no cover - exercised by standalone tooling
    _sc_atomic_save_json = None


def _fallback_atomic_save_json(path: str, data: Any, indent: int = 4) -> None:
    """Atomic JSON write for contexts without StreamController's helper."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _default_schedule(delay: float, callback: Callable[[], None]):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


class StateCache:
    """Small write-behind cache of channel display state.

    Only text, colours, URLs/paths and scalar image layout values are stored;
    image bytes never reach this class.
    """

    def __init__(self, path: str, *, enabled: bool = True, debounce: float = 2.0,
                 max_channels: int = 500, clock: Callable[[], float] = time.monotonic,
                 schedule: Callable[[float, Callable[[], None]], Any] | None = None,
                 saver: Callable[..., None] | None = None):
        self.path = path
        self.enabled = enabled
        self.debounce = debounce
        self.max_channels = max_channels
        self._clock = clock
        self._schedule = schedule or _default_schedule
        self._saver = saver or _sc_atomic_save_json or _fallback_atomic_save_json
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._timer = None

    # ------------------------------------------------------------------ load
    def load(self) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as error:
            log.warning(f"WebSocket Client: could not read state cache: {error}")
            return {}

        channels = payload.get("channels") if isinstance(payload, dict) else None
        if not isinstance(channels, dict):
            return {}

        result: dict[str, dict[str, Any]] = {}
        for channel, entry in channels.items():
            if not isinstance(channel, str) or not isinstance(entry, dict):
                continue
            state = entry.get("state") if "state" in entry else entry
            if not isinstance(state, dict):
                continue
            result[channel] = {
                "state": copy.deepcopy(state),
                "meta": copy.deepcopy(entry.get("meta", {})) if isinstance(entry.get("meta"), dict) else {},
            }
            if len(result) >= self.max_channels:
                break
        with self._lock:
            self._data = copy.deepcopy(result)
            self._dirty = False
        return result

    # ----------------------------------------------------------------- write
    def set_state(self, channel: str, state: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        if not channel:
            return
        with self._lock:
            if channel not in self._data and len(self._data) >= self.max_channels:
                # Drop the oldest entry (dicts preserve insertion order).
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            self._data[channel] = {
                "state": copy.deepcopy(state),
                "meta": copy.deepcopy(meta or {}),
            }
            self._dirty = True
            self._arm_timer_locked()

    def delete_state(self, channel: str) -> None:
        with self._lock:
            if self._data.pop(channel, None) is not None:
                self._dirty = True
                self._arm_timer_locked()

    def all_states(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data)

    # ----------------------------------------------------------------- flush
    def flush(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
            if not self.enabled or not self._dirty:
                return
            payload = {
                "v": 1,
                "channels": {
                    channel: copy.deepcopy(entry)
                    for channel, entry in list(self._data.items())[:self.max_channels]
                },
            }
            self._dirty = False
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        try:
            self._saver(self.path, payload, indent=2)
        except Exception as error:
            log.warning(f"WebSocket Client: could not save state cache: {error}")
            with self._lock:
                self._dirty = True

    def close(self) -> None:
        self.flush()

    def _arm_timer_locked(self) -> None:
        if not self.enabled or self._timer is not None:
            return
        try:
            self._timer = self._schedule(self.debounce, self._timer_fired)
        except Exception as error:  # pragma: no cover - defensive
            log.debug(f"WebSocket Client: cache timer could not start: {error}")
            self._timer = None

    def _timer_fired(self) -> None:
        with self._lock:
            self._timer = None
        self.flush()
