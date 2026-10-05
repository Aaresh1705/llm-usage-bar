"""The provider base class: one provider's numbers, and when and how to ask
for more - pacing, backoff, the poll state kept across restarts.
"""

import json
import os
import threading
import time

from . import paths
from .usage import Usage, limit_identity, load_usage_cache, save_usage_cache
from .util import log, write_json_atomic


# Pacing. The usage endpoint sustains about one request per 100 seconds - at
# one a minute, every fourth or fifth came back 429 - and Claude Code on the
# same account draws on the same allowance. So the app polls well inside it,
# and slows itself down further whenever it is told no.
POLL_MIN_SECONDS = 120     # never faster than this, whatever config.json says
POLL_IDLE_SECONDS = 300    # numbers standing still: stretch out towards this
POLL_MAX_SECONDS = 900     # the slowest that a run of 429s can make the pace
MANUAL_GAP_SECONDS = 15    # Refresh pressed twice in a row asks once


def load_poll_state(path):
    """The pace learned from 429s and the last request, kept across restarts
    so a restart neither forgets a rate limit nor spends a request on it."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: float(raw[k]) for k in ("pace", "pace_at", "last_request", "retry_at", "backoff")
                if isinstance(raw.get(k), (int, float))}
    except Exception:
        return {}


def save_poll_state(path, state):
    write_json_atomic(path, state)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def source_path(base, key):
    """Each provider keeps its own cache and poll state. Claude keeps the
    original file names, so upgrading loses nothing."""
    if key == "claude":
        return base
    stem, ext = os.path.splitext(base)
    return "%s.%s%s" % (stem, key, ext)


class Source(object):
    """One provider: its latest numbers, and when and how to ask for more.

    The widget, the flyout and the notifications only read `usage`. Pacing is
    per provider, because each has its own endpoint and its own rate limit."""

    key = name = ""
    poll_min = POLL_MIN_SECONDS
    poll_idle = POLL_IDLE_SECONDS
    poll_max = POLL_MAX_SECONDS
    missing_error = None     # no credentials at all: the old numbers go too

    def __init__(self, app):
        self.app = app
        self.cache_path = source_path(paths.USAGE_CACHE, self.key)
        self.poll_path = source_path(paths.POLL_STATE, self.key)
        self.usage = load_usage_cache(self.cache_path) or Usage()
        if not self.usage.limits:
            self.usage.error = "Loading…"
        saved = load_poll_state(self.poll_path)
        self.pace = saved.get("pace", 0.0)              # learned from 429s
        self.pace_at = saved.get("pace_at", 0.0)
        self.last_request = saved.get("last_request", 0.0)
        self.backoff = saved.get("backoff", 0.0)
        self.retry_at = saved.get("retry_at", 0.0)
        self._unchanged = 0             # polls in a row that changed nothing
        self._last_seen = None
        self._fetching = False
        self._pending = None
        self._lock = threading.Lock()
        self.signed_out_since = None
        self.next_poll_at = self.first_poll_at()

    @property
    def cfg(self):
        return self.app.cfg

    def settings(self):
        """This provider's block under "providers" in config.json."""
        return (self.cfg.get("providers") or {}).get(self.key) or {}

    # -- what each provider says for itself --------------------------------
    def fetch(self, events):
        raise NotImplementedError

    def configured_interval(self):
        return self.settings().get("refresh_seconds", self.poll_min)

    def usage_page(self):
        return self.settings().get("usage_page_url") or ""

    def metric(self):
        """Which limit the widget shows."""
        return self.settings().get("metric") or "max"

    def wants_events(self, manual):
        return False

    def accept(self, result):
        """False to drop a result (after arranging a retry)."""
        return True

    def merge(self, result):
        """Carry over whatever a result does not repeat."""

    def watched(self, n):
        """(identity, limit) for every limit to notify about."""
        return [("%s:%s" % (self.key, limit_identity(l)), l) for l in self.usage.limits]

    def limit_name(self, lim):
        """A limit's name in a notification, where no heading says whose it is."""
        return "%s %s" % (self.name, lim["label"].lower())

    def signed_out_body(self, error):
        """What to say when `error` means the sign-in is the problem, else None."""
        return None

    def reset_hint(self, lim):
        """What to say under a limit that reports no reset time."""
        return None

    def notes(self):
        """Extra lines for the bottom of this provider's section."""
        return []

    # -- pacing --------------------------------------------------------------
    def base_interval(self):
        """The pace while the numbers are moving: the configured interval,
        never under poll_min, and slower after the endpoint has said no. What
        a 429 taught fades by a tenth every six hours without another."""
        try:
            configured = float(self.configured_interval())
        except (TypeError, ValueError):
            configured = self.poll_min
        learned = 0.0
        if self.pace:
            hours = max(0.0, time.time() - self.pace_at) / 3600.0
            learned = min(self.pace * 0.9 ** (hours / 6.0), self.poll_max)
        return max(configured, self.poll_min, learned)

    def poll_interval(self):
        """How long until the next timed poll. While the numbers stand still
        each wait is half as long again, up to poll_idle - so an idle evening
        costs a fraction of the requests - and the first change snaps back."""
        base = self.base_interval()
        if self._unchanged <= 0:
            return base
        return max(base, min(base * 1.5 ** self._unchanged, self.poll_idle))

    def first_poll_at(self):
        """At startup, don't spend a request on numbers the cache already has:
        a restart - or signing in after a reboot - waits until the cached
        figures are due, and still honours a rate limit the last run was under."""
        now = time.time()
        age = self.usage.age_seconds() if self.usage.limits else None
        if age is None:
            # Nothing to show yet: ask now, just not on top of the last run's request.
            return max(now, self.last_request + 60)
        return max(now, now - age + self.base_interval(), self.retry_at,
                   self.last_request + self.poll_min)

    def on_unlock(self):
        """Back at the desk: fresh numbers soon, as far as the pace allows."""
        now = time.time()
        soon = max(now + 3, self.last_request + self.poll_min, self.retry_at)
        self.next_poll_at = min(self.next_poll_at, soon)

    def maybe_poll(self):
        if self._fetching or time.time() < self.next_poll_at:
            return
        self.start_fetch()

    def start_fetch(self, manual=False):
        """A timed poll, or the Refresh button (`manual`), which skips the wait
        but asks only once when pressed twice in a row."""
        if self._fetching:
            return
        now = time.time()
        if manual:
            if now - self.last_request < MANUAL_GAP_SECONDS:
                return
        elif self.retry_at and now < self.retry_at:
            return
        self._fetch(self.wants_events(manual), manual)

    def _fetch(self, events, manual=False):
        self._fetching = True
        self.last_request = time.time()
        # provisional - replaced by plan_next_poll when the answer lands
        self.next_poll_at = self.last_request + self.poll_interval()

        def worker():
            result = self.fetch(events)
            result.manual = manual
            self._fetching = False          # before the result is visible, so
            with self._lock:                # apply_pending can fetch again
                self._pending = result

        threading.Thread(target=worker, daemon=True).start()

    def plan_next_poll(self, result):
        """When to ask again, from what this answer said."""
        now = time.time()
        if getattr(result, "status", None) == 429:
            if result.manual:
                # Refresh pressed into a rate limit: wait as before, learn nothing.
                self.retry_at = max(self.retry_at, now + max(self.backoff, self.poll_min))
            else:
                # The pace was too quick for what is left of the allowance, so
                # slow down for good - not just this once.
                self.pace = min(self.base_interval() * 1.25, self.poll_max)
                self.pace_at = now
                self.backoff = min(max(self.backoff * 2, self.poll_min), 1800)
                self.retry_at = now + self.backoff
            self.next_poll_at = self.retry_at
            log("%s: rate limited%s; next poll in %ds, then every %ds"
                % (self.name, " (Refresh)" if result.manual else "", self.retry_at - now,
                   self.base_interval()))
        else:
            if not result.error:
                self.backoff, self.retry_at = 0.0, 0.0
                seen = tuple((l.get("label"), round(float(l.get("percent") or 0), 1))
                             for l in result.limits)
                self._unchanged = self._unchanged + 1 if seen == self._last_seen else 0
                self._last_seen = seen
            self.next_poll_at = now + self.poll_interval()
        save_poll_state(self.poll_path, {
            "pace": self.pace, "pace_at": self.pace_at, "last_request": self.last_request,
            "retry_at": self.retry_at, "backoff": self.backoff})

    def apply_pending(self):
        """Take in a finished poll; True when `usage` changed."""
        if self._pending is None:        # cheap check on the hot pump path
            return False
        with self._lock:
            result, self._pending = self._pending, None
        if result is None or not self.accept(result):
            return False
        if result.error and result.error != self.missing_error and self.usage.limits:
            result.limits = self.usage.limits  # keep last good numbers on a blip
            result.spend, result.extra = self.usage.spend, self.usage.extra
            result.breakdown, result.buckets = self.usage.breakdown, self.usage.buckets
            result.updated = self.usage.updated
        self.merge(result)
        self.usage = result
        if not result.error:
            save_usage_cache(result, self.cache_path)
        self.plan_next_poll(result)
        return True
