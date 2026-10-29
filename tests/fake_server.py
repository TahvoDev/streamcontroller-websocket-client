# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny SC/WS v1 fake server for integration tests and manual fake-deck runs.

Run it directly:

    python3 tests/fake_server.py --port 8765

Then type commands such as ``text media.mute MUTED`` or
``send {"v":1,"type":"set","channel":"media.mute","background":"#300000"}``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import threading
import time
from typing import Any, Callable

try:  # websockets >= 13
    from websockets.asyncio.server import serve as _serve
except ImportError:  # pragma: no cover - older websockets
    from websockets.server import serve as _serve


class FakeServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *,
                 auto_hello_ack: bool = True,
                 on_message: Callable[[dict[str, Any]], None] | None = None):
        self.host = host
        self.port = port
        self.auto_hello_ack = auto_hello_ack
        self.on_message = on_message
        self.messages: list[Any] = []
        self.raw_messages: list[str] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._connections: set[Any] = set()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 5.0) -> "FakeServer":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._run, name="FakeSCWSServer", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("fake server did not start")
        if self._start_error is not None:
            raise self._start_error
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop

        async def start_server():
            return await _serve(self._handler, self.host, self.port, max_size=None)

        try:
            self._server = loop.run_until_complete(start_server())
            sockets = getattr(self._server, "sockets", None)
            if sockets:
                self.port = sockets[0].getsockname()[1]
            self._ready.set()
            loop.run_forever()
        except BaseException as error:  # pragma: no cover - startup failure
            self._start_error = error
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def stop(self, timeout: float = 5.0) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._close_all(), loop)
            future.result(timeout=timeout)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self._server = None

    async def _close_all(self) -> None:
        for websocket in list(self._connections):
            try:
                await websocket.close()
            except Exception:
                pass
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------ handler
    async def _handler(self, websocket, *_args) -> None:
        self._connections.add(websocket)
        try:
            async for message in websocket:
                with self._lock:
                    self.raw_messages.append(message if isinstance(message, str) else str(message))
                try:
                    frame = json.loads(message)
                except (TypeError, ValueError):
                    frame = None
                if isinstance(frame, dict):
                    with self._lock:
                        self.messages.append(frame)
                    if self.auto_hello_ack and frame.get("type") == "hello":
                        await websocket.send(json.dumps({
                            "v": 1, "type": "hello_ack", "server": "fake-server",
                        }))
                    if self.on_message is not None:
                        try:
                            self.on_message(frame)
                        except Exception:
                            pass
        finally:
            self._connections.discard(websocket)

    # ------------------------------------------------------------------- output
    def send(self, payload: dict[str, Any] | str) -> None:
        loop = self._loop
        if loop is None:
            raise RuntimeError("fake server is not running")
        text = payload if isinstance(payload, str) else json.dumps(payload)
        future = asyncio.run_coroutine_threadsafe(self._broadcast(text), loop)
        future.result(timeout=5.0)

    def broadcast(self, payload: dict[str, Any] | str) -> None:
        self.send(payload)

    async def _broadcast(self, text: str) -> None:
        for websocket in list(self._connections):
            try:
                await websocket.send(text)
            except Exception:
                pass

    # --------------------------------------------------------------- assertions
    def messages_of_type(self, message_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [message for message in self.messages if message.get("type") == message_type]

    def wait_for(self, predicate: Callable[[list[dict[str, Any]]], bool],
                 timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                snapshot = list(self.messages)
            if predicate(snapshot):
                return True
            time.sleep(0.02)
        return False


def _set(channel: str, **fields: Any) -> dict[str, Any]:
    payload = {"v": 1, "type": "set", "channel": channel}
    payload.update(fields)
    return payload


def _print_help() -> None:
    print("commands:")
    print("  text <channel> <text>        set centre text")
    print("  reset <channel>              reset the key")
    print("  image <channel> <url>        set an image URL")
    print("  bg <channel> <#color>        set background colour")
    print("  error <channel> <true|false> toggle the error state")
    print("  send <json>                  broadcast a raw frame")
    print("  messages                     show frames received from the plugin")
    print("  quit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SC/WS v1 fake server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    server = FakeServer(host=args.host, port=args.port).start()
    print(f"SC/WS v1 fake server listening on ws://{args.host}:{server.port}")
    _print_help()
    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue
            try:
                parts = shlex.split(line)
                command = parts[0].lower()
                if command in ("quit", "exit"):
                    break
                if command == "help":
                    _print_help()
                elif command == "text" and len(parts) >= 3:
                    server.send(_set(parts[1], text=" ".join(parts[2:])))
                elif command == "reset" and len(parts) >= 2:
                    server.send({"v": 1, "type": "reset", "channel": parts[1]})
                elif command == "image" and len(parts) >= 3:
                    server.send(_set(parts[1], image={"url": parts[2]}))
                elif command == "bg" and len(parts) >= 3:
                    server.send(_set(parts[1], background=parts[2]))
                elif command == "error" and len(parts) >= 3:
                    server.send(_set(parts[1], error=parts[2].lower() in ("1", "true", "yes")))
                elif command == "send" and len(parts) >= 2:
                    server.send(json.loads(line[len("send "):]))
                elif command == "messages":
                    with server._lock:
                        for message in server.messages:
                            print(json.dumps(message))
                else:
                    print("unknown command; type help")
            except Exception as error:
                print(f"error: {error}")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
