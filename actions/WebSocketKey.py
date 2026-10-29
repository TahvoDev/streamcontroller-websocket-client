# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""The WebSocket Key action: event publishing and server-driven rendering."""

from __future__ import annotations

import io
import json
import os
import threading
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk
from loguru import logger as log
from PIL import Image, ImageOps

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.PluginManager.EventAssigner import EventAssigner

try:
    from ..protocol import parse_color
except ImportError:
    from protocol import parse_color

import requests

DEFAULT_SETTINGS = {
    "channel": "",
    "default_text": "",
    "default_icon": "",
    "extra": "{}",
    "show_offline_dot": True,
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TIMEOUT = 5
IMAGE_CACHE_SIZE = 32
WHITE = [255, 255, 255, 255]
TRANSPARENT = [0, 0, 0, 0]


class WebSocketKey(ActionCore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._ready = False
        self._registered_channel: str | None = None
        self._connection_state = "offline"
        self._last_state: dict[str, Any] | None = None
        self._image_seq = 0
        self._overlay_seq = 0
        self._server_overlay_active = False
        self._overlay_owner: str | None = None
        self._image_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self._image_cache_lock = threading.Lock()
        self._extra_warning_emitted = False
        # Base (dot-free) face plus the layout StreamController should apply.
        # The offline dot is composited onto a copy of it, because
        # StreamController overlays replace the whole key image.
        self._base_image: Image.Image | None = None
        self._base_path: str = ""
        self._base_layout: dict[str, Any] = {}
        self._default_icon_cache: tuple[str, Image.Image] | None = None
        self._dot_cache: dict[tuple[tuple[int, int], str], Image.Image] = {}

        self.add_event_assigner(EventAssigner(
            id="key-down", ui_label="Key Down", default_event=Input.Key.Events.DOWN,
            callback=self._on_down,
        ))
        self.add_event_assigner(EventAssigner(
            id="key-up", ui_label="Key Up", default_event=Input.Key.Events.UP,
            callback=self._on_up,
        ))
        self.add_event_assigner(EventAssigner(
            id="key-short-up", ui_label="Key Short Up", default_event=Input.Key.Events.SHORT_UP,
            callback=self._on_short_up,
        ))
        self.add_event_assigner(EventAssigner(
            id="key-hold-start", ui_label="Key Hold Start", default_event=Input.Key.Events.HOLD_START,
            callback=self._on_hold_start,
        ))
        self.add_event_assigner(EventAssigner(
            id="key-hold-stop", ui_label="Key Hold Stop", default_event=Input.Key.Events.HOLD_STOP,
            callback=self._on_hold_stop,
        ))

    # ------------------------------------------------------------------ setup
    def on_ready(self) -> None:
        self._ready = True
        channel = self.channel()
        self._registered_channel = channel
        cached = self.plugin_base.registry.register(channel, self, self.event_metadata()[1]) \
            if self.plugin_base and hasattr(self.plugin_base, "registry") else None
        if cached is None:
            self._render_defaults()
        else:
            revision = self.plugin_base.registry.revision(channel)
            self.apply_state(cached, revision)
        state = "offline"
        if self.plugin_base:
            try:
                state = self.plugin_base.connection_state()
            except Exception:
                state = "connected" if self.plugin_base.is_connected() else "offline"
        self.set_connection_state(state)

    def on_remove(self) -> None:
        self._unregister()

    def on_removed_from_cache(self) -> None:
        self._unregister()

    def _unregister(self) -> None:
        self._ready = False
        if self.plugin_base and hasattr(self.plugin_base, "registry"):
            channel = self._registered_channel or self.channel()
            try:
                self.plugin_base.registry.unregister(channel, self)
            except Exception as error:
                log.debug(f"WebSocket Client: unregister failed: {error}")
        self._registered_channel = None

    def on_tick(self) -> None:
        # Deliberately nothing: all updates are pushed by the registry.
        pass

    # --------------------------------------------------------------- settings
    def _settings(self) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        try:
            saved = super().get_settings() or {}
        except Exception:
            saved = {}
        for key in DEFAULT_SETTINGS:
            if key in saved:
                result[key] = saved[key]
        result["channel"] = str(result.get("channel") or "")
        result["extra"] = str(result.get("extra") if result.get("extra") is not None else "{}")
        return result

    def _save(self, **updates) -> None:
        settings = self._settings()
        settings.update(updates)
        try:
            super().set_settings(settings)
        except Exception as error:
            log.warning(f"WebSocket Client: could not save action settings: {error}")

    def channel(self) -> str:
        configured = self._settings().get("channel", "").strip()
        if configured:
            return configured
        return self.generated_channel()

    def generated_channel(self) -> str:
        serial = "unknown"
        try:
            serial = self.deck_controller.safe_serial_number()
        except Exception:
            pass
        input_type = "key"
        try:
            mapping = {
                "keys": "key", "dials": "dial", "touch_keys": "touch_key",
                "touchscreens": "touch", "screens": "screen",
            }
            input_type = mapping.get(self.input_ident.input_type, self.input_ident.input_type)
        except Exception:
            pass
        identifier = ""
        try:
            identifier = self.input_ident.json_identifier
        except Exception:
            pass
        return f"deck/{serial}/{input_type}/{identifier}/{self.state}"

    def event_metadata(self) -> tuple[str, dict[str, Any]]:
        meta = {"deck": "", "page": "", "input": "key", "identifier": "", "state": int(self.state or 0)}
        try:
            meta["deck"] = self.deck_controller.safe_serial_number()
        except Exception:
            pass
        try:
            meta["page"] = self.page.get_name()
        except Exception:
            pass
        try:
            mapping = {
                "keys": "key", "dials": "dial", "touch_keys": "touch_key",
                "touchscreens": "touch", "screens": "screen",
            }
            meta["input"] = mapping.get(self.input_ident.input_type, self.input_ident.input_type)
            meta["identifier"] = self.input_ident.json_identifier
        except Exception:
            pass
        return self.channel(), meta

    def extra(self) -> dict[str, Any]:
        raw = self._settings().get("extra", "{}")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            if not self._extra_warning_emitted:
                self._extra_warning_emitted = True
                log.warning(f"WebSocket Client: extra JSON for {self.channel()!r} is invalid; sending {{}}")
            return {}
        if not isinstance(value, dict):
            if not self._extra_warning_emitted:
                self._extra_warning_emitted = True
                log.warning(f"WebSocket Client: extra JSON for {self.channel()!r} must be an object; sending {{}}")
            return {}
        return value

    # ---------------------------------------------------------------- events
    def _publish(self, event_name: str) -> None:
        if self.plugin_base is None:
            return
        try:
            self.plugin_base.publish_event(self, event_name, self.extra())
        except Exception as error:
            log.warning(f"WebSocket Client: could not publish {event_name}: {error}")

    def _on_down(self, data=None) -> None:
        self._publish("down")

    def _on_up(self, data=None) -> None:
        self._publish("up")

    def _on_short_up(self, data=None) -> None:
        self._publish("short_up")

    def _on_hold_start(self, data=None) -> None:
        self._publish("hold_start")

    def _on_hold_stop(self, data=None) -> None:
        self._publish("hold_stop")

    # -------------------------------------------------------------- rendering
    def apply_state(self, state: dict[str, Any] | None, revision: int = 0) -> None:
        if not self._ready or not self.on_ready_called:
            return
        channel = self.channel()
        if channel != self._registered_channel:
            self._reregister(channel)
        registry = getattr(self.plugin_base, "registry", None)
        if state is not None and revision and registry is not None and not registry.is_current(channel, revision):
            return
        self._last_state = state
        if state is None:
            self._render_defaults()
            return

        self._apply_text(state.get("text") or {})
        background = state.get("background")
        self._safe_call(self.set_background_color, background or TRANSPARENT)

        if state.get("error"):
            self._safe_call(self.show_error)
        else:
            self._safe_call(self.hide_error)

        self._apply_overlay(state.get("overlay"), revision)
        self._apply_image(state.get("image"), revision)

    def _apply_text(self, text_state: dict[str, Any]) -> None:
        setters = {
            "top": self.set_top_label,
            "center": self.set_center_label,
            "bottom": self.set_bottom_label,
        }
        for position, setter in setters.items():
            entry = text_state.get(position) if isinstance(text_state, dict) else None
            if not isinstance(entry, dict):
                entry = {"text": ""}
            text = entry.get("text", "")
            color = entry.get("color") or WHITE
            self._safe_call(setter, str(text), color)

    def _render_defaults(self) -> None:
        settings = self._settings()
        self._safe_call(self.set_top_label, "", WHITE)
        self._safe_call(self.set_center_label, str(settings.get("default_text", "")), WHITE)
        self._safe_call(self.set_bottom_label, "", WHITE)
        self._safe_call(self.set_background_color, TRANSPARENT)
        self._safe_call(self.hide_error)
        self._server_overlay_active = False
        self._overlay_seq += 1
        if self._overlay_owner == "server":
            self._safe_call(self.hide_overlay)
            self._overlay_owner = None
        self._show_default_icon()

    def _show_default_icon(self) -> None:
        self._image_seq += 1
        configured = self._settings().get("default_icon", "").strip()
        path = configured
        if not path:
            try:
                path = os.path.join(self.plugin_base.PATH, "assets", "icon.png")
            except Exception:
                path = ""
        self._base_layout = {}
        self._base_path = ""
        if not path or not os.path.isfile(path):
            self._base_image = self._blank_image()
            self._render_media()
            return

        image = self._load_default_icon(path)
        if image is None:
            # Videos and other formats PIL cannot decode keep StreamController's
            # own media_path handling. They cannot carry the composited dot.
            self._base_image = None
            self._base_path = path
        else:
            self._base_image = image
        self._render_media()

    def _load_default_icon(self, path: str) -> Image.Image | None:
        try:
            stat = os.stat(path)
            cache_key = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            cache_key = path
        cached = self._default_icon_cache
        if cached is not None and cached[0] == cache_key:
            return cached[1].copy()
        try:
            with Image.open(path) as opened:
                opened.load()
                image = opened.convert("RGBA").copy()
            self._default_icon_cache = (cache_key, image.copy())
            return image
        except Exception:
            pass
        try:
            thumb = gl.media_manager.generate_svg_thumbnail(path)
            if isinstance(thumb, Image.Image):
                image = thumb.convert("RGBA").copy()
                self._default_icon_cache = (cache_key, image.copy())
                return image
        except Exception as error:
            log.debug(f"WebSocket Client: could not render default icon {path!r}: {error}")
        return None

    # ------------------------------------------------------- image composition
    def _key_image_size(self) -> tuple[int, int]:
        try:
            size = self.deck_controller.get_key_image_size()
            width, height = int(size[0]), int(size[1])
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass
        try:
            state = self.get_state()
            size = state.controller_input.deck_controller.get_key_image_size()
            width, height = int(size[0]), int(size[1])
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass
        return 144, 144

    DOT_COLORS = {
        "waiting": (245, 166, 35, 255),
        "offline": (225, 40, 40, 255),
    }

    def _dot_layer(self, size: tuple[int, int], state: str) -> Image.Image:
        cache_key = (size, state)
        cached = self._dot_cache.get(cache_key)
        if cached is not None:
            return cached
        width, height = size
        shortest = max(8, min(width, height))
        diameter = max(6, round(shortest * 0.18))
        margin = max(2, round(shortest * 0.04))
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        try:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(layer)
            draw.ellipse(
                (width - margin - diameter, height - margin - diameter,
                 width - margin, height - margin),
                fill=self.DOT_COLORS.get(state, self.DOT_COLORS["offline"]),
                outline=(255, 255, 255, 220),
                width=max(1, round(diameter * 0.12)),
            )
        except Exception:
            pass
        self._dot_cache[cache_key] = layer
        return layer

    def _composite_dot(self, base: Image.Image, state: str) -> Image.Image:
        canvas = base.convert("RGBA").copy()
        canvas.alpha_composite(self._dot_layer(canvas.size, state))
        return canvas

    def _dot_state(self) -> str | None:
        if not bool(self._settings().get("show_offline_dot", True)):
            return None
        if self._server_overlay_active:
            return None
        if self._connection_state == "waiting":
            return "waiting"
        if self._connection_state != "connected":
            return "offline"
        return None

    def _layout_base(self, base: Image.Image) -> Image.Image:
        """Apply the server image layout and return a key-sized canvas.

        The dot is drawn afterwards, at key-image scale, so a server image with
        ``size`` < 1 does not shrink the dot.
        """
        width, height = self._key_image_size()
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        layout = self._base_layout
        try:
            size = float(layout.get("size") if layout.get("size") is not None else 1.0)
        except (TypeError, ValueError):
            size = 1.0
        size = max(0.0, size)
        target = (int(width * size), int(height * size))
        if target[0] <= 0 or target[1] <= 0:
            return canvas
        source = base.convert("RGBA")
        try:
            resized = ImageOps.cover(source, target, Image.Resampling.HAMMING)
        except Exception:
            resized = source.resize(target, Image.Resampling.HAMMING)
        try:
            halign = float(layout.get("halign") or 0)
        except (TypeError, ValueError):
            halign = 0.0
        try:
            valign = float(layout.get("valign") or 0)
        except (TypeError, ValueError):
            valign = 0.0
        halign = max(-1.0, min(1.0, halign))
        valign = max(-1.0, min(1.0, valign))
        left = int((width - resized.width) * (halign + 1) / 2)
        top = int((height - resized.height) * (valign + 1) / 2)
        try:
            if resized.has_transparency_data:
                canvas.paste(resized, (left, top), resized)
            else:
                canvas.paste(resized, (left, top))
        except Exception:
            pass
        return canvas

    def _render_media(self) -> None:
        if not self._ready or not self.on_ready_called:
            return
        if self._base_image is None and self._base_path:
            # Video/unsupported default icons keep StreamController's own
            # media_path handling; they cannot carry the composited dot.
            self._safe_call(self.set_media, media_path=self._base_path, **self._base_layout)
            return
        base = self._base_image if self._base_image is not None else self._blank_image()
        image = self._layout_base(base)
        state = self._dot_state()
        if state is not None:
            image = self._composite_dot(image, state)
        self._safe_call(self.set_media, image=image)

    def _apply_image(self, spec: Any, revision: int) -> None:
        self._image_seq += 1
        sequence = self._image_seq
        if spec is None:
            self._show_default_icon()
            return
        if not isinstance(spec, dict):
            return
        self._load_image_async(spec, sequence, revision, overlay=False)

    def _apply_overlay(self, spec: Any, revision: int) -> None:
        self._overlay_seq += 1
        sequence = self._overlay_seq
        if spec is None:
            self._server_overlay_active = False
            if self._overlay_owner == "server":
                self._safe_call(self.hide_overlay)
                self._overlay_owner = None
            return
        if not isinstance(spec, dict):
            return

        self._server_overlay_active = True
        if "color" in spec:
            color = parse_color(spec.get("color")) or [0, 0, 0, 160]
            self._safe_call(self.show_overlay, self._solid_overlay(color))
            self._overlay_owner = "server"
            return
        self._load_image_async(spec, sequence, revision, overlay=True)

    def _load_image_async(self, spec: dict[str, Any], sequence: int, revision: int, overlay: bool) -> None:
        channel = self.channel()
        def worker():
            image = self._load_image(spec)
            GLib.idle_add(self._finish_image, image, sequence, revision, overlay, channel)

        threading.Thread(target=worker, name="WebSocketClientImage", daemon=True).start()

    def _finish_image(self, image: Image.Image | None, sequence: int, revision: int,
                      overlay: bool, channel: str) -> bool:
        expected_sequence = self._overlay_seq if overlay else self._image_seq
        if sequence != expected_sequence or channel != self.channel() or not self._ready:
            return False
        registry = getattr(self.plugin_base, "registry", None)
        if revision and registry is not None and not registry.is_current(channel, revision):
            return False
        if overlay:
            if image is None:
                self._server_overlay_active = False
                if self._overlay_owner == "server":
                    self._safe_call(self.hide_overlay)
                    self._overlay_owner = None
                return False
            self._safe_call(self.show_overlay, image)
            self._overlay_owner = "server"
            return False

        if image is None:
            return False
        state = self._last_state or {}
        image_spec = state.get("image") if isinstance(state, dict) else None
        size = image_spec.get("size") if isinstance(image_spec, dict) else None
        valign = image_spec.get("valign") if isinstance(image_spec, dict) else None
        halign = image_spec.get("halign") if isinstance(image_spec, dict) else None
        self._base_image = image
        self._base_path = ""
        self._base_layout = {"size": size, "valign": valign, "halign": halign}
        self._render_media()
        return False

    def _load_image(self, spec: dict[str, Any]) -> Image.Image | None:
        url = spec.get("url")
        path = spec.get("path")
        key = None
        if isinstance(url, str) and url:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                log.warning(f"WebSocket Client: refused non-http(s) image URL {url!r}")
                return None
            key = url
        elif isinstance(path, str) and path:
            if not getattr(self.plugin_base, "allow_local_image_paths", lambda: False)():
                log.warning("WebSocket Client: refused local image path (enable 'Allow local image paths')")
                return None
            if not os.path.isabs(path):
                log.warning(f"WebSocket Client: refused relative image path {path!r}")
                return None
            key = path
        else:
            return None

        with self._image_cache_lock:
            cached = self._image_cache.get(key)
            if cached is not None:
                self._image_cache.move_to_end(key)
                return cached.copy()

        try:
            data = self._read_image_bytes(url, path)
            if data is None:
                return None
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                image = opened.convert("RGBA").copy()
            with self._image_cache_lock:
                self._image_cache[key] = image.copy()
                while len(self._image_cache) > IMAGE_CACHE_SIZE:
                    self._image_cache.popitem(last=False)
            return image
        except Exception as error:
            log.warning(f"WebSocket Client: could not load image {key!r}: {error}")
            return None

    def _read_image_bytes(self, url: Any, path: Any) -> bytes | None:
        if isinstance(url, str) and url:
            response = requests.get(url, timeout=IMAGE_TIMEOUT, stream=True)
            try:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > MAX_IMAGE_BYTES:
                            log.warning("WebSocket Client: image larger than 5 MiB, ignored")
                            return None
                    except ValueError:
                        pass
                chunks = []
                total = 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        log.warning("WebSocket Client: image larger than 5 MiB, ignored")
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()

        if isinstance(path, str) and path:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                log.warning("WebSocket Client: image larger than 5 MiB, ignored")
                return None
            with open(path, "rb") as handle:
                return handle.read()
        return None

    def _safe_call(self, callback, *args, **kwargs):
        if not self._ready or not self.on_ready_called:
            return None
        try:
            return callback(*args, **kwargs)
        except Exception as error:
            log.debug(f"WebSocket Client: render call failed: {error}")
            return None

    @staticmethod
    def _blank_image() -> Image.Image:
        return Image.new("RGBA", (144, 144), (0, 0, 0, 0))

    @staticmethod
    def _solid_overlay(color: list[int]) -> Image.Image:
        return Image.new("RGBA", (144, 144), tuple(color))

    def set_connection_state(self, state: str) -> None:
        if state not in ("connected", "waiting", "offline"):
            state = "offline"
        # Status callbacks can originate on the WebSocket or timer thread.
        if threading.current_thread() is not threading.main_thread():
            GLib.idle_add(self._apply_connection_state, state)
            return
        self._apply_connection_state(state)

    def _apply_connection_state(self, state: str) -> bool:
        self._connection_state = state
        self._render_media()
        return False

    def set_connected(self, connected: bool) -> None:
        """Backward-compatible shorthand used by older callers/tests."""
        self.set_connection_state("connected" if connected else "offline")

    # ------------------------------------------------------------- config UI
    def get_config_rows(self) -> list:
        settings = self._settings()
        rows = []

        channel_row = Adw.EntryRow(title="Channel")
        channel_row.set_text(settings["channel"])
        try:
            channel_row.set_placeholder_text(self.generated_channel())
        except Exception:
            pass
        channel_row.connect("changed", self._on_channel_changed)
        rows.append(channel_row)

        # Only useful when the channel field is empty, so don't clutter the
        # config with it once an explicit channel is set.
        generated_row = Adw.ActionRow(
            title="Generated channel",
            subtitle=self.generated_channel(),
        )
        copy_button = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_button.set_tooltip_text("Copy generated channel")
        copy_button.set_valign(Gtk.Align.CENTER)
        copy_button.connect("clicked", lambda _button: self._copy_to_clipboard(self.generated_channel()))
        generated_row.add_suffix(copy_button)
        generated_row.set_visible(not settings["channel"].strip())
        channel_row.connect(
            "changed",
            lambda row: generated_row.set_visible(not row.get_text().strip()),
        )
        rows.append(generated_row)

        text_row = Adw.EntryRow(title="Default text")
        text_row.set_text(str(settings["default_text"]))
        text_row.connect("changed", self._on_default_text_changed)
        rows.append(text_row)

        icon_row = Adw.ActionRow(title="Default icon", subtitle=settings["default_icon"] or "Built-in icon")
        choose_button = Gtk.Button(label="Choose")
        choose_button.set_valign(Gtk.Align.CENTER)
        choose_button.connect("clicked", lambda _button: self._choose_default_icon(icon_row))
        clear_button = Gtk.Button(label="Clear")
        clear_button.set_valign(Gtk.Align.CENTER)
        clear_button.connect("clicked", lambda _button: self._set_default_icon(icon_row, ""))
        icon_row.add_suffix(choose_button)
        icon_row.add_suffix(clear_button)
        rows.append(icon_row)

        extra_row = Adw.EntryRow(title="Extra JSON")
        extra_row.set_text(str(settings["extra"]))
        try:
            extra_row.set_placeholder_text('{"room": "office"}')
        except Exception:
            pass
        extra_row.connect("changed", self._on_extra_changed)
        if not self._extra_is_valid(settings["extra"]):
            extra_row.add_css_class("error")
        rows.append(extra_row)

        offline_row = Adw.SwitchRow(title="Show offline dot")
        offline_row.set_active(bool(settings["show_offline_dot"]))
        offline_row.connect("notify::active", self._on_offline_toggled)
        rows.append(offline_row)
        return rows

    def _on_channel_changed(self, row) -> None:
        old_channel = self._registered_channel
        new_channel = row.get_text().strip()
        self._save(channel=new_channel)
        channel = self.channel()
        try:
            row.set_placeholder_text(self.generated_channel())
        except Exception:
            pass
        if old_channel and old_channel != channel:
            try:
                self.plugin_base.registry.unregister(old_channel, self)
            except Exception:
                pass
        if self._ready:
            self._registered_channel = channel
            cached = self.plugin_base.registry.register(channel, self, self.event_metadata()[1])
            if cached is None:
                self._render_defaults()
            else:
                self.apply_state(cached, self.plugin_base.registry.revision(channel))

    def _reregister(self, channel: str) -> None:
        try:
            if self._registered_channel and self._registered_channel != channel:
                self.plugin_base.registry.unregister(self._registered_channel, self)
            self._registered_channel = channel
            self.plugin_base.registry.register(channel, self, self.event_metadata()[1])
        except Exception as error:
            log.debug(f"WebSocket Client: re-register failed: {error}")

    def _on_default_text_changed(self, row) -> None:
        self._save(default_text=row.get_text())
        if self._last_state is None and self._ready:
            self._safe_call(self.set_center_label, row.get_text(), WHITE)

    def _on_extra_changed(self, row) -> None:
        text = row.get_text()
        self._save(extra=text)
        if self._extra_is_valid(text):
            row.remove_css_class("error")
        else:
            row.add_css_class("error")
            if not self._extra_warning_emitted:
                self._extra_warning_emitted = True
                log.warning("WebSocket Client: extra JSON is invalid; events will send {}")

    @staticmethod
    def _extra_is_valid(text: str) -> bool:
        try:
            return isinstance(json.loads(text), dict)
        except (TypeError, ValueError):
            return False

    def _on_offline_toggled(self, row, _pspec) -> None:
        self._save(show_offline_dot=bool(row.get_active()))
        if self._ready:
            self._render_media()

    def _choose_default_icon(self, row) -> None:
        current = self._settings().get("default_icon", "")
        try:
            gl.app.let_user_select_asset(default_path=current, callback_func=lambda path: self._set_default_icon(row, path))
        except Exception as error:
            log.warning(f"WebSocket Client: asset picker unavailable: {error}")

    def _set_default_icon(self, row, path: str | None) -> None:
        path = path or ""
        self._save(default_icon=path)
        try:
            row.set_subtitle(path or "Built-in icon")
        except Exception:
            pass
        if self._ready and (self._last_state is None or (self._last_state or {}).get("image") is None):
            self._show_default_icon()

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                display.get_clipboard().set(text)
        except Exception as error:
            log.debug(f"WebSocket Client: clipboard unavailable: {error}")
