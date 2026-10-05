"""One poll's worth of numbers (`Usage`), their cache on disk, and the
bookkeeping notifications need: limit identities, windows, what was sent.
"""

import json
from datetime import datetime, timedelta, timezone

from . import paths
from .util import parse_reset, write_json_atomic


class Usage(object):
    status = None
    stale = False
    with_events = False
    manual = False          # asked for by the Refresh button, not the timer

    def age_seconds(self):
        """How old the numbers are, or None when there are none."""
        if self.updated is None:
            return None
        return max(0.0, (datetime.now() - self.updated).total_seconds())

    def __init__(self):
        self.limits = []
        self.spend = None
        self.extra = None
        self.error = None
        self.updated = None
        self.raw = None
        self.breakdown = []     # where this week's usage went, by product
        self.buckets = []       # every other usage pool the endpoint reports
        self.events = None      # live grants; None = not checked yet
        self.events_checked = 0.0

    def by_key(self, key):
        if key == "max" and self.limits:
            return max(self.limits, key=lambda l: l["percent"])
        for lim in self.limits:
            if lim["key"] == key:
                return lim
        return None

    def percent(self, key):
        lim = self.by_key(key)
        return lim["percent"] if lim else 0.0


def save_usage_cache(usage, path=None):
    try:
        write_json_atomic(path or paths.USAGE_CACHE, {
            "limits": usage.limits, "extra": usage.extra, "spend": usage.spend,
            "breakdown": usage.breakdown, "buckets": usage.buckets,
            "events": getattr(usage, "events", None),
            "events_checked": getattr(usage, "events_checked", 0),
            "updated": usage.updated.isoformat() if usage.updated else None})
    except Exception:
        pass


def load_usage_cache(path=None):
    """Show the last known numbers immediately at startup instead of a blank
    panel - a fresh poll can be a minute away, or rate limited."""
    u = Usage()
    try:
        with open(path or paths.USAGE_CACHE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        u.limits = blob.get("limits") or []
        u.extra, u.spend = blob.get("extra"), blob.get("spend")
        u.breakdown, u.buckets = blob.get("breakdown") or [], blob.get("buckets") or []
        u.events, u.events_checked = blob.get("events"), float(blob.get("events_checked") or 0)
        if blob.get("updated"):
            u.updated = datetime.fromisoformat(blob["updated"])
        if not u.limits:
            return None
        age = u.age_seconds()
        if age is None or age > 12 * 3600:
            return None          # a day-old percentage is not worth showing
        u.stale = True
        return u
    except Exception:
        return None


def window_key(resets_at):
    """A stable name for one limit window.

    The API reports the same reset instant with different microseconds on
    every request ("...12:10:00.165535" then "...12:10:00.134418"), so the raw
    string cannot identify a window - comparing it made every poll look like a
    fresh window and re-announced the same threshold once a minute. Resets land
    on whole minutes; rounding to the minute absorbs the jitter.
    """
    if not resets_at:
        return ""
    moment = parse_reset(resets_at)
    if moment is None:
        return str(resets_at).split(".")[0]
    moment = moment.astimezone(timezone.utc) + timedelta(seconds=30)
    return moment.strftime("%Y-%m-%dT%H:%M")


def limit_identity(lim):
    """A stable name for one limit across polls. Several model-scoped weekly
    limits share the kind "weekly_scoped", so the model is part of the name."""
    if lim.get("key") == "weekly_scoped":
        return "weekly_scoped:%s" % lim.get("label", "")
    return lim.get("key") or "?"


def watched_limits(usage, metrics):
    """(identity, limit) for every limit the metric names select. "max" is
    resolved to whichever limit is highest right now, but keeps that limit's
    own identity - so the answer changing does not look like a new window."""
    seen, out = set(), []
    for metric in metrics:
        if metric == "max":
            chosen = [max(usage.limits, key=lambda l: l["percent"])] if usage.limits else []
        else:
            chosen = [l for l in usage.limits if l.get("key") == metric]
        for lim in chosen:
            identity = limit_identity(lim)
            if identity not in seen:
                seen.add(identity)
                out.append((identity, lim))
    return out


def load_notify_state(path):
    """What has already been announced, per limit: {key: [window, level]}.
    Kept on disk so a restart does not announce it all over again."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: (v[0], v[1]) for k, v in raw.items() if isinstance(v, list) and len(v) == 2}
    except Exception:
        return {}


def save_notify_state(path, state):
    try:
        write_json_atomic(path, {k: [v[0], v[1]] for k, v in state.items()})
    except Exception:
        pass


def shown_error(usage):
    """The error worth showing. Being rate limited while the numbers are only
    minutes old is the endpoint pacing us, not news - the panel already says
    when the numbers are from."""
    if usage.error and usage.status == 429 and usage.limits:
        age = usage.age_seconds()
        if age is not None and age < 900:
            return None
    return usage.error
