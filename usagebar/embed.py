"""Running the usage bar inside another app - a "host" such as Ultimate Widget.

A host gets the usage bar's own pieces, not copies of them: the provider
Sources with their pacing and backoff, TrayApp's notification rules, the
readout renderer, the Flyout and the Toast. app.py never imports this module,
so the standalone runs exactly as it always has.

The contract is HOST_API. It is a literal, so a host can read it with `ast`
from this file before importing anything, and pick a copy it understands: the
major number changes when something here stops working the old way, the minor
when something is added. What a host talks to in a standalone that is already
running - the CMD_* numbers (the same since 1.0), the window classes
(LLMUsageBarWnd from 2.0, ClaudeUsageBarWnd before), the mutex names - never
changes without a major bump, because a host has to work with every version
already out there.

One poller. The standalone always wins: while one runs (any version, found by
its mutex), the feed follows it - it mirrors the standalone's cache and poll
state from disk and never sends a request of its own. With none running it
leads, and polls and notifies exactly as the standalone would. Both read and
write the same files (see configure), so the learned pace, a rate limit and
what was already announced carry over whichever way the lead passes.

Everything here runs on the host's UI thread, as the standalone's own code
does: the only I/O is stat calls and small local reads and writes of the
usage bar's own state files.
"""

# The dataclasses below annotate with `X | None`, which Python evaluates when
# the class is made and only 3.10 understands. Kept as text, this module and
# the tests that import it still load on 3.8 and 3.9, as v2.0.1 did.
from __future__ import annotations

import copy
import ctypes
import ctypes.wintypes as wt
import dataclasses
import json
import os
import time
import traceback
import types
from dataclasses import dataclass

from . import flyout, paths, tkui, toast
from .app import TrayApp
from .config import load_config
from .deps import Image
from .providers import SOURCES, enabled_providers
from .providers.claude import live_events
from .usage import Usage, load_notify_state, load_usage_cache, shown_error
from .util import deep_merge, log
from .widget import TaskbarWidget, readout_key, readout_rows


HOST_API = (1, 0)

FLUENT = tkui.FLUENT            # the Fluent palette, for a host's own panels
Toast = toast.Toast             # Toast(owner): owner needs panel_corner() and toggle_flyout()
Flyout = flyout.Flyout          # what UsageFeed.toggle_flyout opens

# The 2.0+ names first, then 1.x's (before the rename), which a host must still
# find: dropping them leaves a 1.x standalone found by its mutex alone, with no
# window to ask.
STANDALONE_MUTEXES = ("Local\\LLMUsageBarMutex", "Local\\ClaudeUsageBarMutex")
STANDALONE_WINDOWS = ("LLMUsageBarWnd", "ClaudeUsageBarWnd")
STANDALONE_OVERLAYS = ("LLMUsageOverlayWnd", "ClaudeUsageOverlayWnd")
# Frozen: every version since v1.0.0 answers these in WM_COMMAND (app.CMD_*).
COMMANDS = {"details": 1, "refresh": 2, "quit": 8}

# The files configure() moves into the data folder - every path in `paths`
# but the app's own folder, LOCALAPPDATA and the fallback log. Their names
# stay the standalone's, so the two share them.
_DATA_FILES = {name: os.path.basename(getattr(paths, name))
               for name in ("CONFIG_PATH", "LOG_PATH", "CRASH_LOG", "ICON_CACHE",
                            "USAGE_CACHE", "NOTIFY_STATE", "POLL_STATE")}


def configure(data_dir, log_path=None):
    """Point the usage bar at `data_dir` - normally the standalone's own folder,
    so config, cache, poll state and notification state are shared. Call it
    once, before the first UsageFeed. The legacy-name migration is left to the
    standalone. Returns the folder."""
    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    paths.DATA_DIR = data_dir
    for name, filename in _DATA_FILES.items():
        setattr(paths, name, os.path.join(data_dir, filename))
    if log_path:
        paths.LOG_PATH = log_path
    return data_dir


def attach_tk(root):
    """The host's Tk root, which the flyout and toasts open from."""
    tkui.root = root


# ---------------------------------------------------------------------------
# The standalone: finding it, and asking it things
# ---------------------------------------------------------------------------

# Private handles with their own argtypes, so nothing here changes how
# win32.py's shared ones behave.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenMutexW.restype = wt.HANDLE
_kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_kernel32.CloseHandle.restype = wt.BOOL
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_user32.FindWindowW.restype = wt.HWND
_user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
_user32.FindWindowExW.restype = wt.HWND
_user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.IsWindowVisible.restype = wt.BOOL
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.GetWindowRect.restype = wt.BOOL
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.PostMessageW.restype = wt.BOOL
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

SYNCHRONIZE = 0x00100000
ERROR_ACCESS_DENIED = 5
WM_COMMAND = 0x0111


@dataclass(frozen=True)
class Standalone:
    """A running standalone. pid and hwnd 0 with window_class "" means only its
    mutex exists yet: it is starting up."""
    pid: int
    hwnd: int
    window_class: str
    overlay_hwnd: int
    overlay_visible: bool
    overlay_rect: tuple | None = None       # (left, top, right, bottom), screen pixels


def _mutex_exists(name):
    """Open, never create: creating the standalone's mutex would keep it from
    starting. Access denied still means somebody holds it."""
    ctypes.set_last_error(0)
    handle = _kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def find_standalone(mutexes=STANDALONE_MUTEXES, windows=STANDALONE_WINDOWS,
                    overlays=STANDALONE_OVERLAYS):
    """The standalone if one is running, else None. The mutex decides; the
    windows only add detail. Nothing here sends a message, so it is safe to
    call from the UI thread every couple of seconds."""
    if not any(_mutex_exists(name) for name in mutexes):
        return None
    pid, hwnd, window_class = 0, 0, ""
    for name in windows:
        found = int(_user32.FindWindowW(name, None) or 0)
        if found:
            owner = wt.DWORD()
            _user32.GetWindowThreadProcessId(found, ctypes.byref(owner))
            pid, hwnd, window_class = int(owner.value), found, name
            break
    overlay, visible, rect = 0, False, None
    taskbar = _user32.FindWindowW("Shell_TrayWnd", None)
    if taskbar:
        for name in overlays:
            found = int(_user32.FindWindowExW(taskbar, None, name, None) or 0)
            if found:
                overlay, visible = found, bool(_user32.IsWindowVisible(found))
                r = wt.RECT()
                if _user32.GetWindowRect(found, ctypes.byref(r)):
                    rect = (r.left, r.top, r.right, r.bottom)
                break
    return Standalone(pid, hwnd, window_class, overlay, visible, rect)


def standalone_running(**kw):
    return find_standalone(**kw) is not None


def _post(hwnd, msg, wparam, lparam):
    return bool(_user32.PostMessageW(hwnd, msg, wparam, lparam))


def ask_standalone(command, windows=STANDALONE_WINDOWS, poster=None):
    """Post WM_COMMAND `command` ("details", "refresh" or "quit") to every
    standalone window. Posted, never sent: a standalone that is stuck cannot
    stall the caller. True if any window took it."""
    code = COMMANDS[command]
    post = poster or _post
    sent = False
    for name in windows:
        hwnd = None
        for _ in range(8):
            hwnd = _user32.FindWindowExW(None, hwnd, name, None)
            if not hwnd:
                break
            if post(int(hwnd), WM_COMMAND, code, 0):
                sent = True
    return sent


# ---------------------------------------------------------------------------
# The readout
# ---------------------------------------------------------------------------

def render_readout(width, height, rows, event, cfg, light, veil=False):
    """The standalone's readout, pixel for pixel, as a width x height RGBA
    image: `rows` and `event` as UsageFeed.readout() gives them, `cfg` the
    whole config. `light` picks the theme (None follows Windows). Without the
    veil the background is fully transparent, for a host that lays one veil
    over its whole window."""
    painter = object.__new__(TaskbarWidget)
    painter.app = types.SimpleNamespace(cfg=cfg)
    painter.light, painter.veil = light, veil
    if not rows:
        return painter._finish(Image.new("RGBA", (width, height), (0, 0, 0, 0)), width, height)
    return painter.compose(width, height, rows, event)


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageSummary:
    """The feed at a glance, for a host's own menus and attention cues."""
    role: str
    standalone_pid: int | None
    standalone_overlay: bool            # its overlay is on the taskbar now
    max_pct: float | None               # the fullest limit of any provider
    max_label: str | None
    signed_out: bool
    grant_live: bool
    error: str | None
    updated: object                     # datetime of the newest numbers, or None
    next_poll_in: float | None          # seconds; leader only


class _App(object):
    """The `app` that usage-bar code expects - Source, Flyout, Toast and the
    TrayApp methods borrowed below - with the feed behind it.

    The notification rules are TrayApp's own methods, borrowed as
    tests/test_usage_bar.py's Harness borrows them, so there is one copy of
    them and the host announces exactly what the standalone would."""

    check_notifications = TrayApp.check_notifications
    _check_signed_out = TrayApp._check_signed_out
    _grant_alerts = TrayApp._grant_alerts
    _notify_limit = TrayApp._notify_limit
    sync_sources = TrayApp.sync_sources
    active = TrayApp.active
    usage_page = TrayApp.usage_page
    set_provider = TrayApp.set_provider

    def __init__(self, feed, corner, overrides):
        self.feed = feed
        self.corner = corner
        self.overrides = dict(overrides or {})
        self.cfg = self.read_config()
        self.sources = {}
        self.locked = False
        self.flyout = None
        self.toast = None
        self.state_path = paths.NOTIFY_STATE
        self.last_notified = load_notify_state(self.state_path)

    def read_config(self):
        # load_config can hand back DEFAULT_CONFIG itself, and deep_merge
        # shares what it does not override: a copy of our own, then, so that
        # the host's flyout settings never leak into anything else.
        cfg = copy.deepcopy(load_config())
        cfg["flyout"] = deep_merge(cfg.get("flyout") or {}, self.overrides)
        return cfg

    def start_fetch(self, manual=False):
        self.feed._start_fetch(manual)

    def notify(self, title, body):
        """True only when shown: anything else is tried again next poll."""
        if self.feed._notify is None:
            return False
        return bool(self.feed._notify(title, body))

    def update_icon(self):
        self.feed._dirty = True

    def reload_config(self):
        self.feed.reload_config(force=True)

    def panel_corner(self):
        return self.corner

    def toggle_flyout(self):
        self.feed.toggle_flyout()


def _signature(path):
    """What changes when a file is rewritten, for the price of a stat."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _wait_words(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return "%dm" % minutes
    return "%dh %dm" % divmod(minutes, 60)


class UsageFeed(object):
    """The usage bar's numbers, notifications and flyout, for a host.

    Roles:
    - starting: the first `startup_grace` seconds. Mirrors the files and never
      fetches - a standalone started at logon with the host gets that long to
      take its mutex.
    - leader: no standalone. Polls and notifies, as the standalone would.
    - follower: a standalone runs. Mirrors its files every second; asks it to
      refresh when told to (WM_COMMAND, at most every 15 s); never fetches and
      never notifies - the standalone does both.

    A standalone found by a probe makes the feed a follower at once. A leader
    looks on every tick and again before a Refresh, so no request or
    announcement goes out on a probe that a standalone started since has made
    out of date. It leads again only after three probes in a row (6 s) find
    none, so a standalone restarting does not hand the lead over; then it
    rebuilds its sources from disk, so the standalone's last request, rate
    limit and announcements count. mode="follow" never leads.

    tick() does all of it, on the host's UI thread, about once a second.
    """

    STARTING, LEADER, FOLLOWER = "starting", "leader", "follower"

    PROBE_SECONDS = 2.0         # how often to look for a standalone
    MIRROR_SECONDS = 1.0        # how often a follower stats the files
    POLL_SECONDS = 5.0          # the standalone's own poll timer
    CONFIG_SECONDS = 2.0        # and its config.json watch
    LEAD_AFTER = 3              # probes in a row without a standalone
    TORN_READS = 3              # unreadable reads of a file before believing it
    ASK_SECONDS = 15.0          # a follower asks the standalone to refresh this often at most
    REOPEN_GUARD = 0.3          # see toggle_flyout

    def __init__(self, notify=None, on_change=None, mode="auto", panel_corner="left",
                 flyout_overrides=None, startup_grace=15.0, probe=find_standalone,
                 poster=None, clock=time.time):
        self._notify = notify
        self._on_change = on_change
        self.mode = "follow" if mode == "follow" else "auto"
        self._probe = probe
        self._poster = poster
        self._clock = clock
        now = clock()
        self.role = self.STARTING
        self.standalone = None
        self._grace_until = now + float(startup_grace)
        self._negatives = 0
        self._vanished_at = None
        self._closed = False
        self._dirty = False
        self._next_probe = self._next_mirror = self._next_poll = 0.0
        self._next_config = now + self.CONFIG_SECONDS
        self._asked_at = None
        self._flyout_lost_at = None
        self._mirrors = {}          # source key -> what the last look at its files saw
        self._told = set()          # source keys whose blocked fetch was logged
        self._app = _App(self, panel_corner, flyout_overrides)
        self._cfg_mtime = _mtime(paths.CONFIG_PATH)
        self._app.sync_sources()
        self._gate()
        self._look(now)
        if self.role == self.STARTING and now >= self._grace_until:
            self._end_grace(now)
        self._last_seen = self._fingerprint()

    # -- what the host reads -----------------------------------------------
    @property
    def cfg(self):
        return self._app.cfg

    def config(self):
        """The config in use: a copy of the usage bar's, with the host's flyout
        settings over it."""
        return self._app.cfg

    def sources(self):
        """The enabled providers' Source objects, in display order."""
        return [] if self._closed else self._app.active()

    def readout(self):
        """(rows, event) for render_readout."""
        return readout_rows(self.sources(), self._app.cfg.get("taskbar_widget") or {})

    def readout_key(self):
        """Changes exactly when the readout would look different."""
        rows, event = self.readout()
        return (readout_key(rows), event)

    def summary(self):
        sources = self.sources()
        top = None
        for src in sources:
            for lim in src.usage.limits or []:
                pct = float(lim.get("percent") or 0)
                if top is None or pct > top[0]:
                    try:
                        name = src.limit_name(lim)
                    except Exception:
                        name = lim.get("label")
                    top = (pct, name)
        errors = [shown_error(src.usage) for src in sources]
        times = [src.usage.updated for src in sources if src.usage.updated]
        next_in = None
        if self.role == self.LEADER and sources:
            next_in = max(0.0, min(src.next_poll_at for src in sources) - self._clock())
        sa = self.standalone
        return UsageSummary(
            role=self.role,
            standalone_pid=(sa.pid or None) if sa else None,
            standalone_overlay=bool(sa and sa.overlay_visible),
            max_pct=top[0] if top else None,
            max_label=top[1] if top else None,
            signed_out=any(src.usage.error and src.signed_out_body(str(src.usage.error))
                           for src in sources),
            grant_live=any(live_events(src.usage) for src in sources),
            error=next((e for e in errors if e), None),
            updated=max(times) if times else None,
            next_poll_in=next_in)

    def status_line(self):
        """One line for a menu: who polls, and when next."""
        if self._closed:
            return "Stopped"
        if self.role == self.STARTING:
            return "Starting · looking for LLM Usage Bar"
        if self.role == self.FOLLOWER:
            sa = self.standalone
            if sa is None:
                return "Not polling · LLM Usage Bar is not running"
            if sa.pid:
                return "Following LLM Usage Bar (pid %d)" % sa.pid
            return "Following LLM Usage Bar (starting up)"
        if self._app.locked:
            return "Polling here · paused while locked"
        if any(src._fetching for src in self.sources()):
            return "Polling here · asking now"
        wait = self.summary().next_poll_in
        return "Polling here" if wait is None else "Polling here · next in %s" % _wait_words(wait)

    def providers(self):
        """[(key, name, enabled)] for every provider there is."""
        on = enabled_providers(self._app.cfg)
        return [(cls.key, cls.name, cls.key in on) for cls in SOURCES]

    def usage_page_url(self):
        return self._app.usage_page() if self.sources() else ""

    def config_path(self):
        return paths.CONFIG_PATH

    # -- the clock ---------------------------------------------------------
    def tick(self, now=None):
        """Run everything that is due. UI thread only, about once a second;
        `now` is on the feed's clock. True when the readout or the summary
        changed (on_change has been called too)."""
        if self._closed:
            return False
        now = self._clock() if now is None else now
        # A leader looks every tick, not every PROBE_SECONDS: a standalone that
        # took its mutex since the last look reads the poll and notify state
        # on its way up, and a request or an announcement made here on an
        # older look would be made by both. Looking in the same tick as the
        # request - whose _fetch writes last_request at once - leaves it no
        # gap; it costs two OpenMutexW calls a second.
        if now >= self._next_probe or self.role == self.LEADER:
            self._look(now)
        if self.role == self.STARTING and now >= self._grace_until:
            self._end_grace(now)
        if now >= self._next_config:
            self._next_config = now + self.CONFIG_SECONDS
            self.reload_config()
        # Results land in every role: a request that went out while this
        # copy led is still written to the cache when it answers.
        landed = False
        for src in self.sources():
            if src._pending is not None and src.apply_pending():
                landed = True
        if self.role == self.LEADER:
            if landed:
                self._app.check_notifications()
            if now >= self._next_poll:
                self._next_poll = now + self.POLL_SECONDS
                if not self._app.locked:
                    for src in self.sources():
                        src.maybe_poll()
        elif now >= self._next_mirror:
            self._next_mirror = now + self.MIRROR_SECONDS
            for src in self.sources():
                if self._mirror(src):
                    landed = True
        if landed:
            self._dirty = True
            if self._app.flyout is not None:
                self._app.flyout.refresh(self.sources())
        return self._changed()

    def _fingerprint(self):
        summary = dataclasses.replace(self.summary(), next_poll_in=None)
        return (self.readout_key(), summary)

    def _changed(self):
        seen = self._fingerprint()
        changed = self._dirty or seen != self._last_seen
        self._dirty, self._last_seen = False, seen
        if changed and self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log("usage feed: on_change failed: %s" % traceback.format_exc())
        return changed

    # -- roles -------------------------------------------------------------
    def _look(self, now):
        """One probe for a standalone, and what it means for the role."""
        self._next_probe = now + self.PROBE_SECONDS
        try:
            found = self._probe()
        except Exception:
            # No answer is not "none running": change nothing on it.
            log("usage feed: looking for LLM Usage Bar failed: %s" % traceback.format_exc())
            return
        if found is not None:
            self._negatives = 0
            self.standalone = found
            if self.role != self.FOLLOWER:
                self._follow()
            return
        self._negatives += 1
        if self._negatives == 1:
            self._vanished_at = now     # first seen gone; see _after_standalone
        if self._negatives >= self.LEAD_AFTER and self.standalone is not None:
            log("usage feed: LLM Usage Bar is gone")
            self.standalone = None
        if self.role == self.FOLLOWER and self.mode != "follow" \
                and self._negatives >= self.LEAD_AFTER:
            self._lead(now)

    def _end_grace(self, now):
        """Still starting when the grace runs out means no probe ever found a
        standalone: lead, unless told only ever to follow."""
        if self.mode == "follow":
            self.role = self.FOLLOWER
            log("usage feed: no LLM Usage Bar running; following anyway (mode follow)")
        else:
            self._lead(now)

    def _follow(self):
        self.role = self.FOLLOWER
        self._gate()
        self._next_mirror = 0.0
        sa = self.standalone
        log("usage feed: following LLM Usage Bar (pid %s); not polling here"
            % (sa.pid if sa and sa.pid else "starting up"))

    def _lead(self, now):
        """Take over polling. Everything is read again from disk: the
        standalone's last request and rate limit (Source.__init__ and
        first_poll_at honour them) and what it already announced."""
        was, self.role = self.role, self.LEADER
        self.standalone = None
        self._app.sources = {}
        self._app.sync_sources()
        if was == self.FOLLOWER:
            self._after_standalone(now)
        self._app.last_notified = load_notify_state(self._app.state_path)
        self._mirrors, self._told = {}, set()
        self._next_poll = now
        self._dirty = True
        log("usage feed: no LLM Usage Bar running; polling here")

    def _after_standalone(self, now):
        """A standalone before 2.1 writes last_request only when an answer
        lands. One that went away with a request out - Take over, Close, a
        crash - left no trace of it, and a source it was due on is due here at
        once: a second request seconds after its own. Such a source waits
        poll_min from when the standalone was first seen gone, as if it had
        asked just then; the cached numbers stay on screen meanwhile.

        2.1 writes the request down when it starts, with in_flight and its
        pid, so a file like that is taken as it stands - unless the pid is
        this process's: then the standalone wrote nothing at all while it ran,
        which is what a 2.0 one with no answer landed yet looks like."""
        gone_for = max(0.0, now - (self._vanished_at if self._vanished_at is not None else now))
        wall = time.time()                  # Source's clock, not the feed's
        vanished = wall - gone_for
        for src in self._app.sources.values():
            raw = _read_json(src.poll_path)
            if isinstance(raw, dict) and "in_flight" in raw and raw.get("pid") != os.getpid():
                continue
            if src.next_poll_at <= wall + self.POLL_SECONDS:
                src.next_poll_at = max(src.next_poll_at, vanished + src.poll_min)
                log("usage feed: %s: the standalone may have quit with a request out; "
                    "next poll in %ds" % (src.name, src.next_poll_at - wall))

    def _gate(self):
        """Outside the leader role, every way into a request ends at a dropped
        _fetch: the timer, Refresh, and ClaudeSource.accept's re-fetch alike."""
        if self.role == self.LEADER and not self._closed:
            return
        for src in self._app.sources.values():
            if "_fetch" not in vars(src):
                src._fetch = self._blocked(src)

    def _blocked(self, src):
        def no_fetch(events, manual=False):
            if src.key not in self._told:
                self._told.add(src.key)
                log("usage feed: %s: not fetching (%s)"
                    % (src.name, "closed" if self._closed else self.role))
        return no_fetch

    # -- following ---------------------------------------------------------
    def _mirror(self, src):
        """Take in what the standalone wrote since the last look. True when
        the numbers or the error changed."""
        seen = self._mirrors.setdefault(src.key, {"cache": None, "poll": None,
                                                  "cache_misses": 0, "poll_misses": 0})
        changed = False
        cache = _signature(src.cache_path)
        if cache != seen["cache"]:
            fresh = load_usage_cache(src.cache_path)
            if fresh is not None:
                src.usage = fresh
                seen["cache"], seen["cache_misses"] = cache, 0
                changed = True
            elif cache is None:
                seen["cache"] = None        # gone: keep what is shown; it dims with age
            else:
                # Unreadable while it exists: v2.0.1 and older write it in
                # place, so this is often a write in progress. Keep the old
                # numbers and look again; only a file that stays that way
                # means there really is nothing to show.
                seen["cache_misses"] += 1
                if seen["cache_misses"] >= self.TORN_READS:
                    src.usage = Usage()
                    seen["cache"], seen["cache_misses"] = cache, 0
                    changed = True
        poll = _signature(src.poll_path)
        if poll != seen["poll"]:
            raw = _read_json(src.poll_path) if poll is not None else None
            if isinstance(raw, dict):
                seen["poll"], seen["poll_misses"] = poll, 0
                for key in ("pace", "pace_at", "last_request", "retry_at", "backoff"):
                    value = raw.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        setattr(src, key, float(value))
                # How the last poll went counts only if it is no older than
                # the numbers; v2.0.1 does not write it at all.
                if cache is None or poll[0] >= cache[0]:
                    for key in ("error", "status"):
                        if key in raw and getattr(src.usage, key) != raw[key]:
                            setattr(src.usage, key, raw[key])
                            changed = True
            elif poll is None:
                seen["poll"] = None
            else:
                seen["poll_misses"] += 1
                if seen["poll_misses"] >= self.TORN_READS:
                    seen["poll"], seen["poll_misses"] = poll, 0
        return changed

    # -- what the host does ------------------------------------------------
    def refresh(self):
        """Fresh numbers now, as the flyout's Refresh button. Returns
        "fetching", "asked" (the standalone was asked to), "too_soon",
        "starting" (this feed, or a standalone without its window yet) or
        "closed"."""
        return self._start_fetch(True)

    def _start_fetch(self, manual):
        if self._closed:
            return "closed"
        if self.role == self.LEADER:
            # Refresh comes between ticks - after a host menu that held the UI
            # thread for as long as it was open, too - so look again first: a
            # standalone that started meanwhile is asked instead.
            self._look(self._clock())
        if self.role == self.STARTING:
            return "starting"
        if self.role == self.FOLLOWER:
            now = self._clock()
            if not manual or (self._asked_at is not None
                              and now - self._asked_at < self.ASK_SECONDS):
                return "too_soon"
            post = self._poster or _post
            sa = self.standalone
            sent = bool(sa and sa.hwnd and post(sa.hwnd, WM_COMMAND, COMMANDS["refresh"], 0))
            if not sent:
                sent = ask_standalone("refresh", poster=self._poster)
            if not sent:
                return "starting"
            self._asked_at = now
            return "asked"
        for src in self.sources():
            src.start_fetch(manual)         # the standalone's own 15 s gap applies
        return "fetching" if any(src._fetching for src in self.sources()) else "too_soon"

    def left_click(self, open_url):
        """What a click on the readout does, per the usage bar's left_click.
        "web" hands the URL to open_url, which should open it off the UI thread."""
        action = self._app.cfg.get("left_click", "flyout")
        if action == "flyout":
            self.toggle_flyout()
        elif action == "refresh":
            self.refresh()
        elif action == "web":
            url = self.usage_page_url()
            if url:
                open_url(url)

    def set_locked(self, locked):
        """Nobody reads a locked screen: a leader stops polling until unlock,
        and then asks soon, as the standalone does."""
        was, self._app.locked = self._app.locked, bool(locked)
        if was and not locked and self.role == self.LEADER:
            for src in self.sources():
                src.on_unlock()

    def reload_config(self, force=False):
        """Read config.json again if it changed (tick looks every 2 s), or now
        if forced. True when it was read."""
        if self._closed:
            return False
        if not force and _mtime(paths.CONFIG_PATH) == self._cfg_mtime:
            return False
        self._app.cfg = self._app.read_config()
        self._cfg_mtime = _mtime(paths.CONFIG_PATH)   # load_config may have just written it
        if self._app.flyout is not None:
            self._app.flyout.destroy()
            self._app.flyout = None
        self._app.sync_sources()
        self._gate()
        self._dirty = True
        return True

    def set_provider(self, key, on):
        """Switch a provider on or off in config.json, keeping at least one."""
        self._app.set_provider(key, on)

    def toggle_flyout(self):
        """Open or close the usage bar's own flyout, next to panel_corner.

        Clicking the host's readout while the flyout is open takes the focus
        from it, and it hides itself on that - then the click arrives and
        would open it again at once. A toggle within REOPEN_GUARD of such a
        hide is that click, and is dropped."""
        if self._closed:
            return
        app = self._app
        if app.flyout is not None and app.flyout.visible:
            app.flyout.hide()
            return
        lost = self._flyout_lost_at
        if lost is not None and 0 <= self._clock() - lost < self.REOPEN_GUARD:
            return
        sources = self.sources()
        if not sources:
            return
        if app.flyout is None:
            app.flyout = Flyout(app)
            app.flyout.win.bind("<FocusOut>", self._flyout_focus_out, add="+")
        app.flyout.show(sources)

    def _flyout_focus_out(self, event=None):
        # Bound after the flyout's own hide-on-focus-loss, so it runs after it.
        panel = self._app.flyout
        if panel is not None and not panel.visible:
            self._flyout_lost_at = self._clock()

    def hide_flyout(self):
        if self._app.flyout is not None and self._app.flyout.visible:
            self._app.flyout.hide()

    def flyout_visible(self):
        return bool(self._app.flyout is not None and self._app.flyout.visible)

    def close(self):
        """Stop for good: no request goes out after this, from any route."""
        if self._closed:
            return
        self._closed = True
        for src in self._app.sources.values():
            src._fetch = self._blocked(src)
        self._app.sources = {}
        if self._app.flyout is not None:
            self._app.flyout.destroy()
            self._app.flyout = None
        log("usage feed: closed")
