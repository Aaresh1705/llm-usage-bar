"""Small helpers used everywhere: the log, colours, times, state files."""

import json
import os
from datetime import datetime, timezone

from . import paths


def log(msg):
    line = "%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    for path in (paths.LOG_PATH, paths.FALLBACK_LOG):
        try:
            if path is paths.FALLBACK_LOG:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            return
        except Exception:
            continue


def write_json_atomic(path, data, **dump_kw):
    """Save `data` as JSON so that a reader never sees half a file.

    The state files are read by more than this process: an app embedding the
    usage bar follows them while the standalone runs, and a copy starting up
    reads what the last one left. A plain overwrite truncates first, so a
    reader arriving mid-write found an empty or cut-off file. Here the text
    goes to '<path>.<pid>.tmp' first and replaces the file in one step.

    Windows refuses that replace while a reader holds the file open. It is
    tried once more straight away - no sleep, this runs on the UI thread - and
    then the text is written in place, which is exactly what happened before,
    so it is never worse than that. The tmp file never outlives the call.
    Never raises; False when nothing could be written."""
    try:
        text = json.dumps(data, **dump_kw)
    except Exception:
        return False                    # unserialisable: leave the old file whole
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        for _ in range(2):
            try:
                os.replace(tmp, path)
                return True
            except OSError:
                pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except Exception:
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def hex_to_rgba(value, alpha=255):
    v = str(value).lstrip("#")
    if len(v) == 8:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), int(v[6:8], 16))
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), alpha)


def shade(color, factor):
    """Lighten (factor > 0) or darken (factor < 0) a colour, keeping its hue."""
    r, g, b = hex_to_rgba(color)[:3]
    if factor >= 0:
        r, g, b = (int(c + (255 - c) * factor) for c in (r, g, b))
    else:
        r, g, b = (int(c * (1 + factor)) for c in (r, g, b))
    return "#%02X%02X%02X" % (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def color_for(pct, thresholds):
    chosen = thresholds[0]["color"]
    for t in sorted(thresholds, key=lambda x: x["at"]):
        if pct >= t["at"]:
            chosen = t["color"]
    return chosen


def parse_reset(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def human_delta(dt):
    if not dt:
        return "?"
    secs = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
    if secs <= 0:
        return "now"
    hours, rem = divmod(int(secs), 3600)
    mins = rem // 60
    if hours >= 24:
        return "%dd %dh" % (hours // 24, hours % 24)
    if hours:
        return "%dh %dm" % (hours, mins)
    return "%dm" % mins
