"""LLM Usage Bar's behaviour, driven through the real code: notifications,
events, pacing, providers, and moving over from the old name.

Run:  python tests/test_usage_bar.py

The reset timestamps here are shaped like the real API's: the same reset
instant comes back with different microseconds on every request, e.g.
"2026-09-23T12:10:00.165535+00:00" then "2026-09-23T12:10:00.134418+00:00".
"""

import base64
import copy
import itertools
import json
import os
import struct
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from usagebar import deps  # noqa: E402 - loads requests and Pillow
import usagebar  # noqa: E402
from usagebar import (app as _app, config, flyout, paths, render, source, tkui,  # noqa: E402
                      toast, usage, util, widget, win32)
from usagebar import providers  # noqa: E402
from usagebar.providers import claude, ollama  # noqa: E402

if not deps.OK:
    raise SystemExit("Pillow / requests are missing")


class Modules(object):
    """All of the app's modules as one namespace. Reading a name finds it in
    whichever module has it; assigning one - to patch it - rebinds it in every
    module that holds the same object, since that is where the code looks it
    up (a function imported into three modules is patched in all three)."""

    def __init__(self, modules):
        object.__setattr__(self, "_modules", modules)

    def __getattr__(self, name):
        for module in self._modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        old = getattr(self, name)
        for module in self._modules:
            if vars(module).get(name, object()) is old:
                setattr(module, name, value)


app = Modules([paths, util, deps, config, usage, source, claude, ollama, providers, render,
               win32, tkui, widget, flyout, toast, _app, usagebar])

# Everything the app would write - its log, the usage cache, the notification
# state, the config - goes to a scratch directory. Without this the tests wrote
# into the real app's files, and the running widget picked up a made-up event.
_SANDBOX = tempfile.mkdtemp(prefix="llm-usage-bar-tests-")
for _name in ("LOG_PATH", "FALLBACK_LOG", "USAGE_CACHE", "NOTIFY_STATE", "POLL_STATE",
              "CONFIG_PATH", "CRASH_LOG"):
    setattr(app, _name, os.path.join(_SANDBOX, os.path.basename(getattr(app, _name))))

_micro = itertools.count(100000, 7919)


def stamp(base):
    """`base` with fresh microseconds, like every API response."""
    return "%s.%06d+00:00" % (base, next(_micro) % 1000000)


def use_state_dir(state_dir):
    """Point the app's per-provider files (cache, poll state) at `state_dir`."""
    for name in ("USAGE_CACHE", "POLL_STATE"):
        setattr(app, name, os.path.join(state_dir, os.path.basename(getattr(app, name))))


class Harness(object):
    """TrayApp's polling and notification logic, with real provider sources
    and no windows attached."""

    check_notifications = app.TrayApp.check_notifications
    _notify_limit = app.TrayApp._notify_limit
    _check_signed_out = app.TrayApp._check_signed_out
    _grant_alerts = app.TrayApp._grant_alerts
    maybe_poll = app.TrayApp.maybe_poll
    apply_pending = app.TrayApp.apply_pending
    active = app.TrayApp.active
    sync_sources = app.TrayApp.sync_sources
    set_provider = app.TrayApp.set_provider

    def __init__(self, state_dir=None, metrics=("session",), at=(80, 95, 100), busy=False,
                 providers=("claude",)):
        state_dir = state_dir or tempfile.mkdtemp()
        use_state_dir(state_dir)
        self.cfg = {"notifications": {"enabled": True, "at": list(at), "metrics": list(metrics)},
                    "refresh_seconds": 60,
                    "providers": dict((k, {"enabled": True}) for k in providers)}
        self.sent = []
        self.busy = busy
        self.state_path = os.path.join(state_dir, ".notify_state.json")
        self.last_notified = app.load_notify_state(self.state_path)
        self.locked, self.flyout = False, None
        self.sources = {}
        self.sync_sources()
        for src in self.sources.values():
            src.usage.error = None          # not "Loading…": tests start from a clean slate

    @property
    def claude(self):
        return self.sources["claude"]

    @property
    def ollama(self):
        return self.sources["ollama"]

    @property
    def usage(self):
        return self.claude.usage

    @usage.setter
    def usage(self, value):
        self.claude.usage = value

    def update_icon(self):
        pass

    def reload_config(self):
        self.cfg = app.load_config()
        self.sync_sources()

    def notify(self, title, body):
        if self.busy:
            return False
        self.sent.append((title, body))
        return True

    def set(self, **limits):
        """set(session=(pct, reset_base[, label]), ...)"""
        self.usage.error = None
        self.usage.limits = []
        for key, spec in limits.items():
            pct, base = spec[0], spec[1]
            label = spec[2] if len(spec) > 2 else key
            kind = key.split("__")[0]
            self.usage.limits.append({"key": kind, "label": label, "percent": float(pct),
                                      "resets_at": stamp(base) if base else None,
                                      "severity": "normal", "group": kind})

    def poll(self, **limits):
        self.set(**limits)
        self.check_notifications()
        return len(self.sent)

    def fail(self, error):
        self.usage.error = error
        self.check_notifications()


FAILURES = []


def check(name, condition, detail=""):
    print("  %s  %s%s" % ("PASS" if condition else "FAIL", name, ("  - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(name)


def titles(h):
    return [t for t, _ in h.sent]


S1 = "2026-09-23T12:10:00"      # a session window
S2 = "2026-09-23T17:10:00"      # the next one
W1 = "2026-09-26T07:00:00"      # a weekly window
SIGNED_OUT = "Signed out - run any Claude Code command"


def test_spam():
    print("sitting at 100% for an hour of polls, reset string jittering every time")
    h = Harness()
    for _ in range(60):
        h.poll(session=(100, S1))
    check("exactly one notification, not one per poll", len(h.sent) == 1, "%d sent" % len(h.sent))

    print("climbing 50 -> 80 -> 95 -> 100 inside one window")
    h = Harness()
    for pct in (50, 70, 80, 81, 90, 95, 97, 100, 100, 100):
        h.poll(session=(pct, S1))
    check("one per threshold: 80, 95, 100", len(h.sent) == 3, repr(titles(h)))
    check("the 100% one says the limit is reached",
          bool(h.sent) and "reached" in h.sent[-1][0].lower(), repr(h.sent[-1:]))

    print("jumping from 70% straight to 100% between polls")
    h = Harness()
    for pct in (70, 100, 100):
        h.poll(session=(pct, S1))
    check("one notification for the jump, not three", len(h.sent) == 1, repr(titles(h)))

    print("the window genuinely rolls over while usage stays high")
    h = Harness()
    for _ in range(5):
        h.poll(session=(100, S1))
    for _ in range(5):
        h.poll(session=(96, S2))
    check("one per window: two in total", len(h.sent) == 2, repr(titles(h)))

    print("dipping below a threshold and coming back in the same window")
    h = Harness()
    for pct in (85, 79, 85, 79, 85):
        h.poll(session=(pct, S1))
    check("80% announced once", len(h.sent) == 1, repr(titles(h)))

    print("errors and rate limiting flapping in between")
    h = Harness()
    for i in range(20):
        if i % 2:
            h.fail("Rate limited by the usage API")
        else:
            h.poll(session=(100, S1))
    check("still one notification", len(h.sent) == 1, "%d sent" % len(h.sent))

    print("the app restarts while you are at 100%")
    state = tempfile.mkdtemp()
    Harness(state).poll(session=(100, S1))
    h2 = Harness(state)                      # a new process, same state file
    for _ in range(10):
        h2.poll(session=(100, S1))
    check("no repeat after a restart", len(h2.sent) == 0, "%d sent after restart" % len(h2.sent))

    print("using a limit reset: 100%, back to 3% in the same window, then out again")
    h = Harness()
    for pct in (100, 100, 3, 10, 50, 81, 100, 100):
        h.poll(session=(pct, S1))
    check("reached, then 80% and reached again after the reset",
          titles(h) == ["session limit reached", "Claude usage 81%", "session limit reached"],
          repr(titles(h)))


def test_coverage():
    print("the weekly limit runs out while the session is fine")
    h = Harness(metrics=("session", "weekly_all"))
    for _ in range(10):
        h.poll(session=(30, S1), weekly_all=(100, W1, "Weekly (all models)"))
    check("weekly exhaustion is announced once", len(h.sent) == 1, repr(titles(h)))

    print("old config shape without 100 in it ('metric': 'session', 'at': [80, 95])")
    h = Harness()
    h.cfg["notifications"] = {"enabled": True, "at": [80, 95], "metric": "session"}
    for pct in (96, 96, 100, 100):
        h.poll(session=(pct, S1))
    check("95% and then 'limit reached' - 100% is always announced",
          titles(h) == ["Claude usage 96%", "session limit reached"], repr(titles(h)))

    print("the old single 'metric' key in config.json")
    for user, want in (({"metric": "max"}, ["max"]),
                       ({"metric": "max", "metrics": ["session"]}, ["session"]),
                       ({}, ["session", "weekly_all", "weekly_scoped"])):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"notifications": user}, fh)
        got = app.load_config()["notifications"]["metrics"]
        check("%r -> metrics %r" % (user, want), got == want, repr(got))
    check("the defaults are not changed by it",
          app.DEFAULT_CONFIG["notifications"]["metrics"] == ["session", "weekly_all", "weekly_scoped"])

    print("session and weekly cross on the same poll")
    h = Harness(metrics=("session", "weekly_all"))
    h.poll(session=(96, S1, "Session (5h)"), weekly_all=(100, W1, "Weekly (all models)"))
    check("one toast that mentions both, not one hiding the other",
          len(h.sent) == 1 and "Session" in h.sent[0][1] and "Weekly" in h.sent[0][1],
          repr(h.sent))

    print("two model-scoped weekly limits, reported in alternating order")
    h = Harness(metrics=("weekly_scoped",))
    for i in range(10):
        a = ("weekly_scoped__a", (100, W1, "Weekly (Opus)"))
        b = ("weekly_scoped__b", (82, W1, "Weekly (Sonnet)"))
        h.poll(**dict([a, b] if i % 2 else [b, a]))
    check("each announced once, no flip-flop", len(h.sent) == 1 and len(h.last_notified) == 2,
          "%d toasts, state=%r" % (len(h.sent), sorted(h.last_notified)))

    print("metric 'max' while the highest limit keeps changing")
    h = Harness(metrics=("max",))
    for i in range(10):
        s, w = (96, 95) if i % 2 else (94, 95)
        h.poll(session=(s, S1, "Session (5h)"), weekly_all=(w, W1, "Weekly (all models)"))
    check("no repeat each time the leader changes", len(h.sent) <= 2, repr(titles(h)))


def test_delivery():
    print("a toast held back while you are in a fullscreen app")
    h = Harness(busy=True)
    for _ in range(5):
        h.poll(session=(100, S1))
    check("nothing shown while busy", len(h.sent) == 0)
    h.busy = False
    for _ in range(5):
        h.poll(session=(100, S1))
    check("shown once you are back, then not again", len(h.sent) == 1, repr(titles(h)))

    print("signed out for a long while")
    h = Harness()
    h.poll(session=(40, S1))
    for _ in range(30):
        h.fail(SIGNED_OUT)
    check("no toast before the grace period", len(h.sent) == 0, repr(titles(h)))
    h.cfg["notifications"]["signed_out_after"] = 0
    for _ in range(30):
        h.fail(SIGNED_OUT)
    check("exactly one 'not updating' toast", titles(h) == ["Claude usage is not updating"],
          repr(titles(h)))
    for error in ("Offline", SIGNED_OUT, "Rate limited by the usage API", SIGNED_OUT):
        h.fail(error)
    check("going offline in the middle does not make it a new outage", len(h.sent) == 1,
          repr(titles(h)))
    h.poll(session=(40, S1))
    for _ in range(5):
        h.fail(SIGNED_OUT)
    check("a new outage after recovering gets its own single toast", len(h.sent) == 2,
          repr(titles(h)))

    print("never signed in on this PC")
    h = Harness()
    h.cfg["notifications"]["signed_out_after"] = 0
    h.fail("Not signed in to Claude Code")
    check("says so, instead of claiming a sign-in expired",
          len(h.sent) == 1 and "not signed in" in h.sent[0][1] and "expired" not in h.sent[0][1],
          repr(h.sent))

    print("other errors, for as long as they last")
    h = Harness()
    h.cfg["notifications"]["signed_out_after"] = 0
    for error in ("Offline", "Something went wrong", "Usage API error (HTTP 418)") * 5:
        h.fail(error)
    check("never read as a sign-in problem", h.sent == [], repr(titles(h)))


REAL_GRANT = {
    "cedar_ember": {
        "eligible": True, "ineligible_reason": None, "at_limit": False, "exhausted": [],
        "grants": [{
            "id": "opus55-launch-promax-20260921",
            "label": "Claude Opus 5.5 launch: one usage-limit reset for Pro and Max",
            "resets_total": 1, "resets_left": 1,
            "starts_at": "2026-09-22T16:00:00+00:00", "ends_at": "2099-10-22T16:00:00+00:00",
            "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
            "paused": False, "usable_now": True, "use_requires_limit": False,
        }],
    },
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
}


def with_events(h, events):
    h.usage.events = events
    return h


def test_events():
    print("parsing the grant the endpoint really returned (expiry moved into the future)")
    events = app.parse_events(REAL_GRANT)
    check("one live event", len(events) == 1, repr(events))
    if events:
        e = events[0]
        check("label, count and what it clears survive",
              e["resets_left"] == 1 and "Opus 5.5" in e["label"]
              and app.describe_clears(e["clears"]) == "5-hour and weekly limits", repr(e))

    print("grants that are not live are left out")
    for name, mutate in (("used up", lambda g: g.update(resets_left=0)),
                         ("expired", lambda g: g.update(ends_at="2020-01-01T00:00:00+00:00")),
                         ("paused", lambda g: g.update(paused=True))):
        data = copy.deepcopy(REAL_GRANT)
        mutate(data["cedar_ember"]["grants"][0])
        check("%s -> no event" % name, app.parse_events(data) == [])
    data = copy.deepcopy(REAL_GRANT)
    data["cedar_ember"]["eligible"] = False
    check("ineligible (asked as the wrong surface) -> no event", app.parse_events(data) == [])
    check("a program under a new codename is still found",
          len(app.parse_events({"brand_new_program": REAL_GRANT["cedar_ember"]})) == 1)

    print("a grant with odd fields")
    data = copy.deepcopy(REAL_GRANT)
    data["cedar_ember"]["grants"][0].update(resets_left="one", resets_total={}, clears="five_hour",
                                            label=None, usable_now=None, ends_at="soon")
    try:
        odd = app.parse_events(data)
        check("is read, not fatal", len(odd) == 1 and odd[0]["clears"] == []
              and odd[0]["resets_left"] is None and odd[0]["label"] == "Cedar ember", repr(odd))
    except Exception as exc:
        check("is read, not fatal", False, repr(exc))

    print("a cached grant that has since expired")
    u = app.Usage()
    u.events = [dict(events[0], ends_at="2020-01-01T00:00:00+00:00")] if events else []
    check("is not displayed, even if no poll has refreshed it", app.live_events(u) == [])
    u.events = events
    check("a live one still is", len(app.live_events(u)) == 1)

    print("announcing the event")
    state = tempfile.mkdtemp()
    h = with_events(Harness(state), events)
    h.poll(session=(10, S1))
    h.poll(session=(10, S1))
    check("announced once", len(h.sent) == 1 and "reset" in h.sent[0][0].lower(), repr(h.sent))
    h2 = with_events(Harness(state), events)
    h2.poll(session=(10, S1))
    check("not again after a restart", len(h2.sent) == 0, repr(h2.sent))
    h3 = with_events(Harness(), events)
    h3.cfg["notifications"]["events"] = False
    h3.poll(session=(10, S1))
    check("can be switched off", len(h3.sent) == 0)

    print("a grant appears on the poll that also crosses a threshold")
    h = with_events(Harness(), events)
    h.poll(session=(100, S1, "Session (5h)"))
    check("one toast carrying both, not the grant replacing the limit alert",
          len(h.sent) == 1 and "Session (5h) limit reached" in h.sent[0][1]
          and "Free limit reset available" in h.sent[0][1], repr(h.sent))
    check("and both are recorded", "session" in h.last_notified
          and "grant:" + events[0]["id"] in h.last_notified, repr(sorted(h.last_notified)))

    print("a grant appears while you are busy")
    h = with_events(Harness(busy=True), events)
    for _ in range(3):
        h.poll(session=(10, S1))
    h.busy = False
    h.poll(session=(10, S1))                 # a normal poll, not an event poll
    h.poll(session=(10, S1))
    check("announced on the next poll you can see it, once", len(h.sent) == 1, repr(h.sent))

    print("a grant that cannot be used yet")
    h = with_events(Harness(), [dict(events[0], usable_now=False)])
    h.poll(session=(10, S1))
    check("is not announced as available", h.sent == [], repr(h.sent))
    h.usage.events = events
    h.poll(session=(10, S1))
    check("until it is", len(h.sent) == 1, repr(h.sent))


def test_parsing():
    print("usage pools")
    pools = app.extra_buckets({
        "five_hour": {"utilization": 10}, "seven_day": {"utilization": 20},
        "seven_day_opus": {"utilization": 40}, "seven_day_sonnet": {"utilization": 5},
        "omelette_promotional": {"utilization": 0}, "nimbus_quill": {"utilization": 0.0},
        "broken_pool": {"utilization": "lots"}, "tangelo": None,
    })
    keys = sorted(p["key"] for p in pools)
    check("per-model weekly pools are left to `limits`, garbage is skipped",
          keys == ["nimbus_quill", "omelette_promotional"], repr(keys))

    print("the weekly breakdown")
    rows = app.breakdown_rows({"seven_day_breakdown": {"rows": [
        {"key": "claude_code", "display_name": "Claude Code", "percent": 12.5, "secret": "x"},
        {"key": "chat", "percent": "n/a"}, "junk"]}})
    check("only the shown fields are kept, odd rows skipped",
          rows == [{"key": "claude_code", "display_name": "Claude Code", "percent": 12.5}],
          repr(rows))
    check("a breakdown of the wrong shape is empty, not fatal",
          app.breakdown_rows({"seven_day_breakdown": ["rows"]}) == [])

    print("the Claude Code version, when it has to come from ~/.claude.json")
    home = tempfile.mkdtemp()
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    try:
        os.environ["USERPROFILE"] = os.environ["HOME"] = home
        for seen, want in (("2.1.99", "claude-cli/2.1.99 (external, cli)"),
                           ("2.1.99) evil (", "claude-cli/2.1.0 (external, cli)"),
                           (7, "claude-cli/2.1.0 (external, cli)")):
            with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as fh:
                json.dump({"lastReleaseNotesSeen": seen}, fh)
            got = app.claude_code_user_agent()
            check("%r -> %s" % (seen, want), got == want, got)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _forward(name):
    """A Poller attribute that is really the Claude source's."""
    return property(lambda self: getattr(self.claude, name),
                    lambda self, value: setattr(self.claude, name, value))


class Poller(Harness):
    """The Claude source's poll loop inside the app, with the network stubbed:
    every request it would make is recorded in `fetches` as (events, manual)."""

    pace, pace_at, last_request = _forward("pace"), _forward("pace_at"), _forward("last_request")
    backoff, retry_at, next_poll_at = _forward("backoff"), _forward("retry_at"), _forward("next_poll_at")
    events_retry_at, _event_backoff = _forward("events_retry_at"), _forward("_event_backoff")
    signed_out_since, _pending = _forward("signed_out_since"), _forward("_pending")

    def __init__(self, state_dir=None):
        Harness.__init__(self, state_dir)
        self.fetches = []
        self.claude._fetch = lambda events, manual=False: self.fetches.append((events, manual))

    def event_poll_due(self):
        return self.claude.event_poll_due()

    def base_interval(self):
        return self.claude.base_interval()

    def poll_interval(self):
        return self.claude.poll_interval()

    def first_poll_at(self):
        return self.claude.first_poll_at()

    def on_unlock(self):
        return self.claude.on_unlock()

    def start_fetch(self, manual=False):
        return self.claude.start_fetch(manual)

    def deliver(self, with_events, error=None, status=None, events=(), spend="S", pct=20.0,
                manual=False):
        r = app.Usage()
        r.with_events, r.manual = with_events, manual
        r.error, r.status = error, status
        if not error:
            r.limits = [{"key": "session", "label": "Session (5h)", "percent": pct,
                         "resets_at": stamp(S1), "severity": "normal", "group": "session"}]
            r.updated = datetime.now()
            r.spend = None if with_events else spend
            if with_events and events is not None:
                r.events = list(events)
                r.events_checked = time.time()
        self.claude._pending = r
        self.apply_pending()


def test_event_poll():
    print("folding the event check into the regular poll")
    grant = app.parse_events(REAL_GRANT)
    p = Poller()
    check("the first poll is an event poll", p.event_poll_due())
    p.deliver(False, spend="SPEND")
    p.deliver(True, events=grant)
    check("an event poll sets the events and announces them",
          [e["id"] for e in (p.usage.events or [])] == ["opus55-launch-promax-20260921"]
          and len(p.sent) == 1, repr(p.sent))
    check("and keeps the spend it skipped", p.usage.spend == "SPEND", repr(p.usage.spend))
    check("no event poll again within the hour", not p.event_poll_due())
    p.deliver(False, spend="SPEND2")
    check("a normal poll inherits the events", len(p.usage.events or []) == 1)

    print("an event poll that is rate limited")
    p = Poller()
    p.deliver(True, error="Rate limited by the usage API", status=429)
    check("leaves the answer unknown", p.usage.events is None)
    check("and the next poll is a plain one, so usage keeps updating", not p.event_poll_due())
    p.events_retry_at = time.time() - 1
    check("the event check is tried again once its own backoff is over", p.event_poll_due())
    p.deliver(True, error="Rate limited by the usage API", status=429)
    first = p._event_backoff
    p.deliver(True, error="Rate limited by the usage API", status=429)
    check("and backs off further each time it fails", p._event_backoff > first,
          "%d then %d" % (first, p._event_backoff))

    print("an event poll refused outright (401/403)")
    p = Poller()
    p.deliver(False, pct=33.0)
    p.cfg["notifications"]["signed_out_after"] = 0
    p.deliver(True, error=SIGNED_OUT, status=403)
    check("is not shown as signed out", p.usage.error is None and p.signed_out_since is None
          and p.sent == [], repr(p.usage.error))
    check("the numbers are asked for again right away, the plain way",
          p.fetches == [(False, False)], repr(p.fetches))
    check("and the event check waits", not p.event_poll_due())
    p.deliver(False, error=SIGNED_OUT, status=401)
    check("while a plain 401 still is a real sign-out", p.usage.error == SIGNED_OUT)

    print("an event poll whose grants cannot be read")
    p = Poller()
    p.usage.events = grant
    p.deliver(True, events=None, pct=44.0)
    check("still delivers the numbers", p.usage.error is None
          and p.usage.limits[0]["percent"] == 44.0)
    check("keeps the grants it knew", len(p.usage.events or []) == 1)
    check("and does not retry the event check on every poll", not p.event_poll_due())

    print("choosing the kind of request")

    class Fetcher(Poller):
        def __init__(self):
            Poller.__init__(self)
            del self.claude._fetch               # the real one, with the fake network

    asked = []

    def fake_fetch(events=False):
        asked.append(events)
        r = app.Usage()
        r.with_events = events
        return r

    real_fetch = app.fetch_usage
    app.fetch_usage = fake_fetch
    try:
        def ask(f, **kw):
            f._pending = None
            f.last_request = 0.0                # not a double press of Refresh
            f.start_fetch(**kw)
            for _ in range(200):
                if f._pending is not None:
                    break
                time.sleep(0.01)
            return asked[-1] if asked else None

        f = Fetcher()
        f.usage.events, f.usage.events_checked = grant, time.time()
        check("a timed poll inside the hour is plain", ask(f) is False)
        check("the Refresh button re-checks events", ask(f, manual=True) is True)
        f.cfg["check_events"] = False
        f.usage.events = None
        check("check_events: false - never an event poll, timed",
              ask(f) is False and not f.event_poll_due())
        check("... or manual", ask(f, manual=True) is False)
    finally:
        app.fetch_usage = real_fetch

    p = Poller()
    p.usage.events = grant
    p.cfg["check_events"] = False
    p.deliver(False)
    check("and cached grants are dropped once it is off", p.usage.events is None)


RL = "Rate limited by the usage API"


def test_pacing():
    print("the pace")
    p = Poller()
    check("config.json's 60 s is raised to the 120 s the endpoint sustains",
          p.base_interval() == 120, repr(p.base_interval()))
    p.cfg["refresh_seconds"] = 600
    check("a slower setting is kept", p.base_interval() == 600)

    print("numbers standing still, then moving")
    p = Poller()
    gaps = []
    for _ in range(5):
        p.deliver(False, pct=20.0)
        gaps.append(round(p.next_poll_at - time.time()))
    check("each unchanged poll waits longer, up to five minutes",
          gaps == [120, 180, 270, 300, 300], repr(gaps))
    p.deliver(False, pct=21.0)
    check("the first change snaps back", round(p.next_poll_at - time.time()) == 120)

    print("a 429 on a timed poll")
    p = Poller()
    p.deliver(False, error=RL, status=429)
    check("waits out a backoff", p.retry_at - time.time() > 100 and p.next_poll_at == p.retry_at)
    check("and slows the pace itself", abs(p.base_interval() - 150) < 0.01,
          repr(p.base_interval()))
    p.deliver(False, error=RL, status=429)
    check("further each time", p.base_interval() > 150 and p.backoff == 240,
          "%r %r" % (p.base_interval(), p.backoff))
    learned = p.base_interval()
    p.deliver(False, pct=20.0)
    check("a success ends the backoff but keeps the pace",
          p.retry_at == 0 and abs(p.base_interval() - learned) < 0.01)
    p.pace_at -= 6 * 3600
    check("what it learned fades by a tenth per six hours without another",
          abs(p.base_interval() - learned * 0.9) < 0.5, repr(p.base_interval()))
    p.pace_at -= 30 * 24 * 3600
    check("and is gone in the end", p.base_interval() == 120)

    print("restarting")
    state = tempfile.mkdtemp()
    p = Poller(state)
    p.deliver(False, error=RL, status=429)
    q = Poller(state)
    check("keeps the pace and the backoff", abs(q.base_interval() - 150) < 0.01
          and abs(q.retry_at - p.retry_at) < 0.01, "%r" % q.base_interval())
    q.usage.limits = [{"key": "session", "label": "Session (5h)", "percent": 20.0}]
    q.usage.updated = datetime.now()
    check("and does not poll before the backoff is over", q.first_poll_at() >= p.retry_at - 0.01)
    q = Poller()
    q.usage.limits = [{"key": "session", "label": "Session (5h)", "percent": 20.0}]
    q.usage.updated = datetime.now() - timedelta(seconds=30)
    q.last_request = time.time() - 30
    check("with numbers 30 s old in the cache, the first request waits till they are due",
          85 < q.first_poll_at() - time.time() <= 91, repr(q.first_poll_at() - time.time()))
    q.usage.limits = []
    check("with nothing cached it asks within a minute of the last run's request",
          25 < q.first_poll_at() - time.time() <= 31)

    print("the Refresh button")
    p = Poller()
    p.deliver(False, error=RL, status=429, manual=True)
    check("into a rate limit: waits, but does not slow the pace",
          p.base_interval() == 120 and p.retry_at > time.time() + 100)
    p = Poller()
    p.last_request = time.time() - 5
    p.start_fetch(manual=True)
    check("pressed right after a request: asks nothing", p.fetches == [])
    p.last_request = time.time() - 20
    p.start_fetch(manual=True)
    check("otherwise asks, and re-checks events", p.fetches == [(True, True)], repr(p.fetches))

    print("the timer")
    p = Poller()
    p.next_poll_at = time.time() + 60
    p.maybe_poll()
    check("does nothing before a poll is due", p.fetches == [])
    p.next_poll_at = time.time() - 1
    p.locked = True
    p.maybe_poll()
    check("nor while the screen is locked", p.fetches == [])
    p.locked = False
    p.maybe_poll()
    check("and polls once due and unlocked", len(p.fetches) == 1)

    print("unlocking")
    p = Poller()
    p.last_request, p.next_poll_at = time.time() - 600, time.time() + 250
    p.on_unlock()
    check("fresh numbers within seconds", p.next_poll_at - time.time() <= 3.5)
    p.last_request, p.next_poll_at = time.time() - 30, time.time() + 250
    p.on_unlock()
    check("but not on top of a request a moment ago", 85 < p.next_poll_at - time.time() <= 91)

    print("what a rate limit looks like")
    u = app.Usage()
    u.limits = [{"key": "session", "label": "Session (5h)", "percent": 20.0}]
    u.updated, u.error, u.status = datetime.now(), RL, 429
    check("numbers minutes old: no error to show", app.shown_error(u) is None)
    u.updated = datetime.now() - timedelta(minutes=20)
    check("numbers 20 minutes old: say why", app.shown_error(u) == RL)
    u.updated, u.error, u.status = datetime.now(), "Offline", None
    check("other errors always show", app.shown_error(u) == "Offline")


class Endpoint(object):
    """A token bucket fitted to the log: about one request per 100 s sustained,
    a burst of a few, 429 when empty."""

    def __init__(self, capacity=5.0, per_second=1 / 100.0):
        self.capacity, self.rate = capacity, per_second
        self.tokens, self.t = capacity, 0.0

    def take(self, now):
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def test_a_day():
    print("a simulated day: the endpoint allows ~1 request per 100 s, and Claude Code")
    print("uses the same allowance every 5 minutes during 8 working hours")
    day, step = 24 * 3600, 5

    def working(t):
        return 8 * 3600 <= t < 16 * 3600

    def pct_at(t):                   # usage climbs while you work, stands still otherwise
        return min(100.0, max(0.0, (min(t, 16 * 3600) - 8 * 3600) / 600.0))

    # The old way: every 60 s, and 120 s (doubling) after a 429.
    ep, old_refused, old_requests, next_at, backoff = Endpoint(), 0, 0, 0, 0
    for t in range(0, day, step):
        if working(t) and t % 300 == 0:
            ep.take(t)
        if t >= next_at:
            old_requests += 1
            if ep.take(t):
                backoff, next_at = 0, t + 60
            else:
                old_refused += 1
                backoff = min(max(backoff * 2, 120), 1800)
                next_at = t + backoff

    # The new way, through the app's own poll loop on a fake clock.
    ep, clock = Endpoint(), [0.0]
    sim = Poller()
    sim.requests, sim.refused, sim.successes = 0, 0, []

    def fake_fetch(events, manual=False):
        now = clock[0]
        sim.last_request = now
        sim.requests += 1
        r = app.Usage()
        r.with_events, r.manual = events, manual
        if ep.take(now):
            sim.successes.append(now)
            r.limits = [{"key": "session", "label": "Session (5h)", "percent": pct_at(now),
                         "resets_at": None, "severity": "normal", "group": "session"}]
            r.updated = datetime.now()
            if events:
                r.events, r.events_checked = [], now
        else:
            sim.refused += 1
            r.error, r.status = RL, 429
        sim._pending = r

    sim.claude._fetch = fake_fetch
    sim.next_poll_at = sim.last_request = 0.0          # the fake clock starts at 0
    real_time = app.time
    app.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        for t in range(0, day, step):
            clock[0] = float(t)
            if working(t) and t % 300 == 0:
                ep.take(t)
            sim.maybe_poll()
            if sim._pending is not None:
                sim.apply_pending()
    finally:
        app.time = real_time
    gaps = [b - a for a, b in zip(sim.successes, sim.successes[1:])]
    print("    old: %d requests, %d refused" % (old_requests, old_refused))
    print("    new: %d requests, %d refused, longest wait for fresh numbers %ds"
          % (sim.requests, sim.refused, max(gaps or [0])))
    check("the simulation really polled all day", sim.requests > 200, "%d requests" % sim.requests)
    check("the old pacing really did run into the limit all day", old_refused > 100,
          "%d refused" % old_refused)
    check("the new pacing is refused a handful of times a day at most", sim.refused <= 5,
          "%d refused" % sim.refused)
    check("and never leaves the numbers more than 10 minutes old", max(gaps or [0]) <= 600)


def test_flyout_key():
    print("the flyout's change detection")
    h = Harness(providers=("claude", "ollama"))
    h.usage.limits = [{"key": "session", "label": "Session (5h)", "percent": 20.0,
                       "resets_at": None}]
    h.usage.updated = datetime.now()
    a = app.Flyout.content_key(h.active())
    check("the same data gives the same key", a == app.Flyout.content_key(h.active()))
    h.usage.events = app.parse_events(REAL_GRANT)
    b = app.Flyout.content_key(h.active())
    check("a grant appearing changes it", a != b)
    h.ollama.usage.limits = [{"key": "monthly", "label": "Monthly credits", "percent": 5.0,
                              "resets_at": None}]
    check("so does the other provider's numbers", b != app.Flyout.content_key(h.active()))


# --- Ollama -------------------------------------------------------------------------

RFC_8032 = (   # (seed, public key, message, signature) - RFC 8032 section 7.1, tests 1 and 2
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
)


def openssh_key(seed, cipher=b"none"):
    """An OpenSSH private key file for `seed`, laid out the way ssh-keygen
    (and so `ollama`) writes one."""
    def string(b):
        return struct.pack(">I", len(b)) + b
    pub = app.ed25519_public(seed)
    pub_blob = string(b"ssh-ed25519") + string(pub)
    private = (struct.pack(">II", 0x5EED, 0x5EED) + string(b"ssh-ed25519") + string(pub)
               + string(seed + pub) + string(b"friend@pc"))
    private += bytes(range(1, 1 + (-len(private)) % 8))
    raw = (b"openssh-key-v1\x00" + string(cipher) + string(b"none" if cipher == b"none" else b"bcrypt")
           + string(b"") + struct.pack(">I", 1) + string(pub_blob) + string(private))
    body = base64.b64encode(raw).decode()
    lines = [body[i:i + 70] for i in range(0, len(body), 70)]
    return ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(lines)
            + "\n-----END OPENSSH PRIVATE KEY-----\n"), pub_blob


class FakeResponse(object):
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_ollama():
    print("signing like the ollama CLI")
    for seed, pub, msg, sig in RFC_8032:
        s_ = bytes.fromhex(seed)
        check("Ed25519 matches RFC 8032 for seed %s..." % seed[:8],
              app.ed25519_public(s_).hex() == pub
              and app.ed25519_sign(s_, bytes.fromhex(msg)).hex() == sig)
    seed = bytes.fromhex(RFC_8032[0][0])
    text, pub_blob = openssh_key(seed)
    got_seed, got_blob = app.read_openssh_ed25519(text)
    check("the private key file is read back to its seed and public key",
          got_seed == seed and got_blob == pub_blob)
    header = app.ollama_signature(text, "GET", "/api/usage?ts=1790000000")
    want = "%s:%s" % (base64.b64encode(pub_blob).decode(), base64.b64encode(
        app.ed25519_sign(seed, b"GET,/api/usage?ts=1790000000")).decode())
    check("the header is <public key>:<signature of METHOD,URI>", header == want)
    try:
        app.read_openssh_ed25519(openssh_key(seed, cipher=b"aes256-ctr")[0])
        check("a passphrase-protected key is refused", False)
    except ValueError:
        check("a passphrase-protected key is refused", True)

    print("reading /api/usage")
    tz = datetime(2026, 9, 23, 12, 0).astimezone().tzinfo
    now = datetime(2026, 9, 23, 12, 0, tzinfo=tz)
    limits, spend = app.parse_ollama_usage(
        {"limits": {"monthly": {"usage": 0.053, "models": [{"name": "glm", "request_count": 9}]}},
         "activity": {"cost": "0.00000", "period": {"type": "last_4_weeks"}}}, None, now)
    check("a monthly plan: one meter, as a percentage",
          [(l["key"], l["label"], round(l["percent"], 1)) for l in limits]
          == [("monthly", "Monthly credits", 5.3)], repr(limits))
    check("no reset date without reset_day, and no spend when it is $0",
          limits[0]["resets_at"] is None and spend is None)
    limits, spend = app.parse_ollama_usage(
        {"limits": {"weekly": {"usage": 0.316}, "session": {"usage": "0.349"},
                    "daily": {"usage": 0.5}, "broken": {"usage": "lots"}},
         "activity": {"cost": "1.68054"}}, None, now)
    check("an older plan: session and weekly, in that order, plus anything new",
          [l["key"] for l in limits] == ["session", "weekly", "daily"], repr(limits))
    check("pay-as-you-go spend is kept", spend == {"cost": 1.68054}, repr(spend))
    try:
        app.parse_ollama_usage({"error": "nope"})
        check("a response without limits is an error, not an empty meter", False)
    except ValueError:
        check("a response without limits is an error, not an empty meter", True)

    print("when the monthly credits renew")
    for day, at, want in ((14, now, "2026-10-14"), (23, now, "2026-10-23"), (24, now, "2026-09-24"),
                          (31, datetime(2026, 2, 3, tzinfo=tz), "2026-02-28"),
                          (5, datetime(2026, 12, 20, tzinfo=tz), "2027-01-05")):
        got = app.next_monthly_reset(day, at)
        check("reset_day %d on %s -> %s" % (day, at.date(), want),
              bool(got) and got.startswith(want), repr(got))
    check("no reset_day, no date", app.next_monthly_reset(None) is None
          and app.next_monthly_reset("x") is None and app.next_monthly_reset(40) is None)

    print("asking ollama.com")
    home = tempfile.mkdtemp()
    key_path = os.path.join(home, "id_ed25519")
    with open(key_path, "w", encoding="ascii") as fh:
        fh.write(text)
    calls, answer = [], [FakeResponse(200, {"limits": {"monthly": {"usage": 0.25}}})]

    def fake_get(url, headers=None, timeout=None, params=None):
        calls.append((url, dict(headers or {})))
        return answer[0]

    saved = (app.requests.get, app.OLLAMA_KEY_PATH, os.environ.pop("OLLAMA_API_KEY", None))
    app.requests.get, app.OLLAMA_KEY_PATH = fake_get, key_path
    try:
        u = app.fetch_ollama_usage({})
        url, headers = calls[-1]
        uri = url[len(app.OLLAMA_URL):]
        check("without an API key the request is signed with the ollama signin key",
              uri.startswith("/api/usage?ts=")
              and headers.get("Authorization") == app.ollama_signature(text, "GET", uri), url)
        check("and the answer becomes a 25% monthly meter",
              u.error is None and [round(l["percent"]) for l in u.limits] == [25])
        u = app.fetch_ollama_usage({"api_key": " secret "})
        url, headers = calls[-1]
        check("an API key is sent as a Bearer token instead",
              url == app.OLLAMA_URL + "/api/usage" and headers.get("Authorization") == "Bearer secret")
        answer[0] = FakeResponse(401, {"error": "invalid credentials"})
        check("401 without a key: signed out", app.fetch_ollama_usage({}).error
              == "Signed out - run ollama signin")
        check("401 with a key: the key", app.fetch_ollama_usage({"api_key": "k"}).error
              == "Ollama API key refused")
        answer[0] = FakeResponse(429, {})
        check("429 is a rate limit", app.fetch_ollama_usage({}).status == 429)
        app.OLLAMA_KEY_PATH = os.path.join(home, "missing")
        check("no key file and no API key: not signed in",
              app.fetch_ollama_usage({}).error == "Not signed in to Ollama")
    finally:
        app.requests.get, app.OLLAMA_KEY_PATH = saved[0], saved[1]
        if saved[2] is not None:
            os.environ["OLLAMA_API_KEY"] = saved[2]

    print("Ollama in the app")
    h = Harness(providers=("claude", "ollama"), metrics=("session",))
    check("its own, gentler pace", h.ollama.base_interval() == 300)
    check("its own files", h.ollama.cache_path.endswith(".usage_cache.ollama.json")
          and h.claude.cache_path.endswith(".usage_cache.json"))
    h.set(session=(30, S1, "Session (5h)"))
    h.ollama.usage.limits = [{"key": "monthly", "label": "Monthly credits", "percent": 81.0,
                              "resets_at": None}]
    h.check_notifications()
    check("its limits are announced under its own name",
          titles(h) == ["Ollama usage 81%"] and "Ollama monthly at 81%" in h.sent[0][1],
          repr(h.sent))
    h.set(session=(100, S1, "Session (5h)"))
    h.ollama.usage.limits[0]["percent"] = 100.0
    h.check_notifications()
    check("both running out on one poll share a toast",
          len(h.sent) == 2 and "Session (5h) limit reached" in h.sent[1][1]
          and "Ollama monthly limit reached" in h.sent[1][1], repr(h.sent[1:]))
    check("kept apart in the state", "session" in h.last_notified
          and "ollama:monthly" in h.last_notified, repr(sorted(h.last_notified)))
    h.cfg["notifications"]["signed_out_after"] = 0
    h.ollama.usage.error = "Signed out - run ollama signin"
    h.check_notifications()
    check("its sign-in trouble gets its own nudge",
          titles(h)[-1] == "Ollama usage is not updating" and "ollama signin" in h.sent[-1][1],
          repr(h.sent[-1:]))

    print("switching providers")
    check("by default only Claude", app.enabled_providers(app.DEFAULT_CONFIG) == ["claude"])
    check("never none", app.enabled_providers({"providers": {"claude": {"enabled": False}}})
          == ["claude"])
    with open(app.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump({"refresh_seconds": 60, "providers": {"claude": {"enabled": True}}}, fh)
    h = Harness()
    h.cfg = app.load_config()
    h.set_provider("ollama", True)
    check("switching Ollama on saves it and starts it",
          [s_.key for s_ in h.active()] == ["claude", "ollama"]
          and json.load(open(app.CONFIG_PATH, encoding="utf-8"))["providers"]["ollama"]["enabled"])
    h.set_provider("claude", False)
    check("Claude can be switched off", [s_.key for s_ in h.active()] == ["ollama"])
    h.set_provider("ollama", False)
    check("but not the last one", [s_.key for s_ in h.active()] == ["ollama"])
    saved_cfg = json.load(open(app.CONFIG_PATH, encoding="utf-8"))
    check("and the rest of config.json is left alone", saved_cfg.get("refresh_seconds") == 60)


def test_window_procedures():
    print("clicks and menu commands arriving in a window procedure")

    class Clicks(object):
        _wndproc = app.TrayApp._wndproc
        defer = app.TrayApp.defer
        run_deferred = app.TrayApp.run_deferred
        request_menu = app.TrayApp.request_menu

        def __init__(self):
            self._deferred, self.ran = [], []
            self._menu_state = None
            self.wm_taskbar_created = 0xC0DE
            self.cfg = {"left_click": "flyout"}

        def on_command(self, cmd):
            self.ran.append(("command", cmd))

        def show_menu(self):
            self.ran.append(("menu",))
            self._menu_state = None

        def left_click(self):
            self.ran.append(("left",))

    c = Clicks()
    c._wndproc(None, app.WM_COMMAND, app.CMD_PROVIDER + 1, 0)
    c._wndproc(None, app.WM_TRAY, 0, app.WM_LBUTTONUP)
    c._wndproc(None, app.WM_TRAY, 0, app.WM_RBUTTONUP)
    widget = object.__new__(app.TaskbarWidget)
    widget.app = c
    widget._on_message(app.WM_LBUTTONUP, 0, 0)
    widget._on_message(app.WM_RBUTTONUP, 0, 0)
    check("nothing runs inside the window procedure, where calling Tk aborts the process",
          c.ran == [] and len(c._deferred) == 4, repr(c.ran))
    c.run_deferred()
    check("the pump then runs them in order - two right clicks, one menu",
          c.ran == [("command", app.CMD_PROVIDER + 1), ("left",), ("menu",), ("left",)],
          repr(c.ran))
    for _ in range(3):
        c._wndproc(None, app.WM_TRAY, 0, app.WM_RBUTTONUP)
    c._menu_state = "open"
    widget._on_message(app.WM_RBUTTONUP, 0, 0)
    c._menu_state = None
    c.run_deferred()
    check("however many right clicks arrive while one is queued or open",
          c.ran[4:] == [("menu",)], repr(c.ran[4:]))


def test_rename():
    print("config.json from before providers had their own settings")
    for user, want in (
            ({"refresh_seconds": 60, "check_events": False, "usage_page_url": "https://x"},
             (60, False, "https://x")),
            ({"refresh_seconds": 60, "providers": {"claude": {"refresh_seconds": 300}}},
             (300, True, "https://claude.ai/settings/usage")),
            ({}, (120, True, "https://claude.ai/settings/usage"))):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(user, fh)
        c = app.load_config()["providers"]["claude"]
        got = (c.get("refresh_seconds"), c.get("check_events"), c.get("usage_page_url"))
        check("%s -> %r" % (json.dumps(user)[:60], want), got == want, repr(got))
    check("the defaults keep no Claude settings at the top level",
          not any(k in app.DEFAULT_CONFIG for k in ("refresh_seconds", "check_events",
                                                     "usage_page_url")))
    h = Harness()
    h.cfg = {"refresh_seconds": 600, "check_events": False, "providers": {"claude": {}}}
    check("and a config built by hand with the old keys still works",
          h.claude.base_interval() == 600 and not h.claude.events_enabled())

    print("an install from when it was called Claude Usage Bar")
    data, local = tempfile.mkdtemp(), tempfile.mkdtemp()
    saved = {k: getattr(app, k) for k in ("DATA_DIR", "LOG_PATH", "CRASH_LOG", "LOCAL")}
    try:
        app.DATA_DIR, app.LOCAL = data, local
        app.LOG_PATH = os.path.join(data, "llm_usage_bar.log")
        app.CRASH_LOG = os.path.join(data, "llm_usage_bar.crash.log")
        with open(os.path.join(data, "claude_usage_bar.log"), "w") as fh:
            fh.write("old history\n")
        open(app.LOG_PATH, "w").close()                 # an empty one, as a first run makes
        os.makedirs(os.path.join(local, "claude-usage-bar"))
        with open(os.path.join(local, "claude-usage-bar", "config.json"), "w") as fh:
            fh.write("{}")
        os.makedirs(os.path.join(local, "llm-usage-bar"))  # created empty on import
        moved = app.migrate_legacy_files()
        check("the log keeps its history under the new name",
              open(app.LOG_PATH).read() == "old history\n"
              and not os.path.exists(os.path.join(data, "claude_usage_bar.log")), repr(moved))
        check("the per-user folder is carried over",
              os.path.exists(os.path.join(local, "llm-usage-bar", "config.json"))
              and not os.path.exists(os.path.join(local, "claude-usage-bar")))
        with open(os.path.join(data, "claude_usage_bar.log"), "w") as fh:
            fh.write("an old copy still writing\n")
        app.migrate_legacy_files()
        check("but never over a log the new name already has",
              open(app.LOG_PATH).read() == "old history\n")
    finally:
        for k, v in saved.items():
            setattr(app, k, v)


def leftover_tmp(folder):
    return [name for name in os.listdir(folder) if name.endswith(".tmp")]


def test_state_files():
    print("the state files are replaced whole, with the bytes they always had")
    d = tempfile.mkdtemp()
    u = app.Usage()
    u.limits = [{"key": "session", "label": "Session (5h)", "percent": 41.0,
                 "resets_at": stamp(S1), "severity": "normal", "group": "session"}]
    u.updated = datetime.now()
    u.events, u.events_checked = [], 1234.5
    cache = os.path.join(d, ".usage_cache.json")
    app.save_usage_cache(u, cache)
    blob = {"limits": u.limits, "extra": None, "spend": None, "breakdown": [], "buckets": [],
            "events": [], "events_checked": 1234.5, "updated": u.updated.isoformat()}
    old = os.path.join(d, "as-before.json")
    with open(old, "w", encoding="utf-8") as fh:      # how every state file used to be saved
        json.dump(blob, fh)
    with open(cache, "rb") as fh, open(old, "rb") as before:
        got, want = fh.read(), before.read()
    check("the usage cache is byte for byte what json.dump wrote before",
          got == want == json.dumps(blob).encode("utf-8"))

    notify = os.path.join(d, ".notify_state.json")
    state = {"session": ("2026-09-23T12:10", 80.0), "grant:abc": ("", 1)}
    app.save_notify_state(notify, state)
    with open(notify, "rb") as fh:
        got = fh.read()
    check("so is the notification state",
          got == json.dumps({k: [v[0], v[1]] for k, v in state.items()}).encode("utf-8"))
    check("which reads back as it was saved", app.load_notify_state(notify) == state,
          repr(app.load_notify_state(notify)))

    poll = os.path.join(d, ".poll_state.json")
    five = {"pace": 150.0, "pace_at": 10.0, "last_request": 20.0, "retry_at": 0.0, "backoff": 0.0}
    app.save_poll_state(poll, dict(five))
    with open(poll, "rb") as fh:
        got = fh.read()
    check("and the poll state", got == json.dumps(five).encode("utf-8"))
    app.save_poll_state(poll, dict(five, next_poll_at=99.0, in_flight=True, error="Offline",
                                   status=429, pid=4242, updated_at=30.0))
    check("a v2.0.1 reading the newer poll state takes only the five numbers it knows",
          app.load_poll_state(poll) == five, repr(app.load_poll_state(poll)))
    check("and indent and the like still reach json.dumps",
          app.write_json_atomic(old, {"a": [1]}, indent=2)
          and open(old, encoding="utf-8").read() == json.dumps({"a": [1]}, indent=2))
    check("no temporary file is left behind", leftover_tmp(d) == [], repr(leftover_tmp(d)))

    print("a reader holding the file open")
    probe = os.path.join(d, "probe.tmp")
    with open(cache, "r", encoding="utf-8") as reader:
        reader.read(10)
        with open(probe, "w") as fh:
            fh.write("{}")
        try:
            os.replace(probe, cache)
            refused = False
        except OSError:
            refused = True
        os.remove(probe)
        saved = app.write_json_atomic(cache, {"limits": []})
    with open(cache, encoding="utf-8") as fh:
        text = fh.read()
    check("Windows does refuse a replace while the file is open (what the fallback is for)",
          refused)
    check("the save still lands - written in place, as it always was",
          saved and text == '{"limits": []}', text[:60])
    check("and the temporary file is gone anyway", leftover_tmp(d) == [], repr(leftover_tmp(d)))

    print("nowhere to write")
    missing = os.path.join(d, "no such folder", "state.json")
    try:
        results = [app.write_json_atomic(missing, {"a": 1})]
        app.save_usage_cache(u, missing)
        app.save_notify_state(missing, state)
        app.save_poll_state(missing, five)
        raised = None
    except Exception as exc:
        results, raised = [], exc
    check("a missing folder is a False, not an exception", results == [False] and raised is None,
          repr(raised))
    before = open(poll, "rb").read()
    check("nor is anything unserialisable, and the old file stays whole",
          app.write_json_atomic(poll, {"bad": object()}) is False
          and open(poll, "rb").read() == before)

    with open(os.path.join(os.path.dirname(HERE), ".gitignore"), encoding="utf-8") as fh:
        ignored = [line.strip() for line in fh]
    check("git ignores a temporary file a crash leaves behind", "*.tmp" in ignored)


def main():
    for test in (test_spam, test_coverage, test_delivery, test_events, test_parsing,
                 test_event_poll, test_pacing, test_a_day, test_flyout_key, test_ollama,
                 test_window_procedures, test_rename, test_state_files):
        test()
    print()
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
