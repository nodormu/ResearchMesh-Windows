"""`desktop_window` — list, focus, move and resize windows on Windows.

Windows are read and changed with the Win32 API through ctypes (EnumWindows,
ShowWindow, SetWindowPos, SetForegroundWindow). Matching, validation and output
formatting happen in Python, over a small API object, so an action is a listing
call, one call on the single matching window, and a read-back once the window
settles.

Typing goes to whichever window has the focus, and a click through the `computer`
tool does not always move it, so this is how the agent puts the right window in
front before it types.

Windows has no full-screen mode for an arbitrary window, so `fullscreen`
maximizes. Windows refuses some focus changes, and a program that is not
elevated cannot change an elevated program's window; the reply says so when the
window did not change as asked.

Requires:  Windows. No extra package.
"""

import asyncio
import ctypes
import re
import sys
import time
from typing import Any

from core.output import clip

TOOLS = [
    {
        "name": "desktop_window",
        "description": (
            "List the desktop's windows, or bring one to the front, move or resize "
            "it, maximize it, or minimize it. Windows only. Use it before `computer` "
            "when keystrokes must reach a particular window: typing goes to whichever "
            "window has the focus. `window` is a window id from `list` (like 0x1A2B3C), "
            "or part of its title or program name (for example `chrome` or "
            "`notepad`); if more than one window matches, the matches are listed and "
            "nothing changes. `fullscreen` maximizes the window, since Windows has no "
            "full-screen mode for an arbitrary one. A window of an elevated program "
            "ignores a client that is not elevated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "activate", "move", "fullscreen", "minimize", "restore"],
                },
                "window": {"type": "string", "description": "Id, title or program name, for every action but `list`."},
                "x": {"type": "integer", "description": "Left edge in desktop pixels, for `move`."},
                "y": {"type": "integer", "description": "Top edge in desktop pixels, for `move`."},
                "width": {"type": "integer", "description": "Width in pixels, for `move`."},
                "height": {"type": "integer", "description": "Height in pixels, for `move`."},
                "enabled": {
                    "type": "boolean",
                    "description": "For `fullscreen`: false un-maximizes. Default true.",
                },
            },
            "required": ["action"],
        },
    }
]

_NAMES = {"desktop_window"}
_ACTIONS = ("list", "activate", "move", "fullscreen", "minimize", "restore")
_MAX_TEXT = 6000
_SETTLE = 0.3
_ID = re.compile(r"0x[0-9a-f]+")
_TOLERANCE = 2  # pixels a moved window may differ from the request

_SW_MAXIMIZE, _SW_MINIMIZE, _SW_RESTORE = 3, 6, 9
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class _MONITORINFOEXW(ctypes.Structure):
    # szDevice is 32 UTF-16 units; kept as integers so the size is the same on any platform.
    _fields_ = [
        ("cbSize", ctypes.c_uint32), ("rcMonitor", _RECT), ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32), ("szDevice", ctypes.c_uint16 * 32),
    ]


def _outer_rect(
    visible: tuple[int, int, int, int], delta: tuple[int, int, int, int], request: dict
) -> tuple[int, int, int, int]:
    """The rectangle SetWindowPos needs, for the visible (x, y, width, height) the
    caller asked for. Windows 10 and 11 add invisible resize borders around a
    window: `delta` is how far the outer rectangle extends past the visible frame
    on the left, top, right and bottom. Fields missing from `request` keep their
    current visible value."""
    x = request.get("x", visible[0])
    y = request.get("y", visible[1])
    width = request.get("width", visible[2])
    height = request.get("height", visible[3])
    left, top, right, bottom = delta
    return x - left, y - top, width + left + right, height + top + bottom


class _Win32:
    """The Win32 calls this tool makes. Only this class touches ctypes."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("desktop_window needs Windows")
        c = ctypes
        self.user32 = c.WinDLL("user32", use_last_error=True)
        self.dwm = c.WinDLL("dwmapi", use_last_error=True)
        self.kernel32 = c.WinDLL("kernel32", use_last_error=True)
        hwnd, dword, boolean = c.c_void_p, c.c_uint32, c.c_int
        for name, restype, argtypes in (
            ("IsWindowVisible", boolean, [hwnd]),
            ("IsIconic", boolean, [hwnd]),
            ("IsZoomed", boolean, [hwnd]),
            ("GetWindowTextLengthW", c.c_int, [hwnd]),
            ("GetWindowTextW", c.c_int, [hwnd, c.c_wchar_p, c.c_int]),
            ("GetClassNameW", c.c_int, [hwnd, c.c_wchar_p, c.c_int]),
            ("GetWindowThreadProcessId", dword, [hwnd, c.POINTER(dword)]),
            ("GetWindow", hwnd, [hwnd, dword]),
            ("GetWindowLongW", c.c_long, [hwnd, c.c_int]),
            ("GetForegroundWindow", hwnd, []),
            ("GetWindowRect", boolean, [hwnd, c.POINTER(_RECT)]),
            ("ShowWindow", boolean, [hwnd, c.c_int]),
            ("SetWindowPos", boolean, [hwnd, hwnd, c.c_int, c.c_int, c.c_int, c.c_int, c.c_uint]),
            ("SetForegroundWindow", boolean, [hwnd]),
            ("BringWindowToTop", boolean, [hwnd]),
            ("AttachThreadInput", boolean, [dword, dword, boolean]),
            ("GetMonitorInfoW", boolean, [hwnd, c.POINTER(_MONITORINFOEXW)]),
        ):
            function = getattr(self.user32, name)
            function.restype, function.argtypes = restype, argtypes
        self.dwm.DwmGetWindowAttribute.argtypes = [hwnd, dword, c.c_void_p, dword]
        self.dwm.DwmGetWindowAttribute.restype = c.c_long
        self.kernel32.OpenProcess.restype = hwnd
        self.kernel32.OpenProcess.argtypes = [dword, boolean, dword]
        self.kernel32.QueryFullProcessImageNameW.argtypes = [hwnd, dword, c.c_wchar_p, c.POINTER(dword)]
        self.kernel32.CloseHandle.argtypes = [hwnd]
        self.kernel32.GetCurrentThreadId.restype = dword
        self._enum_windows = c.WINFUNCTYPE(boolean, hwnd, c.c_ssize_t)
        self._enum_monitors = c.WINFUNCTYPE(boolean, hwnd, hwnd, c.POINTER(_RECT), c.c_ssize_t)
        self.user32.EnumWindows.restype = boolean
        self.user32.EnumWindows.argtypes = [self._enum_windows, c.c_ssize_t]
        self.user32.EnumDisplayMonitors.restype = boolean
        self.user32.EnumDisplayMonitors.argtypes = [hwnd, hwnd, self._enum_monitors, c.c_ssize_t]
        self.user32.keybd_event.argtypes = [c.c_ubyte, c.c_ubyte, dword, c.c_size_t]

    def _text(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _class(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _program(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(1024)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def _cloaked(self, hwnd: int) -> bool:
        value = ctypes.c_uint32(0)
        self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), 4)  # DWMWA_CLOAKED
        return value.value != 0

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = _RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def _frame_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = _RECT()
        result = self.dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))  # DWMWA_EXTENDED_FRAME_BOUNDS
        if result != 0:
            return self._window_rect(hwnd)
        return rect.left, rect.top, rect.right, rect.bottom

    def bounds(self, hwnd: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = self._frame_rect(hwnd)
        return left, top, right - left, bottom - top

    def frame_delta(self, hwnd: int) -> tuple[int, int, int, int]:
        outer, frame = self._window_rect(hwnd), self._frame_rect(hwnd)
        return frame[0] - outer[0], frame[1] - outer[1], outer[2] - frame[2], outer[3] - frame[3]

    def info(self, hwnd: int) -> dict:
        pid = ctypes.c_uint32(0)
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        x, y, w, h = self.bounds(hwnd)
        return {
            "id": f"0x{hwnd:x}", "hwnd": hwnd, "title": self._text(hwnd),
            "cls": self._program(pid.value) or self._class(hwnd), "pid": pid.value,
            "x": x, "y": y, "w": w, "h": h, "output": "",
            "minimized": bool(self.user32.IsIconic(hwnd)), "maximized": bool(self.user32.IsZoomed(hwnd)),
            "active": self.foreground() == hwnd,
        }

    def windows(self) -> list[dict]:
        found: list[int] = []

        def collect(hwnd: int, _lparam: int) -> int:
            found.append(hwnd)
            return 1

        callback = self._enum_windows(collect)
        self.user32.EnumWindows(callback, 0)
        out = []
        for hwnd in found:
            if not self.user32.IsWindowVisible(hwnd) or self._cloaked(hwnd) or self._class(hwnd) == "Progman":
                continue
            style = self.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            appwindow = bool(style & 0x40000)  # WS_EX_APPWINDOW
            if style & 0x80 and not appwindow:  # WS_EX_TOOLWINDOW
                continue
            if self.user32.GetWindow(hwnd, 4) and not appwindow:  # GW_OWNER
                continue
            record = self.info(hwnd)
            if record["title"]:
                out.append(record)
        return out

    def screens(self) -> list[dict]:
        rects: list[tuple[str, int, int, int, int]] = []

        def collect(monitor: int, _dc: int, _rect: Any, _lparam: int) -> int:
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcMonitor
                name = bytes(info.szDevice).decode("utf-16-le").split("\x00")[0]
                rects.append((name, r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        callback = self._enum_monitors(collect)
        self.user32.EnumDisplayMonitors(None, None, callback, 0)
        return [{"name": n, "x": x, "y": y, "w": w, "h": h} for n, x, y, w, h in sorted(rects, key=lambda r: (r[1], r[2]))]

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.user32.IsIconic(hwnd))

    def is_zoomed(self, hwnd: int) -> bool:
        return bool(self.user32.IsZoomed(hwnd))

    def show(self, hwnd: int, command: int) -> None:
        self.user32.ShowWindow(hwnd, command)

    def set_pos(self, hwnd: int, x: int, y: int, width: int, height: int) -> None:
        self.user32.SetWindowPos(hwnd, None, x, y, width, height, _SWP_NOZORDER | _SWP_NOACTIVATE)

    def set_foreground(self, hwnd: int) -> None:
        self.user32.SetForegroundWindow(hwnd)

    def set_foreground_attached(self, hwnd: int) -> None:
        """Share the foreground window's input queue for the call, which Windows
        accepts as the caller being the foreground process."""
        foreground = self.user32.GetForegroundWindow()
        theirs = self.user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        mine = self.kernel32.GetCurrentThreadId()
        attached = bool(theirs and theirs != mine and self.user32.AttachThreadInput(mine, theirs, 1))
        try:
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                self.user32.AttachThreadInput(mine, theirs, 0)

    def set_foreground_after_alt(self, hwnd: int) -> None:
        """Last resort: a tap of Alt counts as input from this process. It can
        highlight the menu bar of the window that had the focus."""
        self.user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU
        self.user32.keybd_event(0x12, 0, 2, 0)  # KEYEVENTF_KEYUP
        self.user32.SetForegroundWindow(hwnd)


_api_instance: Any = None


def _api() -> Any:
    global _api_instance
    if _api_instance is None:
        _api_instance = _Win32()
    return _api_instance


def handles(name: str) -> bool:
    return name in _NAMES


def _validate(tool_input: dict) -> tuple[str, dict, str | None]:
    """(action, arguments, error)."""
    action = tool_input.get("action")
    if action not in _ACTIONS:
        return "", {}, f"Error: `action` must be one of {', '.join(_ACTIONS)}"
    args: dict[str, Any] = {}
    if action != "list":
        window = tool_input.get("window")
        if not isinstance(window, str) or not window.strip():
            return "", {}, "Error: this action needs `window` (an id, or part of a title or program name)"
        args["window"] = window.strip()
    if action == "move":
        for key in ("x", "y", "width", "height"):
            if key in tool_input:
                value = tool_input[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    return "", {}, f"Error: `{key}` must be an integer"
                if key in ("width", "height") and value < 1:
                    return "", {}, f"Error: `{key}` must be at least 1"
                args[key] = value
        if not any(key in args for key in ("x", "y", "width", "height")):
            return "", {}, "Error: `move` needs at least one of x, y, width, height"
    if action == "fullscreen" and "enabled" in tool_input:
        if not isinstance(tool_input["enabled"], bool):
            return "", {}, "Error: `enabled` must be true or false"
        args["enabled"] = tool_input["enabled"]
    return str(action), args, None


def _pick(windows: list[dict], term: str) -> list[dict]:
    """The windows a `window` argument names: an exact id, else a part of the
    title or program name, ignoring case."""
    t = term.strip().lower()
    if _ID.fullmatch(t):
        return [w for w in windows if w["id"] == t]
    return [w for w in windows if t in w["title"].lower() or t in w["cls"].lower()]


def _attach_outputs(windows: list[dict], screens: list[dict]) -> None:
    """Name the monitor under each window's centre."""
    for w in windows:
        cx, cy = w["x"] + w["w"] / 2, w["y"] + w["h"] / 2
        w["output"] = next(
            (s["name"] for s in screens if s["x"] <= cx < s["x"] + s["w"] and s["y"] <= cy < s["y"] + s["h"]), ""
        )


def _row(window: dict) -> str:
    flags = [name for name in ("active", "minimized", "maximized") if window.get(name)]
    title = window["title"] if len(window["title"]) <= 80 else window["title"][:77] + "..."
    return (
        f'{window["id"]:<10} {window["output"] or "-":<12} {window["x"]},{window["y"]} '
        f'{window["w"]}x{window["h"]}  {",".join(flags) or "-":<10} {window["cls"]}  "{title}"'
    )


def _render(action: str, result: dict) -> str:
    if not result.get("ok"):
        text = f"Error: {result.get('error', 'desktop_window failed')}"
        if result.get("matches"):
            text += "\n" + "\n".join(_row(w) for w in result["matches"])
        return text
    if action == "list":
        screens = "; ".join(f'{s["name"]} {s["x"]},{s["y"]} {s["w"]}x{s["h"]}' for s in result["screens"])
        rows = [_row(w) for w in result["windows"][:40]]
        return clip(f"Screens: {screens}\nWindows ({len(result['windows'])}):\n" + "\n".join(rows), _MAX_TEXT)
    text = f"{action} done:\n{_row(result['window'])}"
    if result.get("note"):
        text += f"\nNote: {result['note']}"
    return text


def available() -> str | None:
    """Why the tool cannot run here, or None."""
    if sys.platform != "win32":
        return "it runs on Windows only"
    return None


def _activate(api: Any, hwnd: int) -> str:
    """Bring the window to the front. Windows lets only some callers take the
    focus, so try the plain call, then sharing the foreground thread's input,
    then a tap of Alt. Returns a note when none of them worked."""
    if api.is_iconic(hwnd):
        api.show(hwnd, _SW_RESTORE)
    for step in (api.set_foreground, api.set_foreground_attached, api.set_foreground_after_alt):
        step(hwnd)
        time.sleep(0.05)
        if api.foreground() == hwnd:
            return ""
    return "Windows did not let this process take the focus; another program holds the foreground."


def _apply(api: Any, hwnd: int, action: str, args: dict) -> str:
    """Do `action` to the window. Returns a note, or an empty string."""
    if action == "activate":
        return _activate(api, hwnd)
    if action == "minimize":
        api.show(hwnd, _SW_MINIMIZE)
    elif action == "restore":
        api.show(hwnd, _SW_RESTORE)
    elif action == "fullscreen":
        if args.get("enabled") is False:
            if api.is_zoomed(hwnd):
                api.show(hwnd, _SW_RESTORE)
        else:
            api.show(hwnd, _SW_MAXIMIZE)
    elif action == "move":
        # Moving a minimized or maximized window would act on its stored frame.
        if api.is_iconic(hwnd) or api.is_zoomed(hwnd):
            api.show(hwnd, _SW_RESTORE)
        visible = api.bounds(hwnd)
        x, y, width, height = _outer_rect(visible, api.frame_delta(hwnd), args)
        api.set_pos(hwnd, x, y, width, height)
    return ""


def _unchanged(action: str, args: dict, window: dict) -> bool:
    """True when the window does not look the way the action should have left it."""
    if action == "minimize":
        return not window["minimized"]
    if action == "restore":
        return window["minimized"] or window["maximized"]
    if action == "fullscreen":
        return window["maximized"] == (args.get("enabled") is False)
    if action == "move":
        want = {"x": window["x"], "y": window["y"], "width": window["w"], "height": window["h"]}
        return any(abs(want[k] - args[k]) > _TOLERANCE for k in want if k in args)
    return False


def _call(action: str, args: dict) -> dict:
    api = _api()
    screens = api.screens()
    windows = api.windows()
    _attach_outputs(windows, screens)
    if action == "list":
        return {"ok": True, "windows": windows, "screens": screens}
    found = _pick(windows, args["window"])
    if not found:
        return {"ok": False, "error": "no window matches"}
    if len(found) > 1:
        return {"ok": False, "error": "more than one window matches", "matches": found}
    target = found[0]
    note = _apply(api, target["hwnd"], action, args)
    # Minimizing, maximizing and moving animate, so read the window again.
    time.sleep(_SETTLE)
    settled = api.info(target["hwnd"])
    _attach_outputs([settled], screens)
    if not note and _unchanged(action, args, settled):
        note = (
            "the window did not change as asked. If it belongs to an elevated program, "
            "Windows ignores a client that is not elevated; run this client elevated."
        )
    return {"ok": True, "window": settled, "note": note}


async def execute(name: str, tool_input: dict) -> str:
    if name not in _NAMES:
        return f"Error: unknown tool {name!r}"
    action, args, error = _validate(tool_input)
    if error:
        return error
    reason = available()
    if reason:
        return f"Error: desktop_window cannot run: {reason}"
    try:
        result = await asyncio.to_thread(_call, action, args)
    except Exception as e:
        return f"Error: desktop_window failed ({type(e).__name__}: {e})."
    return _render(action, result)

