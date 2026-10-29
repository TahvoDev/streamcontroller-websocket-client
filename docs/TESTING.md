# Testing the WebSocket Client plugin

Two levels are covered here: the automated suite (no StreamController needed)
and a fake-deck / real-deck end-to-end pass. The fake-deck pass is the one to
repeat before tagging a release.

## Automated tests

```sh
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests -t .
.venv-dev/bin/python -m compileall -q .
```

What is covered:

- `tests/test_protocol.py` — colours, text normalisation, merge/replace,
  reset, version and instance filtering, malformed/oversized frames.
- `tests/test_registry.py` — duplicate channel broadcast, idempotent
  registration, cache apply on ready, 25 ms coalescing with a fake clock,
  reset deleting the cache entry, stale revisions.
- `tests/test_ws_roundtrip.py` — a real `WebSocketApp` against
  `tests/fake_server.py`: `hello`/`channels`, event reception, `set` text and
  image URL, `reset`, ping/pong, invalid frames, server restart → reconnect,
  and the **Test connection** probe.
- `tests/test_offline_dot.py` — the offline-dot state machine: red only when
  the connection is known to be down (no dot while waiting), composited into
  the key image (base icon pixels survive), not stacked by repeated status
  changes or redraws, hidden while a server overlay is active, constant size
  when a server image uses `size < 1`, and removed on reconnect. It also drives
  a real `WebSocketClient` against
  `tests/fake_server.py` through waiting → connected and forced/complete
  outages. `tests/test_ws_verification.py` covers the acknowledgement timer,
  no-response timeout (no reconnect), timer cancellation, disabled
  verification, and the **Test connection** probe. `tests/sc_stubs.py` supplies the
  minimal StreamController/GTK stand-ins for the action-level assertions.

## Manual fake-deck E2E

These steps verify the parts that only exist inside StreamController: action
registration, main-thread rendering, page switching and display restoration.

1. Clone and run StreamController from source:

   ```sh
   git clone https://github.com/StreamController/StreamController.git
   cd StreamController
   python3 main.py --devel --data data --close-running
   ```

   Use a disposable `data` directory. If you already have a StreamController
   instance running, close it first; `--close-running` does that for you.

2. Install this plugin into the disposable data directory:

   ```sh
   ln -s /path/to/streamcontroller-websocket-client \
         data/plugins/com_tahvodev_WebSocketClient
   ```

   Restart StreamController and enable **WebSocket Client** if it is disabled.

3. Add a fake deck in the StreamController UI. Note its serial number and the
   page name it uses (for example `ABC123` and `Main`).

4. Place a **WebSocket Key** action on key `0,0`. Open the action settings and
   note the generated channel:

   ```
   deck/<serial>/key/0x2/0
   ```

   Either leave the channel blank (generated) or set a stable name such as
   `media.mute`. Give it default text `Mute` and leave **Show offline dot** on.

5. Start the fake server:

   ```sh
   python3 tests/fake_server.py --port 8765
   ```

   In the plugin settings set `ws://127.0.0.1:8765`, instance `desk-test`, then
   press **Apply & Reconnect**. The status row must say **Connected**, the
   channel count must be at least 1, and the fake server must print/receive a
   `hello` frame (use the `messages` command).

6. Press the fake key:

   ```sh
   python3 main.py --data data --emulate-input press ABC123 Main 0,0
   ```

   The fake server receives an `event` frame whose `channel` matches the action
   and whose `event` is `down`/`up`/`short_up` etc. The action's `extra` JSON,
   deck serial, page and identifier are included. With the offline dot on,
   stopping the server should show a small red dot while offline.

7. From the fake server console, exercise rendering:

   ```
   text media.mute MUTED
   bg media.mute #300000
   image media.mute https://example.com/mute.png
   error media.mute true
   reset media.mute
   ```

   Check top/centre/bottom labels and colours, image fitting, background,
   overlay/error, and that reset restores the default icon/text, transparent
   background, and hidden overlays.

8. Switch to another page and back. The key must show the last server state
   (or the reset/default state). Restart the whole StreamController app with
   **Restore last display** on and confirm the last text/colour/image URL comes
   back. With the toggle off, the key starts at its defaults.

9. Check all three connection states:

   - **Waiting for server (no dot):** start a listener that accepts the upgrade
     but sends no SC/WS frames (`FakeServer(auto_hello_ack=False)`). The status
     must read **Waiting for server** and the key must show **no dot**; after
     5 s the detail must read **No response from server** while the status
     stays **Waiting**, not Offline, and no reconnect is attempted. Send any
     valid frame and it must become **Connected**, without a reconnect.
   - **Connected (no dot):** with the normal fake server, the status is
     **Connected** and the dot is absent.
   - **Offline (red):** from the fake server console type `drop`, or stop the
     server. The red dot must appear within a second, stay while retries fail,
     and clear on reconnect. Stop/restart the whole server: the status must go
     through **Reconnecting** and return to **Connected**; the client sends a
     fresh `hello`, and a subsequent `set` updates the key again.

   While offline the dot must sit in the corner without replacing the icon or
   labels, and a server `overlay` must still take precedence.
   Outbound events sent while offline are dropped and the dropped-send counter
   increases.

10. Enable **Log traffic**, reconnect, and confirm the log contains frames but
    no bearer token or `Authorization` value. Then confirm a `ws://` URL with
    an invalid host fails cleanly and **Test connection** reports the error
    without crashing the settings window. With **Require server response** on,
    a listener that never replies must make **Test connection** report
    **Handshake OK, no server response**.

## Real deck checklist

- [ ] Fresh install → configured → working key in under five minutes.
- [ ] Text labels at top/centre/bottom, including custom colours and empty
      positions.
- [ ] Image aspect ratio/cropping with `size`, `valign`, `halign`.
- [ ] Background colour and RGBA alpha.
- [ ] Overlay and error state; overlay clears correctly.
- [ ] Offline dot appears on disconnect and disappears on reconnect; a
      server-sent overlay is not clobbered by the dot.
- [ ] Page switch restores the display; app restart restores it when enabled.
- [ ] Server restart reconnects without pressing Apply.
- [ ] `wss://` with a valid certificate, and the explicit skip-verification
      toggle against a self-signed test server.
- [ ] Action appears as **WebSocket Key** and has the five key event assigners.
- [ ] No GTK warnings about calls from worker threads in the log.

## Troubleshooting

- **Settings shows “websocket-client is missing”**: the plugin was loaded in an
  environment without StreamController's runtime packages. This plugin does
  not vendor them.
- **Status stays Disconnected with “No WebSocket URL configured”**: press
  **Apply & Reconnect** after entering the URL.
- **No event arrives**: check that the channel shown in the action matches the
  channel the server sends to, and that the action is on the active page.
- **Image does not load**: only `http(s)` URLs are accepted by default, the
  5 MiB limit applies, and local paths require the plugin toggle as well as an
  absolute path.
