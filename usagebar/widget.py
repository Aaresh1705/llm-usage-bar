"""The readout drawn into the taskbar itself."""

import ctypes
import ctypes.wintypes as wt
import math
import time
import traceback

from .deps import Image, ImageDraw
from .providers.claude import live_events
from .render import draw_skull, draw_sparkle, fade_image, load_font, premultiply
from .util import color_for, hex_to_rgba, human_delta, log, parse_reset
from .win32 import ABE_BOTTOM, ABE_TOP, AC_SRC_ALPHA, AC_SRC_OVER, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, BLENDFUNCTION, DIB_RGB_COLORS, ERROR_CLASS_ALREADY_EXISTS, GA_PARENT, GWL_STYLE, GW_HWNDPREV, HWND_TOP, SIZE, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SW_HIDE, SW_SHOWNOACTIVATE, ULW_ALPHA, WM_DISPLAYCHANGE, WM_DPICHANGED, WM_LBUTTONUP, WM_RBUTTONUP, WM_SETTINGCHANGE, WM_THEMECHANGED, WNDCLASS, WNDPROC, WS_CHILD, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_POPUP, gdi32, kernel32, taskbar_info, user32, windows_uses_light_theme


def short_error(error):
    """The overlay has room for a word, not a sentence."""
    if not error:
        return None
    lowered = str(error).lower()
    if "rate limited" in lowered:
        return "paused"
    if "offline" in lowered:
        return "offline"
    if "signed" in lowered or "sign in" in lowered:
        return "sign in"
    if "loading" in lowered:
        return "…"
    return "no data"


def readout_rows(sources, widget_cfg):
    """What the readout shows, as plain data: a row per provider of
    (key, pct, reset time or None, error word or None, stale), and whether a
    live event earns the sparkle. Kept apart from the window so that an app
    embedding the usage bar can draw the same readout into a window of its own."""
    rows = []
    for src in sources:
        usage = src.usage
        limit = usage.by_key(src.metric())
        pct = float(limit["percent"]) if limit else 0.0
        reset = parse_reset(limit["resets_at"]) if limit else None
        # Keep showing the last known figures; the flyout explains the
        # trouble. A metric the API never returns would otherwise read as a
        # confident 0%.
        error = None if limit else (short_error(usage.error) or "no data")
        # Numbers that stopped being refreshed deserve to be trusted less:
        # dim them rather than pretend they are live. Dimming is about age,
        # not about whether the last poll failed - a figure from a minute
        # ago is still worth showing at full strength.
        age = usage.age_seconds()
        stale = age is None or age > max(600.0, 2.0 * src.poll_interval() + 60.0)
        rows.append((src.key, pct, reset, error, stale))
    event = (any(live_events(src.usage) for src in sources)
             and bool(widget_cfg.get("show_events", True)))
    return rows, event


def readout_key(rows):
    """The rows as they look: a percentage to one decimal and the reset as the
    words it is drawn with, so the readout is redrawn only when that changes."""
    return tuple((k, round(pct, 1), human_delta(r), e, st) for k, pct, r, e, st in rows)


class TaskbarWidget(object):
    """The always-visible readout, drawn as part of the taskbar.

    A layered window that is a real CHILD of Shell_TrayWnd (created as a popup
    and reparented with SetParent - creating it as a child outright is refused).
    That gives the one property that matters: it is exactly as visible as the
    taskbar itself, no more and no less. It is covered by whatever covers the
    taskbar (a fullscreen video, Task View), it stays put when the taskbar is
    clicked or the Start menu opens, and it slides away with an auto-hiding
    taskbar - all done by Windows, with no detection code here.

    It paints with UpdateLayeredWindow (per-pixel alpha), so the taskbar's own
    acrylic shows through around the glyphs, and every frame lands as one
    composited update.
    """

    CLASS_NAME = "LLMUsageOverlayWnd"
    _atom = None
    _proc = None                      # one WNDPROC for the class, kept alive here
    _windows = {}                     # hwnd -> instance, so the proc can dispatch
    light = None                      # None: follow Windows; True/False: drawn for that theme
    veil = True                       # False: no alpha-1 veil (the embedder lays its own)

    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.geometry = None          # (x, y, w, h) in the taskbar's client space
        self._last_key = None
        self._dc = None
        self._bitmap = None
        self._old_bitmap = None
        self._bits = None
        self._dc_size = None
        self._image = None
        self._last_blit_error = None
        self._retry_at = 0.0
        self._retry_backoff = 2.0
        self._create()

    # -- window ------------------------------------------------------------
    @classmethod
    def _dispatch(cls, hwnd, msg, wparam, lparam):
        """The window class owns one procedure; it routes to whichever instance
        owns the window. An exception here would be swallowed by ctypes and
        leave the window half-alive, so nothing is allowed to escape."""
        try:
            self = cls._windows.get(int(hwnd or 0))
            if self is not None:
                handled = self._on_message(msg, wparam, lparam)
                if handled is not None:
                    return handled
        except Exception:
            log("overlay wndproc: %s" % traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create(self):
        """Build the window, or return False and let tick() try again later."""
        if TaskbarWidget._atom is None:
            if TaskbarWidget._proc is None:
                TaskbarWidget._proc = WNDPROC(TaskbarWidget._dispatch)
            wc = WNDCLASS()
            wc.lpfnWndProc = TaskbarWidget._proc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = self.CLASS_NAME
            ctypes.set_last_error(0)
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                return False
            TaskbarWidget._atom = atom or True

        taskbar = user32.FindWindowW("Shell_TrayWnd", None)
        if not taskbar:
            return False                  # the shell is between lives; try later

        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            self.CLASS_NAME, "LLM usage", WS_POPUP, 0, 0, 10, 10,
            None, None, kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            log("overlay window creation failed: %s" % ctypes.get_last_error())
            return False

        # Popup -> child, then hand it to the taskbar. A refused SetParent
        # leaves a stray popup behind, so destroy it rather than keep it.
        get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        put(hwnd, GWL_STYLE, (get(hwnd, GWL_STYLE) & ~WS_POPUP) | WS_CHILD)
        ctypes.set_last_error(0)
        if not user32.SetParent(hwnd, taskbar):
            log("overlay could not attach to the taskbar: %s" % ctypes.get_last_error())
            user32.DestroyWindow(hwnd)
            return False

        self.hwnd = hwnd
        TaskbarWidget._windows[int(hwnd)] = self
        self.geometry = None
        self._last_key = None
        return True

    def _on_message(self, msg, wparam, lparam):
        """Return None for anything we don't handle. Clicks are handed to the
        pump rather than acted on here - see TrayApp.defer."""
        if msg == WM_LBUTTONUP:
            self.app.defer(self.app.left_click)
            return 0
        if msg == WM_RBUTTONUP:
            self.app.request_menu()
            return 0
        if msg in (WM_DISPLAYCHANGE, WM_SETTINGCHANGE, WM_DPICHANGED, WM_THEMECHANGED):
            self.geometry = None      # re-measure against the new taskbar/theme
            self._last_key = None
            return 0
        return None

    def cfg(self):
        return self.app.cfg.get("taskbar_widget") or {}

    def reconfigure(self):
        self._last_key = None
        self.geometry = None

    def screen_rect(self):
        """Where the widget is on screen, for anchoring the flyout and toast."""
        if not self.hwnd:
            return None
        r = wt.RECT()
        if not user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return None
        return r

    def destroy(self):
        self._release_dc()
        if self.hwnd:
            TaskbarWidget._windows.pop(int(self.hwnd), None)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    # -- surface -----------------------------------------------------------
    def _release_dc(self):
        if self._dc:
            if self._old_bitmap:
                gdi32.SelectObject(self._dc, self._old_bitmap)
            gdi32.DeleteDC(self._dc)
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
        self._dc = self._bitmap = self._old_bitmap = self._bits = self._dc_size = None

    def _ensure_dc(self, w, h):
        if self._dc_size == (w, h) and self._dc:
            return True
        self._release_dc()
        screen = user32.GetDC(None)
        if not screen:
            return False
        try:
            self._dc = gdi32.CreateCompatibleDC(screen)
            if not self._dc:
                return False
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = w
            info.bmiHeader.biHeight = -h          # top-down
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            self._bitmap = gdi32.CreateDIBSection(self._dc, ctypes.byref(info), DIB_RGB_COLORS,
                                                  ctypes.byref(bits), None, 0)
            if not self._bitmap:
                return False
            self._bits = bits
            self._old_bitmap = gdi32.SelectObject(self._dc, self._bitmap)
            self._dc_size = (w, h)
            return True
        finally:
            user32.ReleaseDC(None, screen)

    def _blit(self, image):
        """Push a fresh frame to DWM in one atomic call. Position and size are
        the window's own (set with SetWindowPos in the taskbar's client space);
        only the pixels travel here."""
        if not self.hwnd or self.geometry is None:
            return False
        _, _, w, h = self.geometry
        if not self._ensure_dc(w, h):
            return False
        data = premultiply(image).tobytes("raw", "BGRA")
        ctypes.memmove(self._bits, data, len(data))
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        src, size = wt.POINT(0, 0), SIZE(w, h)
        screen = user32.GetDC(None)
        if not screen:
            return False
        try:
            ctypes.set_last_error(0)
            ok = user32.UpdateLayeredWindow(self.hwnd, screen, None, ctypes.byref(size),
                                            self._dc, ctypes.byref(src), 0,
                                            ctypes.byref(blend), ULW_ALPHA)
            if not ok:
                # Silently ignoring this is how a blank widget goes unnoticed.
                err = ctypes.get_last_error()
                if err != self._last_blit_error:
                    self._last_blit_error = err
                    log("UpdateLayeredWindow failed: err=%s" % err)
                return False
            self._last_blit_error = None
            return True
        finally:
            user32.ReleaseDC(None, screen)

    # -- placement ---------------------------------------------------------
    def _target_geometry(self, taskbar):
        """Where to sit inside the taskbar, or None for a vertical taskbar."""
        info = taskbar_info()
        if info is None or info[1] not in (ABE_TOP, ABE_BOTTOM):
            return None
        client = wt.RECT()
        if not user32.GetClientRect(taskbar, ctypes.byref(client)):
            return None
        width, height = client.right - client.left, client.bottom - client.top
        if width <= 0 or height <= 0:
            return None

        c = self.cfg()
        scale = height / 48.0                      # 48px is the 100% DPI taskbar
        pad = max(2, int(float(c.get("padding", 5)) * scale))
        h = max(12, height - 2 * pad)
        w = max(40, int(float(c.get("width", 128)) * scale))
        ox, oy = (c.get("offset") or [8, 0])[:2]
        ox, oy = int(float(ox) * scale), int(float(oy) * scale)
        x = width - w - ox if str(c.get("corner", "left")) == "right" else ox
        y = (height - h) // 2 + oy
        return x, y, w, h

    def tick(self):
        """Called twice a second: liveness, placement, content."""
        if self.hwnd and not user32.IsWindow(self.hwnd):
            # Explorer restarted: the taskbar took its children with it.
            log("overlay window went away with the taskbar; rebuilding")
            TaskbarWidget._windows.pop(int(self.hwnd), None)
            self.hwnd = None
            self._release_dc()
        if not self.hwnd:
            now = time.time()
            if now < self._retry_at:
                return
            if not self._create():
                self._retry_backoff = min(60.0, self._retry_backoff * 2)
                self._retry_at = now + self._retry_backoff
                return
            self._retry_backoff = 2.0

        taskbar = user32.GetAncestor(self.hwnd, GA_PARENT)
        geometry = self._target_geometry(taskbar)
        if geometry is None:
            if self.geometry is not None:
                user32.ShowWindow(self.hwnd, SW_HIDE)
                self.geometry = None
            return
        if geometry != self.geometry:
            x, y, w, h = geometry
            self.geometry = geometry
            self._last_key = None
            user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOP), x, y, w, h, SWP_NOACTIVATE)
        # The taskbar's own content is a sibling; if it ever ends up in front
        # of us, step back in front of it. Read-only unless something changed.
        if user32.GetWindow(self.hwnd, GW_HWNDPREV):
            user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOP), 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.refresh(self.app.active())
        if not user32.IsWindowVisible(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    # -- drawing -----------------------------------------------------------
    def _is_light(self):
        """The theme to draw for: Windows' own, unless one was set."""
        if self.light is None:
            return windows_uses_light_theme()
        return bool(self.light)

    def _colors(self):
        """No background is painted, so these only have to stay legible on the
        real taskbar - which means following the Windows theme."""
        c = self.cfg()
        light = self._is_light()
        fg = c.get("text_color", "auto")
        muted = c.get("muted_color", "auto")
        if fg == "auto":
            fg = "#1A1A1A" if light else "#F2F2F2"
        if muted == "auto":
            muted = "#5F6368" if light else "#B9BEC4"
        track = (0, 0, 0, 60) if light else (255, 255, 255, 70)
        return fg, muted, track

    def refresh(self, sources):
        if self.geometry is None:
            return
        rows, event = readout_rows(sources, self.cfg())
        state = (readout_key(rows), event, self.geometry, windows_uses_light_theme())
        if state == self._last_key:
            return
        _, _, w, h = self.geometry
        image = self.compose(w, h, rows, event)
        self._image = image
        if self._blit(image):
            self._last_key = state

    def compose(self, w, h, rows, event):
        """The readout for `rows` (see readout_rows), w x h pixels."""
        c = self.cfg()
        sparkle = hex_to_rgba(c.get("event_color", "#F59E0B"))
        if len(rows) == 1:
            _, pct, reset, error, stale = rows[0]
            image = self._render(w, h, pct, reset, error)
            if event:
                draw_sparkle(image, w, h, sparkle)
            if stale:
                image = fade_image(image, float(c.get("stale_opacity", 0.55)))
        else:
            image = self._render_rows(w, h, rows, event)
            if event:
                draw_sparkle(image, w, h, sparkle)
        return image

    # How each provider's row is told apart when there are several.
    MARKS = {"claude": ("spark", "#D97757"), "ollama": ("ring", None)}

    def _render_rows(self, w, h, rows, event):
        """Several providers at once: a row each, in the width one takes -
        mark, percentage, bar, time to reset."""
        c = self.cfg()
        fg, muted, track = self._colors()
        ss = max(1, int(c.get("supersample", 3)))
        W, H = w * ss, h * ss
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        backdrop = c.get("background", "transparent")
        if backdrop and backdrop != "transparent":
            ImageDraw.Draw(img).rounded_rectangle(
                (0, 0, W - 1, H - 1), radius=float(c.get("corner_radius", 6)) * ss,
                fill=hex_to_rgba(backdrop, int(c.get("background_alpha", 255))))

        def font(px, bold=True):
            name = c.get("bold_font_file" if bold else "font_file",
                         "segoeuib.ttf" if bold else "segoeui.ttf")
            return load_font({"font_file": name}, px)

        rh = H // len(rows)
        m = rh * 0.62                          # the mark
        gap = rh * 0.28
        num, small = font(rh * 0.78), font(rh * 0.62, False)
        probe = ImageDraw.Draw(img)
        skull = rh * 0.80 * 0.9

        # Columns shared by every row - mark, percentage, bar, time to reset -
        # so the bars line up, and are drawn in every row or in none.
        pct_w = reset_w = 0.0
        for _, pct, reset, error, _ in rows:
            if error:
                continue
            pct = max(0.0, min(100.0, pct))
            pct_w = max(pct_w, skull if pct >= 99.5 else probe.textlength("%d%%" % round(pct), font=num))
            if bool(c.get("show_reset", True)) and reset is not None:
                reset_w = max(reset_w, probe.textlength(human_delta(reset), font=small))
        # The sparkle sits in the top-right corner; the time column stays clear of it.
        right = W - ((max(7, int(h * 0.30)) + 2) * ss if event else 0)
        bar_x0 = m + gap + pct_w + gap
        bar_x1 = right - (reset_w + gap if reset_w else 0)
        show_bar = bool(c.get("show_bar", True)) and bar_x1 - bar_x0 > rh * 0.6
        bar_h = max(2 * ss, int(rh * 0.24))

        for i, (key, pct, reset, error, stale) in enumerate(rows):
            row = Image.new("RGBA", (W, rh), (0, 0, 0, 0))
            d = ImageDraw.Draw(row)
            self._draw_mark(d, key, (0, (rh - m) / 2, m, (rh + m) / 2), fg)
            x = m + gap
            if error:
                d.text((x, rh / 2), error, font=small, fill=hex_to_rgba(muted), anchor="lm")
            else:
                pct = max(0.0, min(100.0, pct))
                color = color_for(pct, self.app.cfg["thresholds"])
                if pct >= 99.5:
                    size = rh * 0.80
                    draw_skull(row, (x, (rh - size) / 2, x + skull, (rh + size) / 2),
                               hex_to_rgba(fg), False)
                else:
                    d.text((x, rh / 2), "%d%%" % round(pct), font=num,
                           fill=hex_to_rgba(color), anchor="lm")
                if reset_w and reset is not None:
                    d.text((right, rh / 2), human_delta(reset), font=small,
                           fill=hex_to_rgba(muted), anchor="rm")
                if show_bar:
                    y0 = rh / 2 - bar_h / 2
                    d.rounded_rectangle((bar_x0, y0, bar_x1, y0 + bar_h), radius=bar_h / 2,
                                        fill=track)
                    span = (bar_x1 - bar_x0) * pct / 100.0
                    if span > 0:
                        d.rounded_rectangle((bar_x0, y0, bar_x0 + max(span, bar_h), y0 + bar_h),
                                            radius=bar_h / 2, fill=hex_to_rgba(color))
            if stale:
                row = fade_image(row, float(c.get("stale_opacity", 0.55)))
            img.alpha_composite(row, (0, i * rh + (H - rh * len(rows)) // 2))
        return self._finish(img, w, h)

    def _draw_mark(self, d, key, box, fg):
        """Claude's spark, Ollama's ring - enough to tell two rows apart."""
        shape, color = self.MARKS.get(key, ("ring", None))
        color = hex_to_rgba(color or fg)
        x0, y0, x1, y1 = box
        cx, cy, r = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0
        if shape == "spark":
            width = max(1, int(r * 0.36))
            for angle in (0, 45, 90, 135):
                dx, dy = math.cos(math.radians(angle)) * r, math.sin(math.radians(angle)) * r
                d.line((cx - dx, cy - dy, cx + dx, cy + dy), fill=color, width=width)
        else:
            d.ellipse((x0, y0, x1, y1), outline=color, width=max(1, int(r * 0.30)))
            inner = r * 0.36
            d.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=color)

    def _render(self, w, h, pct, reset, error):
        c = self.cfg()
        fg, muted, track = self._colors()
        ss = max(1, int(c.get("supersample", 3)))
        W, H = w * ss, h * ss
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pct = max(0.0, min(100.0, pct))
        color = color_for(pct, self.app.cfg["thresholds"])
        spent = pct >= 99.5

        def font(px, bold=True):
            name = c.get("bold_font_file" if bold else "font_file",
                         "segoeuib.ttf" if bold else "segoeui.ttf")
            return load_font({"font_file": name}, px)

        backdrop = c.get("background", "transparent")
        if backdrop and backdrop != "transparent":
            radius = float(c.get("corner_radius", 6)) * ss
            d.rounded_rectangle((0, 0, W - 1, H - 1), radius=radius,
                                fill=hex_to_rgba(backdrop, int(c.get("background_alpha", 255))))

        if error:
            d.text((0, H / 2), error, font=font(H * 0.44, False),
                   fill=hex_to_rgba(muted), anchor="lm")
            return self._finish(img, w, h)

        # Left: the number (or the skull, same as the tray at 100%).
        num_font = font(H * 0.62)
        if spent:
            skull_w = H * 0.52
            draw_skull(img, (0, H * 0.18, skull_w, H * 0.82), hex_to_rgba(fg), False)
            text_end = skull_w
        else:
            label = "%d%%" % round(pct)
            d.text((0, H / 2), label, font=num_font, fill=hex_to_rgba(color), anchor="lm")
            text_end = d.textlength(label, font=num_font)

        x0 = text_end + H * 0.22
        if x0 >= W:
            return self._finish(img, w, h)

        show_reset = bool(c.get("show_reset", True)) and reset is not None
        show_bar = bool(c.get("show_bar", True))
        bar_h = float(c.get("bar_height", 4)) * ss * (h / 38.0)   # 38px tall at 100% DPI
        bar_h = int(max(2 * ss, min(H * 0.22, bar_h)))

        if show_reset:
            small = font(H * 0.36, False)
            text = human_delta(reset)
            while d.textlength(text, font=small) > (W - x0) and len(text) > 4:
                text = text[:-1]
            d.text((x0, H * 0.30), text, font=small, fill=hex_to_rgba(muted), anchor="lm")
            bar_y = H * 0.62
        else:
            bar_y = H / 2 - bar_h / 2

        if show_bar:
            d.rounded_rectangle((x0, bar_y, W - 1, bar_y + bar_h), radius=bar_h / 2, fill=track)
            span = (W - 1 - x0) * pct / 100.0
            if span > 0:
                d.rounded_rectangle((x0, bar_y, x0 + max(span, bar_h), bar_y + bar_h),
                                    radius=bar_h / 2, fill=hex_to_rgba(color))

        return self._finish(img, w, h)

    def _finish(self, img, w, h):
        """Downsample, then float the whole rectangle one step above fully
        transparent: UpdateLayeredWindow hit-tests on alpha, and a click has to
        land anywhere on the widget, not only on a letter. The veil takes the
        theme's own colour so that even those 0.4% are invisible. An app that
        draws the readout into a bigger window of its own veils that whole
        window instead (`veil` False), and gets the plain downsample."""
        small = img.resize((w, h), Image.LANCZOS)
        if not self.veil:
            return small
        veil = (255, 255, 255, 1) if self._is_light() else (0, 0, 0, 1)
        base = Image.new("RGBA", (w, h), veil)
        base.alpha_composite(small)
        return base
