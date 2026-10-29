# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Websocket-client wrapper with reconnect, status callbacks and safe sends.

This module never imports GTK or StreamController.  Status and frame callbacks
run on the WebSocket thread; the plugin is responsible for marshalling them to
the main thread with ``GLib.idle_add``.
"""

from __future__ import annotations

import json
import ssl
import threading
from typing import Any, Callable
from urllib.parse import urlparse

from loguru import logger as log

try:
    from .protocol import MAX_FRAME_BYTES, parse_frame
except ImportError:  # imported as a top-level module by the test suite
    from protocol import MAX_FRAME_BYTES, parse_frame

try:
    import websocket
except Exception as error:  # pragma: no cover - depends on installation
    websocket = None
    WEBSOCKET_IMPORT_ERROR = str(error)
else:
    WEBSOCKET_IMPORT_ERROR = None


SENSITIVE_KEYS = {"token", "authorization", "password", "secret", "cookie"}


def redact(value: Any) -> Any:
    """Recursively replace token-ish values for logs."""
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key).lower() in SENSITIVE_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "...<truncated>"
    return value


def validate_url(url: str) -> tuple[bool, str]:
    if not isinstance(url, str) or not url.strip():
        return False, "No WebSocket URL configured"
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("ws", "wss"):
        return False, "URL must start with ws:// or wss://"
    if not parsed.hostname:
        return False, "URL has no host"
    return True, ""


def build_header_list(headers: dict[str, str] | list[tuple[str, str]] | None,
                      token: str | None = None) -> list[str]:
    pairs: list[tuple[str, str]] = []
    if token:
        pairs.append(("Authorization", f"Bearer {token}"))
    if isinstance(headers, dict):
        pairs.extend((str(name), str(value)) for name, value in headers.items())
    elif isinstance(headers, (list, tuple)):
        for item in headers:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
    result = []
    for name, value in pairs:
        name = name.strip()
        if not name or "\n" in name or "\r" in name:
            continue
        if "\n" in value or "\r" in value:
            continue
        result.append(f"{name}: {value}")
    return result


class WebSocketClient:
    """One managed WebSocket connection."""

    def __init__(self, *, url: str, instance: str = "",
                 headers: dict[str, str] | list[tuple[str, str]] | None = None,
                 token: str | None = None, tls_skip_verify: bool = False,
                 hello_provider: Callable[[], dict[str, Any]] | None = None,
                 on_frame: Callable[[dict[str, Any]], None] | None = None,
                 on_status: Callable[[str, str], None] | None = None,
                 log_traffic: bool = False,
                 reconnect_interval: float = 3.0,
                 ping_interval: float = 20.0,
                 ping_timeout: float = 10.0):
        self.url = (url or "").strip()
        self.instance = instance
        self.token = token or ""
        self.tls_skip_verify = bool(tls_skip_verify)
        self.headers = headers or {}
        self.hello_provider = hello_provider
        self.on_frame = on_frame
        self.on_status = on_status
        self.log_traffic = bool(log_traffic)
        self.reconnect_interval = reconnect_interval
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._app: Any = None
        self._connected = False
        self._stopping = False
        self._ever_connected = False
        self._dropped_sends = 0
        self._last_error = ""

    # ------------------------------------------------------------------ state
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def dropped_sends(self) -> int:
        return self._dropped_sends

    @property
    def last_error(self) -> str:
        return self._last_error

    def _emit_status(self, status: str, detail: str = "") -> None:
        if detail:
            self._last_error = detail if status == "error" else self._last_error
            log.info(f"WebSocket Client: {status}: {detail}")
        else:
            log.debug(f"WebSocket Client: {status}")
        if self.on_status is not None:
            try:
                self.on_status(status, detail)
            except Exception as error:
                log.debug(f"WebSocket Client: status callback failed: {error}")

    # ------------------------------------------------------------------ start
    def start(self) -> bool:
        if websocket is None:
            self._emit_status("error", f"websocket-client is not available: {WEBSOCKET_IMPORT_ERROR}")
            return False
        valid, reason = validate_url(self.url)
        if not valid:
            self._emit_status("disconnected", reason)
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="WebSocketClient", daemon=True)
            self._thread.start()
        return True

    def _ssl_options(self) -> dict[str, Any]:
        if self.url.startswith("wss://") and self.tls_skip_verify:
            return {"cert_reqs": ssl.CERT_NONE}
        return {}

    def _run(self) -> None:
        self._emit_status("disconnected", "Connecting")
        app = websocket.WebSocketApp(
            self.url,
            header=build_header_list(self.headers, self.token),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        with self._lock:
            self._app = app
        try:
            app.run_forever(
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                reconnect=self.reconnect_interval,
                sslopt=self._ssl_options(),
            )
        except Exception as error:  # never surface a traceback to the user
            self._connected = False
            self._emit_status("error", f"Connection failed: {error}")
        finally:
            self._connected = False
            with self._lock:
                self._app = None

    # -------------------------------------------------------------- callbacks
    def _on_open(self, ws) -> None:
        self._connected = True
        self._ever_connected = True
        self._last_error = ""
        self._emit_status("connected", "Connected")
        hello = None
        if self.hello_provider is not None:
            try:
                hello = self.hello_provider()
            except Exception as error:
                log.warning(f"WebSocket Client: hello builder failed: {error}")
        if isinstance(hello, dict):
            self._send_json_ws(ws, hello)

    def _on_message(self, ws, message) -> None:
        try:
            size = len(message.encode("utf-8")) if isinstance(message, str) else len(message)
        except Exception:
            return
        if size > MAX_FRAME_BYTES:
            log.warning(f"WebSocket Client: dropping oversized inbound frame ({size} bytes)")
            return
        frame = parse_frame(message)
        if frame is None:
            return
        if self.log_traffic:
            log.debug(f"WebSocket Client <- {redact(frame)}")
        if self.on_frame is not None:
            try:
                self.on_frame(frame)
            except Exception as error:
                log.warning(f"WebSocket Client: frame callback failed: {error}")

    def _on_error(self, ws, error) -> None:
        self._connected = False
        message = str(error)
        if not message:
            message = type(error).__name__
        # If we had a working connection, the library is already retrying;
        # "Reconnecting" is more truthful than flashing a terminal error.
        self._emit_status("reconnecting" if self._ever_connected and not self._stopping else "error", message)

    def _on_close(self, ws, close_status_code=None, close_msg=None) -> None:
        self._connected = False
        if self._stopping:
            self._emit_status("disconnected", "Closed")
            return
        detail = "Connection lost"
        if close_status_code is not None:
            detail = f"Connection lost ({close_status_code})"
        self._emit_status("reconnecting", detail)

    # ------------------------------------------------------------------- send
    def _send_json_ws(self, ws, payload: dict[str, Any]) -> bool:
        if self.log_traffic:
            log.debug(f"WebSocket Client -> {redact(payload)}")
        try:
            data = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            log.warning(f"WebSocket Client: payload is not JSON serialisable: {error}")
            return False
        try:
            with self._lock:
                ws.send(data)
            return True
        except Exception as error:
            log.debug(f"WebSocket Client: send failed: {error}")
            return False

    def send_json(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            app = self._app
            connected = self._connected
        if not connected or app is None:
            self._dropped_sends += 1
            log.debug(f"WebSocket Client: dropping outbound frame while disconnected ({self._dropped_sends} total)")
            return False
        return self._send_json_ws(app, payload)

    # --------------------------------------------------------------- shutdown
    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            app = self._app
            thread = self._thread
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._connected = False
        self._emit_status("disconnected", "Closed")
        with self._lock:
            self._thread = None
            self._app = None

    # ------------------------------------------------------------ diagnostics
    @staticmethod
    def test_connection(url: str, headers: dict[str, str] | list[tuple[str, str]] | None = None,
                        token: str | None = None, tls_skip_verify: bool = False,
                        hello: dict[str, Any] | None = None,
                        timeout: float = 5.0) -> tuple[bool, str]:
        """Open a throwaway socket; success means the TCP/TLS/WS handshake worked."""
        if websocket is None:
            return False, f"websocket-client is not available: {WEBSOCKET_IMPORT_ERROR}"
        valid, reason = validate_url(url)
        if not valid:
            return False, reason

        sslopt = {"cert_reqs": ssl.CERT_NONE} if url.startswith("wss://") and tls_skip_verify else {}
        connection = None
        try:
            connection = websocket.create_connection(
                url,
                timeout=timeout,
                header=build_header_list(headers, token),
                sslopt=sslopt,
            )
            if hello is not None:
                connection.send(json.dumps(hello, separators=(",", ":")))
                try:
                    connection.settimeout(1.0)
                    reply = connection.recv()
                    frame = parse_frame(reply)
                    if isinstance(frame, dict) and frame.get("type") == "hello_ack":
                        return True, "Connected, hello_ack received"
                except Exception:
                    pass
            return True, "Connected"
        except Exception as error:
            return False, str(error) or type(error).__name__
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
