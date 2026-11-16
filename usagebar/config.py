"""config.json: the defaults, and reading the user's file over them."""

import json
import os

from . import paths
from .util import deep_merge, log


DEFAULT_CONFIG = {
    "primary_metric": "session",
    "secondary_metric": "weekly_all",
    "icon": {
        "style": "bar_text",
        "count": 1,
        "segment_mode": "cells",
        "use_guid": True,
        "segments": ["text", "bar", "bar"],
        "reverse_segments": False,
        "label_format": "{pct}",
        "orientation": "horizontal",
        "margin": 1,
        "corner_radius": 2,
        "track_color": "#4A4A4A",
        "track_alpha": 200,
        "text_color": "#FFFFFF",
        "text_shadow": True,
        "font_file": "segoeuib.ttf",
        "font_scale": 0.78,
        "bar_thickness": 0.30,
        "ring_thickness": 0.22,
        "show_secondary": True,
        "secondary_thickness": 0.14,
        "secondary_color": "#7A7AFF",
        "gap": 0.08,
        "supersample": 8,
        "skull_at": 100,
    },
    "thresholds": [
        {"at": 0, "color": "#22C55E"},
        {"at": 60, "color": "#F59E0B"},
        {"at": 85, "color": "#EF4444"},
    ],
    "tooltip_template": "Claude \u00b7 {primary_label}: {primary}%\n{secondary_label}: {secondary}%\nResets {primary_reset_short} (in {primary_reset_in})",
    "notifications": {"enabled": True, "at": [80, 95, 100],
                      "metrics": ["session", "weekly_all", "weekly_scoped"]},
    "flyout": {
        "width": 320,
        "theme": "auto",
        "accent": "auto",
        "corner_offset": [12, 12],
        "close_on_focus_loss": True,
    },
    "tray": {"enabled": False},
    "taskbar_widget": {
        "enabled": True,
        "metric": None,
        "corner": "left",
        "width": 128,
        "offset": [8, 0],
        "padding": 5,
        "text_color": "auto",
        "muted_color": "auto",
        "font_file": "segoeui.ttf",
        "bold_font_file": "segoeuib.ttf",
        "show_bar": True,
        "show_reset": True,
        "bar_height": 4,
        "second_bar": "weekly_scoped",
        "second_bar_colors": ["#6366F1", "#A855F7", "#EC4899"],
        "background": "transparent",
        "background_alpha": 255,
        "corner_radius": 6,
        "stale_opacity": 0.7,
        "supersample": 3,
    },
    "left_click": "flyout",
    # Which usage to show. Switch them from the right-click menu, or here.
    "providers": {
        "claude": {
            "enabled": True,
            "refresh_seconds": 120,
            "usage_page_url": "https://claude.ai/settings/usage",
            # Ask for promotional grants once an hour. The endpoint only
            # answers Claude Code, so that one request uses Claude Code's user
            # agent; false never does that.
            "check_events": True,
        },
        "ollama": {
            "enabled": False,
            "api_key": "",          # optional; `ollama signin` is used when empty
            "metric": "max",        # what the widget shows: monthly, session, weekly, max
            "reset_day": None,      # day of the month the credits renew (1-31)
            "refresh_seconds": 300,
        },
    },
}


# Settings that belong to Claude, and were top-level before there were others.
CLAUDE_KEYS = ("refresh_seconds", "check_events", "usage_page_url")


def load_config():
    cfg = DEFAULT_CONFIG
    try:
        if os.path.exists(paths.CONFIG_PATH):
            with open(paths.CONFIG_PATH, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            cfg = deep_merge(DEFAULT_CONFIG, user)
            # The old single "metric" still means what it says: otherwise the
            # default "metrics" list is merged in and quietly wins over it.
            n = user.get("notifications") if isinstance(user, dict) else None
            if isinstance(n, dict) and "metric" in n and "metrics" not in n:
                cfg["notifications"] = dict(cfg["notifications"], metrics=[n["metric"]])
            # Claude's own settings used to sit at the top level; a value set
            # there still counts, unless providers.claude sets it too.
            claude = ((user.get("providers") or {}).get("claude") or {}) \
                if isinstance(user, dict) else {}
            moved = {k: user[k] for k in CLAUDE_KEYS
                     if isinstance(user, dict) and k in user and k not in claude}
            if moved:
                providers = dict(cfg["providers"])
                providers["claude"] = dict(providers["claude"], **moved)
                cfg["providers"] = providers
        else:
            with open(paths.CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=2)
    except Exception as exc:
        log("config load failed: %r" % (exc,))
    return cfg
