# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""StreamController plugin entry point for the generic WebSocket client."""

from __future__ import annotations

import atexit
import copy
import html
import os
import socket
import threading
import time
from typing import Any

from loguru import logger as log

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.PluginManager.PluginBase import PluginBase

try:
    from .protocol import build_channels, build_event, build_hello
    from .registry import Registry
    from .state_cache import StateCache
    from .ws_client import WEBSOCKET_IMPORT_ERROR, WebSocketClient
    from .actions.WebSocketKey import WebSocketKey
except ImportError:  # loaded as a top-level module in some tooling
    from protocol import build_channels, build_event, build_hello
    from registry import Registry
    from state_cache import StateCache
    from ws_client import WEBSOCKET_IMPORT_ERROR, WebSocketClient
    from actions.WebSocketKey import WebSocketKey

PLUGIN_VERSION = "1.0.0"
PLUGIN_ID = "com_tahvodev_WebSocketClient"
GITHUB_REPO = "https://github.com/TahvoDev/streamcontroller-websocket-client"

ACTION_SETTINGS_SCHEMA = {
    "channel": {
        "type": "string",
        "required": True,
        "description": "Stable name for this key. Leave empty to use the generated deck/position name.",
        "example": "media.mute",
    },
    "default_text": {
        "type": "string",
        "default": "",
        "description": "Text shown before the server sends anything.",
        "example": "Mute",
    },
    "default_icon": {
        "type": "string",
        "default": "",
        "description": "Absolute path to the default icon image.",
        "example": "/home/me/icons/mute.png",
    },
    "extra": {
        "type": "string",
        "default": "{}",
        "description": "Extra JSON merged into every event from this key.",
        "example": '{"room": "office"}',
    },
    "show_offline_dot": {
        "type": "boolean",
        "default": True,
        "description": "Draw a small red dot while the WebSocket is disconnected.",
    },
}


def _default_settings() -> dict[str, Any]:
    return {
        "profile-version": 1,
        "connections": [{
            "id": "default",
            "name": "Default",
            "url": "",
            "token": "",
            "headers": {},
            "tls_skip_verify": False,
        }],
        "default_connection": "default",
        "instance": socket.gethostname() or "streamcontroller",
        "auto_connect": True,
        "require_server_response": True,
        "restore_last_display": True,
        "log_traffic": False,
        "allow_local_image_paths": False,
        "first-setup": False,
    }


class WebSocketClientPlugin(PluginBase):
    def __init__(self):
        super().__init__()
        self.register(
            plugin_name="WebSocket Client",
            github_repo=GITHUB_REPO,
            plugin_version=PLUGIN_VERSION,
            app_version="1.5.0",
        )
        self.has_plugin_settings = True

        try:
            from locales.LocaleManager import LocaleManager
            self.locale_manager = LocaleManager(os.path.join(self.PATH, "locales", "locales.csv"))
            self.locale_manager.set_to_os_default()
        except Exception as error:
            log.debug(f"WebSocket Client: locale manager fallback: {error}")

        self.settings: dict[str, Any] = self._load_settings()
        self._status = "disconnected"
        self._status_detail = ""
        self._last_error_text = ""
        self._test_detail = ""
        self._channels_pending = False
        self._shutdown_done = False
        self._ui: dict[str, Any] = {}

        cache_path = os.path.join(
            gl.DATA_PATH, "settings", "plugins", self.get_plugin_id(), "state_cache.json",
        )
        self.state_cache = StateCache(
            cache_path,
            enabled=bool(self.settings.get("restore_last_display", True)),
            debounce=2.0,
            max_channels=500,
        )
        self.registry = Registry(
            instance_id=str(self.settings.get("instance", "")),
            dispatch=self._dispatch_to_action,
            cache=self.state_cache,
            max_channels=500,
            on_channels_changed=self._on_channels_changed,
        )
        self.registry.load_cache()
        self.ws_client: WebSocketClient | None = None

        icon_image = Gtk.Image.new_from_file(os.path.join(self.PATH, "assets", "icon.png"))
        self.add_action_holder(ActionHolder(
            plugin_base=self,
            action_core=WebSocketKey,
            action_id_suffix="WebSocketKey",
            action_name="WebSocket Key",
            action_support={Input.Key: ActionInputSupport.SUPPORTED},
            description="Shows text, images and colours pushed by a WebSocket server and publishes this key's presses to it.",
            requirements="A WebSocket server speaking SC/WS v1, reachable from this computer.",
            settings_schema=ACTION_SETTINGS_SCHEMA,
            icon=icon_image,
        ))

        atexit.register(self.shutdown)
        if self.settings.get("auto_connect") and self._connection_settings().get("url"):
            # start_connection() returns bool; GLib would treat a True return as
            # "run this idle source again" and reconnect in a tight loop.
            GLib.idle_add(self._start_connection_idle)

    # --------------------------------------------------------------- settings
    def _load_settings(self) -> dict[str, Any]:
        defaults = _default_settings()
        saved = self.get_settings() or {}
        result = copy.deepcopy(defaults)
        for key in defaults:
            if key in saved:
                result[key] = copy.deepcopy(saved[key])
        result["instance"] = str(result.get("instance") or defaults["instance"])

        connections = result.get("connections")
        if not isinstance(connections, list) or not connections:
            connections = copy.deepcopy(defaults["connections"])
        cleaned = []
        for index, connection in enumerate(connections):
            if not isinstance(connection, dict):
                continue
            base = copy.deepcopy(defaults["connections"][0])
            base.update({key: connection[key] for key in base if key in connection})
            base["id"] = str(base.get("id") or f"connection-{index}")
            base["headers"] = base.get("headers") if isinstance(base.get("headers"), dict) else {}
            cleaned.append(base)
        result["connections"] = cleaned or copy.deepcopy(defaults["connections"])
        if not any(item["id"] == result.get("default_connection") for item in result["connections"]):
            result["default_connection"] = result["connections"][0]["id"]
        return result

    def _connection_settings(self) -> dict[str, Any]:
        default_id = self.settings.get("default_connection")
        for connection in self.settings.get("connections", []):
            if connection.get("id") == default_id:
                return connection
        connections = self.settings.get("connections") or [copy.deepcopy(_default_settings()["connections"][0])]
        return connections[0]

    def _save_settings(self) -> None:
        self.settings["profile-version"] = 1
        self.settings["first-setup"] = False
        self.set_settings(self.settings)
        self.first_setup = False

    # ------------------------------------------------------------- settings UI
    def get_settings_area(self) -> Adw.PreferencesGroup:
        connection = self._connection_settings()
        group = Adw.PreferencesGroup(
            title=self._t("settings-title", "WebSocket Client"),
            description=self._t(
                "settings-description",
                "One connection per plugin instance. Changes apply when you press Apply & Reconnect.",
            ),
        )

        self._ui = {}
        if WEBSOCKET_IMPORT_ERROR:
            error_row = Adw.ActionRow(
                title=self._t("settings-websocket-missing", "websocket-client is missing"),
                subtitle=WEBSOCKET_IMPORT_ERROR,
            )
            error_row.add_css_class("error")
            group.add(error_row)

        url_row = Adw.EntryRow(title=self._t("settings-url", "URL"))
        url_row.set_text(str(connection.get("url", "")))
        url_row.set_show_apply_button(False)
        try:
            url_row.set_placeholder_text("wss://example.com/sc")
        except Exception:
            pass
        group.add(url_row)

        instance_row = Adw.EntryRow(title=self._t("settings-instance", "Instance name"))
        instance_row.set_text(str(self.settings.get("instance", "")))
        instance_row.set_show_apply_button(False)
        group.add(instance_row)

        try:
            token_row = Adw.PasswordEntryRow(title=self._t("settings-token", "Bearer token"))
        except Exception:
            token_row = Adw.EntryRow(title=self._t("settings-token", "Bearer token"))
        token_row.set_text(str(connection.get("token", "")))
        group.add(token_row)

        headers_expander = Adw.ExpanderRow(title=self._t("settings-headers", "Extra headers"))
        headers_expander.set_subtitle(self._t("settings-headers-hint", "One Name: value header per row"))
        group.add(headers_expander)
        header_rows: list[tuple[Any, Any, Any]] = []
        for name, value in (connection.get("headers") or {}).items():
            self._add_header_row(headers_expander, header_rows, str(name), str(value))
        if not header_rows:
            self._add_header_row(headers_expander, header_rows, "", "")
        add_header_button = Gtk.Button(label=self._t("settings-header-add", "Add"))
        add_header_button.set_valign(Gtk.Align.CENTER)
        add_header_button.connect(
            "clicked",
            lambda _button: self._add_header_row(headers_expander, header_rows, "", ""),
        )
        headers_expander.add_suffix(add_header_button)

        auto_connect_row = Adw.SwitchRow(title=self._t("settings-auto-connect", "Connect on start"))
        auto_connect_row.set_active(bool(self.settings.get("auto_connect", True)))
        group.add(auto_connect_row)

        restore_row = Adw.SwitchRow(title=self._t("settings-restore", "Restore last display"))
        restore_row.set_subtitle(self._t(
            "settings-restore-hint", "Keep text, colours and image URLs across page switches and restarts.",
        ))
        restore_row.set_active(bool(self.settings.get("restore_last_display", True)))
        group.add(restore_row)

        log_row = Adw.SwitchRow(title=self._t("settings-log-traffic", "Log traffic"))
        log_row.set_subtitle(self._t("settings-log-traffic-hint", "Tokens and Authorization values are redacted."))
        log_row.set_active(bool(self.settings.get("log_traffic", False)))
        group.add(log_row)

        skip_tls_row = Adw.SwitchRow(title=self._t("settings-skip-tls", "Skip TLS verification"))
        skip_tls_row.set_subtitle(self._t(
            "settings-skip-tls-hint", "Only for self-signed development servers. Prefer a trusted certificate.",
        ))
        skip_tls_row.set_active(bool(connection.get("tls_skip_verify", False)))
        group.add(skip_tls_row)

        local_paths_row = Adw.SwitchRow(title=self._t("settings-local-images", "Allow local image paths"))
        local_paths_row.set_subtitle(self._t(
            "settings-local-images-hint", "Servers may reference files on this computer. Off by default.",
        ))
        local_paths_row.set_active(bool(self.settings.get("allow_local_image_paths", False)))
        group.add(local_paths_row)

        require_response_row = Adw.SwitchRow(title=self._t(
            "settings-require-response", "Require server response",
        ))
        require_response_row.set_subtitle(self._t(
            "settings-require-response-hint",
            "Show Waiting for server until the server sends hello_ack or any valid SC/WS frame (5 s timeout).",
        ))
        require_response_row.set_active(bool(self.settings.get("require_server_response", True)))
        group.add(require_response_row)

        status_row = Adw.ActionRow(title=self._t("settings-status", "Status"))
        last_error_row = Adw.ActionRow(title=self._t("settings-last-error", "Last error"))
        channels_row = Adw.ActionRow(title=self._t("settings-channels", "Channels"))
        dropped_row = Adw.ActionRow(title=self._t("settings-dropped", "Dropped sends"))
        test_row = Adw.ActionRow(title=self._t("settings-test-result", "Test result"))
        for row in (status_row, last_error_row, channels_row, dropped_row, test_row):
            group.add(row)

        actions_row = Adw.ActionRow(title=self._t("settings-actions", "Connection actions"))
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_valign(Gtk.Align.CENTER)
        test_button = Gtk.Button(label=self._t("settings-test", "Test connection"))
        apply_button = Gtk.Button(label=self._t("settings-apply", "Apply & Reconnect"))
        apply_button.add_css_class("suggested-action")
        button_box.append(test_button)
        button_box.append(apply_button)
        actions_row.add_suffix(button_box)
        group.add(actions_row)

        self._ui = {
            "group": group,
            "url": url_row,
            "instance": instance_row,
            "token": token_row,
            "headers_expander": headers_expander,
            "header_rows": header_rows,
            "auto_connect": auto_connect_row,
            "restore": restore_row,
            "log_traffic": log_row,
            "skip_tls": skip_tls_row,
            "local_paths": local_paths_row,
            "require_response": require_response_row,
            "status": status_row,
            "last_error": last_error_row,
            "channels": channels_row,
            "dropped": dropped_row,
            "test": test_row,
            "test_button": test_button,
            "apply_button": apply_button,
        }
        test_button.connect("clicked", self._test_connection_clicked)
        apply_button.connect("clicked", self._apply_clicked)
        self._refresh_status_rows()
        return group

    def _t(self, key: str, fallback: str) -> str:
        try:
            text = self.locale_manager.get(key, fallback) or fallback
        except Exception:
            text = fallback
        # LocaleManager.get() HTML-escapes its result for markup-capable
        # widgets, but every widget here takes plain text, so undo it.
        return html.unescape(text)

    def _add_header_row(self, expander, rows: list[tuple[Any, Any, Any]], name: str, value: str) -> None:
        row = Adw.ActionRow(title=self._t("settings-header", "Header"))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_valign(Gtk.Align.CENTER)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text(self._t("settings-header-name", "Name"))
        name_entry.set_text(name)
        name_entry.set_width_chars(12)
        value_entry = Gtk.Entry()
        value_entry.set_placeholder_text(self._t("settings-header-value", "Value"))
        value_entry.set_text(value)
        value_entry.set_width_chars(18)
        remove_button = Gtk.Button(icon_name="user-trash-symbolic")
        remove_button.set_tooltip_text(self._t("settings-header-remove", "Remove header"))
        box.append(name_entry)
        box.append(value_entry)
        box.append(remove_button)
        row.add_suffix(box)
        expander.add_row(row)
        rows.append((row, name_entry, value_entry))

        def remove(_button):
            if row in rows:
                rows.remove((row, name_entry, value_entry))
            expander.remove(row)

        remove_button.connect("clicked", remove)

    def _collect_settings_from_ui(self) -> None:
        if not self._ui:
            return
        connection = self._connection_settings()
        connection["url"] = self._ui["url"].get_text().strip()
        connection["token"] = self._ui["token"].get_text().strip()
        connection["tls_skip_verify"] = bool(self._ui["skip_tls"].get_active())
        headers: dict[str, str] = {}
        for _row, name_entry, value_entry in list(self._ui["header_rows"]):
            name = name_entry.get_text().strip()
            if name:
                headers[name] = value_entry.get_text()
        connection["headers"] = headers
        self.settings["instance"] = self._ui["instance"].get_text().strip() or socket.gethostname()
        self.settings["auto_connect"] = bool(self._ui["auto_connect"].get_active())
        self.settings["restore_last_display"] = bool(self._ui["restore"].get_active())
        self.settings["log_traffic"] = bool(self._ui["log_traffic"].get_active())
        self.settings["allow_local_image_paths"] = bool(self._ui["local_paths"].get_active())
        self.settings["require_server_response"] = bool(self._ui["require_response"].get_active())
        self.registry.instance_id = self.settings["instance"]
        self.state_cache.enabled = self.settings["restore_last_display"]
        self._save_settings()

    def _apply_clicked(self, _button=None) -> None:
        self._collect_settings_from_ui()
        self.stop_connection()
        self.start_connection()

    def _test_connection_clicked(self, _button=None) -> None:
        self._collect_settings_from_ui()
        connection = self._connection_settings()
        if self._ui:
            self._ui["test"].set_subtitle(self._t("settings-testing", "Testing…"))
        url = connection.get("url", "")
        token = connection.get("token", "")
        headers = connection.get("headers", {})
        skip_tls = bool(connection.get("tls_skip_verify", False))

        require_response = bool(self.settings.get("require_server_response", True))

        def worker():
            ok, detail = WebSocketClient.test_connection(
                url, headers=headers, token=token, tls_skip_verify=skip_tls,
                hello=self._build_hello(), require_response=require_response, timeout=5.0,
            )
            GLib.idle_add(self._show_test_result, ok, detail)

        threading.Thread(target=worker, name="WebSocketClientTest", daemon=True).start()

    def _show_test_result(self, ok: bool, detail: str) -> bool:
        text = ("✓ " if ok else "✗ ") + (detail or "")
        self._test_detail = text
        if self._ui:
            self._ui["test"].set_subtitle(text)
        log.info(f"WebSocket Client: connection test: {text}")
        return False

    # ------------------------------------------------------------- connection
    def _start_connection_idle(self) -> bool:
        self.start_connection()
        return False

    def start_connection(self) -> bool:
        if WEBSOCKET_IMPORT_ERROR:
            log.error(f"WebSocket Client: websocket-client is not available: {WEBSOCKET_IMPORT_ERROR}")
            self._set_status("error", f"websocket-client is not available: {WEBSOCKET_IMPORT_ERROR}")
            return False
        connection = self._connection_settings()
        url = str(connection.get("url", "")).strip()
        if not url:
            self._set_status("disconnected", "No WebSocket URL configured")
            return False
        self.stop_connection()
        self.ws_client = WebSocketClient(
            url=url,
            instance=str(self.settings.get("instance", "")),
            headers=connection.get("headers", {}),
            token=connection.get("token", ""),
            tls_skip_verify=bool(connection.get("tls_skip_verify", False)),
            hello_provider=self._build_hello,
            on_frame=self._on_ws_frame,
            on_status=self._on_ws_status,
            log_traffic=bool(self.settings.get("log_traffic", False)),
            verify_timeout=5.0 if self.settings.get("require_server_response", True) else None,
            reconnect_interval=3.0,
        )
        return self.ws_client.start()

    def stop_connection(self) -> None:
        client = self.ws_client
        self.ws_client = None
        if client is not None:
            client.shutdown()

    def is_connected(self) -> bool:
        # Socket-open alone is not enough: the server must have proven it
        # speaks SC/WS (when response verification is enabled).
        return bool(self.ws_client and self.ws_client.healthy)

    def connection_state(self) -> str:
        if self._status == "connected":
            return "connected"
        if self._status == "waiting":
            return "waiting"
        return "offline"

    def _on_ws_status(self, status: str, detail: str) -> None:
        # Called from the WebSocket thread: never touch GTK directly.
        GLib.idle_add(self._set_status, status, detail)

    def _set_status(self, status: str, detail: str = "") -> bool:
        self._status = status
        self._status_detail = detail or ""
        if status in ("error", "reconnecting") and detail:
            self._last_error_text = detail
        if status == "connected":
            # Don't keep showing a stale failure next to a live connection.
            self._last_error_text = ""
            action_state = "connected"
        elif status == "waiting":
            action_state = "waiting"
        elif status in ("disconnected", "error", "reconnecting"):
            action_state = "offline"
        else:
            action_state = "offline"
            log.debug(f"WebSocket Client: unknown status {status!r}")
        self._apply_connection_state_to_actions(action_state)
        self._refresh_status_rows()
        return False

    def _apply_connection_state_to_actions(self, state: str) -> None:
        for action in self.registry.all_actions():
            try:
                action.set_connection_state(state)
            except Exception as error:
                log.debug(f"WebSocket Client: could not update action connection state: {error}")

    def _refresh_status_rows(self) -> None:
        if not self._ui:
            return
        labels = {
            "connected": self._t("status-connected", "Connected"),
            "reconnecting": self._t("status-reconnecting", "Reconnecting"),
            "disconnected": self._t("status-offline", "Offline"),
            "error": self._t("status-offline", "Offline"),
            "waiting": self._t("status-waiting", "Waiting for server"),
        }
        status_text = labels.get(self._status, self._status)
        detail = self._status_detail
        if self._status == "waiting" and detail == "No response from server":
            detail = self._t("status-no-response", "No response from server")
        if detail and self._status != "connected":
            status_text = f"{status_text} — {detail}"
        self._ui["status"].set_subtitle(status_text)
        self._ui["last_error"].set_subtitle(self._last_error_text or "—")
        self._ui["channels"].set_subtitle(str(self.registry.channel_count()))
        dropped = self.ws_client.dropped_sends if self.ws_client else 0
        self._ui["dropped"].set_subtitle(str(dropped))
        if self._test_detail:
            self._ui["test"].set_subtitle(self._test_detail)

    # ------------------------------------------------------------------ frames
    def _on_ws_frame(self, frame: dict[str, Any]) -> None:
        try:
            reply = self.registry.handle_frame(frame)
        except Exception as error:
            log.warning(f"WebSocket Client: could not handle inbound frame: {error}")
            return
        if reply and self.ws_client is not None:
            self.ws_client.send_json(reply)

    def _dispatch_to_action(self, action, state, revision: int) -> None:
        # Called by the registry from any thread.
        GLib.idle_add(self._apply_action_state, action, state, revision)

    def _apply_action_state(self, action, state, revision: int) -> bool:
        try:
            action.apply_state(state, revision)
        except Exception as error:
            log.warning(f"WebSocket Client: action render failed: {error}")
        return False

    def _on_channels_changed(self) -> None:
        GLib.idle_add(self._queue_channels_update)

    def _queue_channels_update(self) -> bool:
        if self._channels_pending:
            return False
        self._channels_pending = True
        GLib.timeout_add(500, self._send_channels_now)
        return False

    def _send_channels_now(self) -> bool:
        self._channels_pending = False
        if self.ws_client is not None and self.ws_client.connected:
            self.ws_client.send_json(build_channels(self.registry.channel_entries()))
        self._refresh_status_rows()
        return False

    # ---------------------------------------------------------------- outgoing
    def publish_event(self, action, event_name: str, extra: dict[str, Any], timestamp: float | None = None) -> bool:
        channel, meta = action.event_metadata()
        if not channel:
            return False
        payload = build_event(
            channel=channel,
            instance=str(self.settings.get("instance", "")),
            deck=meta.get("deck", ""),
            page=meta.get("page", ""),
            input_type=meta.get("input", "key"),
            identifier=meta.get("identifier", ""),
            state=int(meta.get("state", 0) or 0),
            event=event_name,
            extra=extra,
            timestamp=time.time() if timestamp is None else timestamp,
        )
        if self.ws_client is None:
            return False
        return self.ws_client.send_json(payload)

    def _build_hello(self) -> dict[str, Any]:
        return build_hello(
            instance=str(self.settings.get("instance", "")),
            plugin_version=PLUGIN_VERSION,
            decks=self._build_decks(),
            pages=self._build_pages(),
            channels=self.registry.channel_entries(),
        )

    def _build_decks(self) -> list[dict[str, Any]]:
        decks: list[dict[str, Any]] = []
        try:
            controllers = list(getattr(getattr(gl, "deck_manager", None), "deck_controller", []) or [])
        except Exception:
            controllers = []
        for controller in controllers:
            try:
                deck = controller.deck
                rows, cols = deck.key_layout()
                decks.append({
                    "serial": controller.safe_serial_number(),
                    "name": str(getattr(deck, "model_name", None) or deck.deck_type()),
                    "rows": int(rows),
                    "cols": int(cols),
                    "dials": int(getattr(deck, "dial_count", lambda: 0)()),
                    "touch": bool(getattr(deck, "is_touch", lambda: False)()),
                    "screen": False,
                })
            except Exception as error:
                log.debug(f"WebSocket Client: could not describe deck: {error}")
        return decks

    def _build_pages(self) -> list[str]:
        try:
            return list(gl.page_manager.get_page_names())
        except Exception as error:
            log.debug(f"WebSocket Client: could not read page names: {error}")
            return []

    def allow_local_image_paths(self) -> bool:
        return bool(self.settings.get("allow_local_image_paths", False))

    # ---------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.registry.flush_all()
        except Exception as error:
            log.debug(f"WebSocket Client: registry flush on shutdown failed: {error}")
        self.stop_connection()
        try:
            self.state_cache.close()
        except Exception as error:
            log.debug(f"WebSocket Client: cache flush on shutdown failed: {error}")

    # Kept for compatibility with plugin managers that call this spelling.
    def on_unload(self) -> None:
        self.shutdown()
