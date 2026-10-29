# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure SC/WS v1 protocol helpers.

This module is deliberately free of StreamController, GTK and filesystem
imports so it can be unit tested outside the application.  Nothing in here
raises on malformed input: bad frames are dropped and reported to the caller
by returning ``None``.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

from loguru import logger as log

VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024  # 1 MiB
EVENT_NAMES = ("down", "up", "short_up", "hold_start", "hold_stop")
TEXT_POSITIONS = ("top", "center", "bottom")

_MISSING = object()
_bad_versions_logged: set[Any] = set()


class _Invalid:
    """Sentinel used internally to mean "leave the previous value alone"."""


INVALID = _Invalid()


def parse_color(value: Any) -> list[int] | None:
    """Return ``[r, g, b, a]`` for a colour, or ``None`` if invalid.

    Accepted forms are ``#RRGGBB``, ``#RRGGBBAA`` (with or without ``#``) and
    three/four element integer sequences.  Alpha defaults to 255.
    """
    if isinstance(value, (list, tuple)):
        if len(value) not in (3, 4):
            return None
        try:
            channels = [int(channel) for channel in value]
        except (TypeError, ValueError):
            return None
        if any(channel < 0 or channel > 255 for channel in channels):
            return None
        if len(channels) == 3:
            channels.append(255)
        return channels

    if not isinstance(value, str):
        return None

    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(channel * 2 for channel in text)
    elif len(text) == 4:
        text = "".join(channel * 2 for channel in text)
    if len(text) not in (6, 8):
        return None
    try:
        channels = [int(text[index:index + 2], 16) for index in range(0, len(text), 2)]
    except ValueError:
        return None
    if len(channels) == 3:
        channels.append(255)
    return channels


def normalize_text(value: Any) -> dict[str, dict[str, Any]] | None:
    """Normalise the ``text`` field of a ``set`` message.

    A bare string is shorthand for the centre label.  A mapping may contain
    ``top``, ``center`` and/or ``bottom``; each value is either a string or an
    object with ``text`` and an optional ``color``.  Invalid colours are
    omitted so the previous colour is kept.  Returns ``None`` when nothing
    usable was supplied.
    """
    if isinstance(value, str):
        return {"center": {"text": value}}

    if not isinstance(value, dict):
        return None

    result: dict[str, dict[str, Any]] = {}
    for position in TEXT_POSITIONS:
        if position not in value:
            continue
        item = value[position]
        if isinstance(item, str):
            result[position] = {"text": item}
        elif isinstance(item, dict):
            entry: dict[str, Any] = {}
            if "text" in item:
                if item["text"] is None:
                    entry["text"] = ""
                elif isinstance(item["text"], (str, int, float, bool)):
                    entry["text"] = str(item["text"])
            color = parse_color(item.get("color"))
            if color is not None:
                entry["color"] = color
            if entry:
                result[position] = entry
    return result or None


def normalize_image(value: Any) -> dict[str, Any] | None | _Invalid:
    """Normalise an image reference.

    Returns a spec dict, ``None`` for an explicit clear, or :data:`INVALID`
    when the value should be ignored (keeping the previous image).
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = {"url": value}
    if not isinstance(value, dict):
        return INVALID

    spec: dict[str, Any] = {}
    url = value.get("url")
    path = value.get("path")
    if isinstance(url, str) and url.strip():
        spec["url"] = url.strip()
    elif isinstance(path, str) and path.strip():
        spec["path"] = path.strip()
    else:
        return INVALID

    for key in ("size", "valign", "halign"):
        if key not in value:
            continue
        try:
            spec[key] = float(value[key])
        except (TypeError, ValueError):
            continue
    if "size" in spec:
        spec["size"] = max(0.05, min(1.0, spec["size"]))
    if "valign" in spec:
        spec["valign"] = max(-1.0, min(1.0, spec["valign"]))
    if "halign" in spec:
        spec["halign"] = max(-1.0, min(1.0, spec["halign"]))
    return spec


def normalize_overlay(value: Any) -> dict[str, Any] | None | _Invalid:
    """Normalise the optional ``overlay`` field.

    An overlay is normally an image reference (same shape as ``image``), but a
    colour string is accepted as a solid full-key overlay for convenience.
    """
    if value is None:
        return None
    if isinstance(value, str):
        color = parse_color(value)
        if color is not None:
            return {"color": color}
        return normalize_image(value)
    if isinstance(value, dict):
        if "color" in value and "url" not in value and "path" not in value:
            color = parse_color(value.get("color"))
            return {"color": color} if color is not None else INVALID
        return normalize_image(value)
    return INVALID


def empty_state() -> dict[str, Any]:
    """Return the display state used when nothing has been received."""
    return {
        "text": {
            position: {"text": "", "color": None}
            for position in TEXT_POSITIONS
        },
        "image": None,
        "background": None,
        "overlay": None,
        "error": False,
    }


def apply_patch(state: dict[str, Any] | None, patch: dict[str, Any], merge: bool = True) -> dict[str, Any]:
    """Apply a normalised ``set`` patch to a state, returning a new dict."""
    result = copy.deepcopy(state) if (merge and state is not None) else empty_state()

    text_patch = patch.get("text")
    if isinstance(text_patch, dict):
        for position, entry in text_patch.items():
            if position not in TEXT_POSITIONS or not isinstance(entry, dict):
                continue
            current = result["text"].setdefault(position, {"text": "", "color": None})
            if "text" in entry:
                current["text"] = entry["text"]
            if "color" in entry:
                current["color"] = entry["color"]

    for key in ("image", "background", "overlay"):
        if key in patch:
            result[key] = copy.deepcopy(patch[key])

    if "error" in patch:
        result["error"] = bool(patch["error"])

    return result


def parse_frame(raw: bytes | str) -> dict[str, Any] | None:
    """Decode one frame, enforcing size and protocol version.

    Returns the decoded object for valid v1 frames and ``None`` otherwise.
    Wrong versions are logged once per distinct version value.
    """
    if isinstance(raw, str):
        try:
            data = raw.encode("utf-8")
        except UnicodeEncodeError:
            return None
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        return None

    if len(data) > MAX_FRAME_BYTES:
        log.debug(f"SC/WS: dropping oversized frame ({len(data)} bytes)")
        return None

    try:
        frame = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        log.debug("SC/WS: dropping malformed JSON frame")
        return None

    if not isinstance(frame, dict):
        log.debug("SC/WS: dropping non-object frame")
        return None

    version = frame.get("v")
    if isinstance(version, bool) or version != VERSION:
        try:
            hashable_version = version
            seen = hashable_version in _bad_versions_logged
        except TypeError:
            hashable_version = repr(version)
            seen = hashable_version in _bad_versions_logged
        if not seen:
            _bad_versions_logged.add(hashable_version)
            log.warning(f"SC/WS: ignoring frames with unsupported version {version!r}")
        return None

    return frame


def normalize_server_message(frame: dict[str, Any], instance: str | None = None) -> dict[str, Any] | None:
    """Turn a valid v1 frame from the server into a registry operation."""
    if not isinstance(frame, dict):
        return None

    message_type = frame.get("type")
    if message_type == "ping":
        return {"type": "ping"}
    if message_type == "hello_ack":
        server = frame.get("server")
        return {"type": "hello_ack", "server": server}

    if message_type not in ("set", "reset"):
        return None

    channel = frame.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        log.debug(f"SC/WS: ignoring {message_type} without a channel")
        return None
    channel = channel.strip()

    if instance is not None:
        remote_instance = frame.get("instance")
        if remote_instance is not None and remote_instance != instance:
            log.debug(f"SC/WS: ignoring {message_type} for instance {remote_instance!r}")
            return None

    if message_type == "reset":
        return {"type": "reset", "channel": channel}

    patch: dict[str, Any] = {}
    if "text" in frame:
        text = normalize_text(frame.get("text"))
        if text is not None:
            patch["text"] = text

    if "image" in frame:
        image = normalize_image(frame.get("image"))
        if image is not INVALID:
            patch["image"] = image

    if "background" in frame:
        value = frame["background"]
        if value is None:
            patch["background"] = None
        else:
            color = parse_color(value)
            if color is not None:
                patch["background"] = color

    if "overlay" in frame:
        overlay = normalize_overlay(frame.get("overlay"))
        if overlay is not INVALID:
            patch["overlay"] = overlay

    if "error" in frame and isinstance(frame.get("error"), bool):
        patch["error"] = frame["error"]

    merge = frame.get("merge", True) is not False
    return {"type": "set", "channel": channel, "merge": merge, "patch": patch}


def build_event(channel: str, instance: str, deck: str, page: str, input_type: str,
                identifier: str, state: int, event: str, extra: dict[str, Any] | None = None,
                timestamp: float | None = None) -> dict[str, Any]:
    if event not in EVENT_NAMES:
        raise ValueError(f"Unknown SC/WS v1 event {event!r}")
    return {
        "v": VERSION,
        "type": "event",
        "channel": channel,
        "instance": instance,
        "deck": deck,
        "page": page,
        "input": input_type,
        "identifier": identifier,
        "state": state,
        "event": event,
        "extra": extra or {},
        "ts": time.time() if timestamp is None else timestamp,
    }


def build_hello(instance: str, plugin_version: str, decks: list[dict[str, Any]],
                pages: list[str], channels: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "v": VERSION,
        "type": "hello",
        "instance": instance,
        "plugin": plugin_version,
        "decks": decks,
        "pages": pages,
        "channels": channels,
    }


def build_channels(channels: list[dict[str, Any]]) -> dict[str, Any]:
    return {"v": VERSION, "type": "channels", "channels": channels}


def build_pong(timestamp: float | None = None) -> dict[str, Any]:
    return {"v": VERSION, "type": "pong", "ts": time.time() if timestamp is None else timestamp}


def build_channel_entry(channel: str, deck: str = "", page: str = "", input_type: str = "key",
                        identifier: str = "", state: int = 0, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = {
        "channel": channel,
        "deck": deck,
        "page": page,
        "input": input_type,
        "identifier": identifier,
        "state": int(state),
    }
    if extra:
        entry.update(extra)
    return entry


def frame_size(raw: bytes | str) -> int:
    """Return the byte length of a frame (used by tests and logging)."""
    if isinstance(raw, str):
        return len(raw.encode("utf-8"))
    return len(raw or b"")
