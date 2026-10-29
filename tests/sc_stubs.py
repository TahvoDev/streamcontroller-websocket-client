# SPDX-FileCopyrightText: 2026 TahvoDev
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight StreamController/GTK stand-ins for action-level tests.

``actions/WebSocketKey.py`` imports GTK and StreamController at module level.
The tests only need the offline-dot/overlay logic, so call :func:`install`
before importing the action to register minimal fake modules.
"""

from __future__ import annotations

import sys
import types
from typing import Any


class _Events:
    DOWN = "down"
    UP = "up"
    SHORT_UP = "short_up"
    HOLD_START = "hold_start"
    HOLD_STOP = "hold_stop"


class _Key:
    Events = _Events


class Input:
    Key = _Key


class EventAssigner:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs


class ActionCore:
    """Replaces real rendering with a call recorder."""

    def __init__(self, *args: Any, **kwargs: Any):
        self.on_ready_called = True
        self.assigners: list[Any] = []
        self.render_calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._settings_store: dict[str, Any] = {}

    def add_event_assigner(self, assigner: Any) -> None:
        self.assigners.append(assigner)

    def get_settings(self) -> dict[str, Any]:
        return dict(self._settings_store)

    def set_settings(self, settings: dict[str, Any]) -> None:
        self._settings_store = dict(settings)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.render_calls.append((name, args, kwargs))

    def set_top_label(self, *args, **kwargs):
        self._record("set_top_label", *args, **kwargs)

    def set_center_label(self, *args, **kwargs):
        self._record("set_center_label", *args, **kwargs)

    def set_bottom_label(self, *args, **kwargs):
        self._record("set_bottom_label", *args, **kwargs)

    def set_background_color(self, *args, **kwargs):
        self._record("set_background_color", *args, **kwargs)

    def set_media(self, *args, **kwargs):
        self._record("set_media", *args, **kwargs)

    def show_overlay(self, *args, **kwargs):
        self._record("show_overlay", *args, **kwargs)

    def hide_overlay(self, *args, **kwargs):
        self._record("hide_overlay", *args, **kwargs)

    def show_error(self, *args, **kwargs):
        self._record("show_error", *args, **kwargs)

    def hide_error(self, *args, **kwargs):
        self._record("hide_error", *args, **kwargs)


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


def _no_network(*args: Any, **kwargs: Any):
    raise RuntimeError("network access is not available in these tests")


def install() -> None:
    """Register fake ``gi``, ``globals``, ``requests`` and ``src.*`` modules."""
    if sys.modules.get("_sc_action_stubs_installed"):
        return

    gi = types.ModuleType("gi")
    gi.require_version = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    repository = types.ModuleType("gi.repository")
    for name in ("Adw", "Gdk", "GLib", "Gtk"):
        setattr(repository, name, type(name, (), {}))
    gi.repository = repository  # type: ignore[attr-defined]

    globals_module = types.ModuleType("globals")
    globals_module.app = None  # type: ignore[attr-defined]

    requests_module = types.ModuleType("requests")
    requests_module.get = _no_network  # type: ignore[attr-defined]

    src = _module("src")
    backend = _module("src.backend")
    deck_management = _module("src.backend.DeckManagement")
    input_identifier = _module("src.backend.DeckManagement.InputIdentifier")
    input_identifier.Input = Input  # type: ignore[attr-defined]
    plugin_manager = _module("src.backend.PluginManager")
    action_core = _module("src.backend.PluginManager.ActionCore")
    action_core.ActionCore = ActionCore  # type: ignore[attr-defined]
    event_assigner = _module("src.backend.PluginManager.EventAssigner")
    event_assigner.EventAssigner = EventAssigner  # type: ignore[attr-defined]

    sys.modules.update({
        "gi": gi,
        "gi.repository": repository,
        "globals": globals_module,
        "requests": requests_module,
        "src": src,
        "src.backend": backend,
        "src.backend.DeckManagement": deck_management,
        "src.backend.DeckManagement.InputIdentifier": input_identifier,
        "src.backend.PluginManager": plugin_manager,
        "src.backend.PluginManager.ActionCore": action_core,
        "src.backend.PluginManager.EventAssigner": event_assigner,
        "_sc_action_stubs_installed": types.ModuleType("_sc_action_stubs_installed"),
    })
