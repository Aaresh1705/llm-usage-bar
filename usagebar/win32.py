"""The Win32 API through ctypes: functions, constants, structures, theme queries."""

import ctypes
import ctypes.wintypes as wt
import uuid
import winreg


# ---------------------------------------------------------------------------
# Win32 tray plumbing
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
wtsapi32.WTSRegisterSessionNotification.argtypes = [wt.HWND, wt.DWORD]
wtsapi32.WTSRegisterSessionNotification.restype = wt.BOOL
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
NOTIFY_FOR_THIS_SESSION = 0
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
NIN_SELECT = WM_APP + 0
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIF_GUID = 0x20
NIF_SHOWTIP = 0x80
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49
TPM_RIGHTALIGN, TPM_BOTTOMALIGN, TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0008, 0x0020, 0x0100, 0x0002
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0800, 0x0008
PM_REMOVE = 0x0001

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_byte * 8)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wt.UINT), ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wt.HANDLE),
    ]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                   wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.CreatePopupMenu.restype = wt.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.LPVOID]
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_void_p, wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
shell32.Shell_NotifyIconW.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]

ICON_NAMESPACE = uuid.UUID("8f2b41c6-0d7a-4f19-9a3e-7c5e2b1d4a60")


def guid_for(index):
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.uuid5(ICON_NAMESPACE, "segment-%d" % index).bytes_le, 16)
    return g
MF_GRAYED = 0x0001

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_ROUND = 2

try:
    dwmapi = ctypes.WinDLL("dwmapi")
except Exception:
    dwmapi = None


def round_window_corners(hwnd):
    """Windows 11 rounds its own flyouts; a borderless Tk window stays square
    unless we ask DWM for the same treatment."""
    if dwmapi is None or not hwnd:
        return
    try:
        value = ctypes.c_int(DWMWA_ROUND)
        dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Taskbar overlay
#
# Windows 11 dropped deskbands, and the widgets board can't put anything on the
# taskbar itself, so the only way to be permanently visible down there is to
# draw our own window over the taskbar's free corner - the same trick the
# now-playing widgets use. It has to follow the taskbar: position, DPI,
# auto-hide, Explorer restarts, and it must get out of the way of fullscreen
# apps.
# ---------------------------------------------------------------------------

ABM_GETSTATE = 0x00000004
ABM_GETTASKBARPOS = 0x00000005
ABS_AUTOHIDE = 0x00000001
ABE_LEFT, ABE_TOP, ABE_RIGHT, ABE_BOTTOM = 0, 1, 2, 3
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_EX_NOPARENTNOTIFY = 0x00000004
HWND_TOP = 0
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
GW_HWNDPREV = 3
MONITOR_DEFAULTTONEAREST = 2
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0
ERROR_CLASS_ALREADY_EXISTS = 1410

WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
WM_DISPLAYCHANGE = 0x007E
WM_SETTINGCHANGE = 0x001A
WM_DPICHANGED = 0x02E0
WM_THEMECHANGED = 0x031A

# Windows' own "is the user busy" signal: a game, a fullscreen video, a
# presentation. Used to hold back toasts, the same way Windows holds back its own.
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uCallbackMessage", wt.UINT),
                ("uEdge", wt.UINT), ("rc", wt.RECT), ("lParam", wt.LPARAM)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                       wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.IsWindow.argtypes = [wt.HWND]
user32.SetParent.restype = wt.HWND
user32.SetParent.argtypes = [wt.HWND, wt.HWND]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.MonitorFromPoint.restype = wt.HANDLE
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
if hasattr(user32, "GetWindowLongPtrW"):
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
shell32.SHAppBarMessage.restype = ctypes.c_size_t
shell32.SHAppBarMessage.argtypes = [wt.DWORD, ctypes.POINTER(APPBARDATA)]
try:
    shell32.SHQueryUserNotificationState.restype = ctypes.c_long
    shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
except Exception:
    pass

GA_PARENT = 1


def taskbar_info():
    """(rect, edge, autohidden) for the taskbar, or None if it can't be found."""
    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    if not shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
        return None
    state = shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
    return data.rc, data.uEdge, bool(state & ABS_AUTOHIDE)


def user_is_busy():
    """True when Windows says a game, a fullscreen video or a presentation owns
    the screen. This is the signal Windows itself uses to hold back toasts."""
    try:
        state = ctypes.c_int(0)
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
    except Exception:
        pass
    return False


def windows_accent_color():
    """The user's accent colour, as Windows stores it (ABGR)."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        with key:
            value = int(winreg.QueryValueEx(key, "AccentColor")[0]) & 0xFFFFFF
        return "#%02X%02X%02X" % (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)
    except Exception:
        return "#0F6CBD"


def windows_uses_light_theme():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except Exception:
        return False


def theme_ink(light=None):
    """Ink for glyphs that have to read against the taskbar itself rather than
    against a fill of ours. Near-black on light, near-white on dark - the flat
    extremes look like a hole punched in the bar, so both are pulled back a
    step, the same pair the widget uses for its text."""
    if light is None:
        light = windows_uses_light_theme()
    return "#1A1A1A" if light else "#F2F2F2"
