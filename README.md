# WebSocket Client for StreamController

Connect StreamController keys to **any** WebSocket server with a small,
documented JSON protocol (SC/WS v1). One plugin instance owns one connection;
each **WebSocket Key** action has its own channel.

Version: **1.0.0** · StreamController **1.5.0+** · GPL-3.0-or-later

## What it does

- Publishes key events (`down`, `up`, `short_up`, `hold_start`, `hold_stop`)
  tagged with a per-key channel.
- Renders server-pushed text (three positions + colours), images, background
  colour, overlays and an error state.
- Reconnects with exponential backoff and jitter, sends periodic WebSocket
  pings, and shows connection status in the plugin settings.
- Distinguishes a bare WebSocket upgrade ("Waiting for server") from a server
  that actually speaks SC/WS ("Connected") and from a closed connection
  ("Offline"). The red dot is only shown when the connection is known to be
  gone, never while the socket is merely silent.
- Restores the last display across page switches and app restarts.
- Generates a stable channel name when you leave the channel field empty.

It is deliberately **server-agnostic**: no Node-RED-specific logic, no raw
mapping/templating, no dials/touch, and no server mode.

## Requirements

- StreamController 1.5.0 or newer.
- No extra Python packages. StreamController already ships
  `websocket-client==1.9.0`, `requests`, `Pillow` and `loguru`.
- A server that speaks SC/WS v1 (or your own small script; see
  [`tests/fake_server.py`](tests/fake_server.py)).

## Install

Copy or symlink this repository into your StreamController data directory:

```sh
ln -s /path/to/streamcontroller-websocket-client \
      ~/.var/app/com.core447.StreamController/data/plugins/com_tahvodev_WebSocketClient
```

The repository root **is** the plugin root, so `manifest.json` must stay at the
top level. Enable the plugin in StreamController, then open its settings from
the plugin page.

## Quick start

1. Start a server, e.g. `python3 tests/fake_server.py --port 8765`.
2. In the plugin settings set the URL to `ws://127.0.0.1:8765` and press
   **Apply & Reconnect**. The status should become **Connected**.
3. Add a **WebSocket Key** action to a key.
4. Give it a channel such as `media.mute`, or copy the generated name shown in
   its configuration (`deck/<serial>/key/<identifier>/<state>`).
5. Press the key. The server receives an `event` frame.
6. From the fake server type `text media.mute MUTED` (or send a `set` frame)
   and the key updates.

## Settings

### Plugin settings

| Setting | Default | Notes |
| --- | --- | --- |
| URL | empty | `ws://` or `wss://`, placeholder `wss://example.com/sc`. |
| Instance name | hostname | Stable identity used in `hello`/`event` frames. |
| Bearer token | empty | Sent as `Authorization: Bearer …`. |
| Extra headers | one empty row | `Name: value` rows added to the handshake. |
| Connect on start | on | Start the connection when the plugin loads. |
| Restore last display | on | Persist text/colours/image URLs across restarts. |
| Log traffic | off | Debug logs frames; tokens and `Authorization` are redacted. |
| Skip TLS verification | off | For self-signed development servers only. |
| Allow local image paths | off | Lets a server reference files on this computer. |
| Require server response | on | Show **Waiting for server** until the server sends `hello_ack` or any valid SC/WS frame within 5 s. No dot is shown while the socket is open. |
| Apply & Reconnect | — | Saves settings and restarts the connection. |
| Test connection | — | Opens a throwaway socket and, with response checking on, waits for an SC/WS frame. |

The on-disk settings are profile-shaped (`connections: [...]`,
`default_connection`), but v1 renders only the default profile. Adding a
profile picker later is a UI-only change.

Status, last error, channel count and dropped-send count are updated from the
WebSocket thread through `GLib.idle_add`.

### WebSocket Key settings

| Setting | Default | Notes |
| --- | --- | --- |
| Channel | empty | Stable name; blank uses `deck/<serial>/<input>/<identifier>/<state>`. |
| Default text | empty | Shown before the server sends anything and after `reset`. |
| Default icon | built-in | Absolute path chosen with StreamController's asset picker. |
| Extra JSON | `{}` | Object merged into every event from this key. Invalid JSON sends `{}` and shows feedback. |
| Show offline dot | on | Small red dot composited onto the key image while the connection is known to be down. It is hidden while the socket is open but silent, and while a server overlay is active. |

## SC/WS v1

One JSON object per frame, maximum **1 MiB**. `v` must be `1`; other versions
are dropped and logged once. Unknown fields are ignored. Malformed frames never
raise.

### Plugin → server

On every (re)connect:

```json
{"v":1,"type":"hello","instance":"desk-pc","plugin":"1.0.0",
 "decks":[{"serial":"ABC123","name":"Stream Deck +","rows":2,"cols":4,"dials":4,"touch":true,"screen":false}],
 "pages":["Main","Media"],
 "channels":[{"channel":"media.mute","deck":"ABC123","page":"Main","input":"key","identifier":"0x2","state":0}]}
```

Channel-list changes are sent after a 500 ms debounce:

```json
{"v":1,"type":"channels","channels":[{ "...": "same shape as above" }]}
```

Key events:

```json
{"v":1,"type":"event","channel":"media.mute","instance":"desk-pc",
 "deck":"ABC123","page":"Main","input":"key","identifier":"0x2",
 "state":0,"event":"short_up","extra":{},"ts":1730000000.12}
```

`event` is one of `down`, `up`, `short_up`, `hold_start`, `hold_stop`.

### Server → plugin

```json
{"v":1,"type":"set","channel":"media.mute",
 "text":{"center":"MUTED","color":"#ff5555"},
 "image":{"url":"https://example/mute.png","size":0.8},
 "background":"#300000"}
```

- `set` merges by default. `"merge": false` clears the display first.
- `text` is either a bare string (centre) or `{top, center, bottom}`; each
  position is a string or `{text, color}`.
- `image` is `{url, size?, valign?, halign?}` or `{path, ...}`.
  `"image": null` clears it. Local paths require **Allow local image paths**.
- `background` is `#RRGGBB` / `#RRGGBBAA`; `null` clears it. Invalid colours are
  ignored and the previous colour is kept.
- `overlay` is an image reference (same shape as `image`) or a colour string;
  `null` clears it.
- `error` is a boolean.
- `channel` is required. `instance` is optional; a different instance is
  ignored. Unknown channels are logged at debug and otherwise ignored.

Other frame types:

```json
{"v":1,"type":"reset","channel":"media.mute"}
{"v":1,"type":"ping"}
{"v":1,"type":"hello_ack","server":"anything"}
```

`ping` is answered with `{"v":1,"type":"pong","ts":…}`.

### Connection health

A successful WebSocket upgrade only proves that something is listening. The
WebSocket protocol layer (or a proxy) answers pings without the application
being involved, so the plugin does not report **Connected** on the upgrade
alone. With **Require server response** on (the default), the plugin sends
`hello` and waits up to 5 s for `hello_ack` or any other valid SC/WS v1 frame
(`set`, `reset`, `ping`). Until one arrives the state is **Waiting for server**;
if nothing arrives the detail reads **No response from server** and the state
stays **Waiting** without reconnecting. A later frame promotes the same
connection to **Connected** with no reconnect.

A server that only reacts to key presses (or never replies at all) therefore
stays **Waiting for server**. That is only a status text: no dot is shown,
because the socket is still open and the plugin cannot prove the server is
unreachable. Disable **Require server response** to treat the handshake itself
as connected.

Only a closed, failed or never-opened socket is **Offline** and shows the red
dot. The dot is composited into the key image, so it does not hide the icon or
labels. Video default icons are the exception: they are handed to
StreamController as a media path and cannot carry the dot.

Colours are converted to RGBA integer lists (`0–255`) before they reach
StreamController's rendering API.

## Persistence

The merged display state is written to

```
<DATA_PATH>/settings/plugins/com_tahvodev_WebSocketClient/state_cache.json
```

with StreamController's `atomic_save_json`, debounced by 2 seconds and flushed
on shutdown. Only text, colours, image URLs/paths and layout scalars are stored —
never image bytes. The cache is capped at 500 channels. `reset` removes the
channel entry. With **Restore last display** off, the cache is not loaded or
written.

The image cache in memory holds at most 32 decoded images and is never written
to disk.

## Threading model

- The WebSocket runs in a daemon thread.
- The registry merges and coalesces frames off the GTK thread.
- Every action mutation and every settings-widget update goes through
  `GLib.idle_add`.
- Image downloads and decodes run in worker threads; only the final
  `set_media`/`show_overlay` call happens on the main thread.
- A per-channel revision plus a local image sequence rejects stale downloads.

## Security

- This is a **client only**. It does not listen for inbound TCP connections and
  cannot execute server-supplied code or templates.
- A server can drive the display of keys that use this plugin and can read the
  event metadata you send (deck serial, page, identifier, channel, `extra`).
- `wss://` verifies certificates by default. **Skip TLS verification** disables
  verification and should only be used on a trusted development network.
- A bearer token protects the handshake only as far as the transport does. **A
  bearer token is not a substitute for TLS**; use `wss://` or a trusted local
  network.
- Extra headers and tokens are redacted from traffic logs, but a server you
  connect to sees them.
- Local image paths are disabled by default; when enabled, a server can ask the
  plugin to read an image file reachable by the StreamController user. URLs are
  limited to `http(s)` and 5 MiB; paths must be absolute.
- The protocol has no per-key authentication. Anyone who can connect to your
  server can push display updates to known channels.

## Compatibility and upstream API notes

Implemented against the StreamController source at the time of writing
(`1.5.0-beta.15` line, September 2026 checkout). The signatures used are:

- `PluginBase.register(plugin_name=…, github_repo=…, plugin_version=…, app_version=…)`
- `ActionHolder(plugin_base=…, action_core=…, action_id_suffix=…, action_name=…, action_support=…, description=…, settings_schema=…)`
- `EventAssigner(id=…, ui_label=…, default_event=…, callback=…)` and
  `ActionCore.add_event_assigner`
- `set_media`, `set_top_label`/`set_center_label`/`set_bottom_label`,
  `set_background_color`, `show_overlay`/`hide_overlay`,
  `show_error`/`hide_error`
- `src.backend.Utils.AtomicSaveUtils.atomic_save_json`
- `GLib.idle_add`, `gl.DATA_PATH`, `gl.deck_manager.deck_controller`,
  `gl.page_manager.get_page_names()`

The upstream source now also contains an RPyC backend-process facility. This
plugin does **not** use a backend process; all work stays in the plugin process.

## Testing

```sh
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests -t .
.venv-dev/bin/python -m compileall -q .
```

The suite covers protocol parsing/merging/reset/version and instance filtering,
registry registration/coalescing/cache/revisions, and a real WebSocket
round-trip against [`tests/fake_server.py`](tests/fake_server.py) including
server restart, ping/pong and invalid frames. See
[`docs/TESTING.md`](docs/TESTING.md) for the fake-deck and real-deck checklist.

## v1 non-goals

- Node-RED-aware behaviour or Node-RED-specific frame shapes.
- Dials, touchscreens, touch keys, or screens.
- Multiple named connections in the settings UI.
- Server mode / listening sockets.
- Raw mapping or templating languages.
- Store submission and new runtime dependencies.
- A plugin backend process.

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE). Commercial use is permitted under
the terms of the GPL.
