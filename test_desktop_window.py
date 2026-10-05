"""Tests for desktop_window.

    python test_desktop_window.py

The Win32 calls are replaced by a fake that models what matters to the logic:
Windows' invisible resize borders, a foreground lock that refuses some focus
attempts, and a window that ignores changes (an elevated program's). These
cover this module's logic. They cannot show what the real Win32 calls return,
so listing, focusing and moving still need a hands-on run on Windows.
"""

import asyncio
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import desktop_window as dw

FAILURES: list[str] = []
dw._SETTLE = 0
REAL_AVAILABLE = dw.available
dw.available = lambda: None

SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


SCREENS = [{"name": r"\\.\DISPLAY1", "x": 0, "y": 0, "w": 1920, "h": 1080},
           {"name": r"\\.\DISPLAY2", "x": 1920, "y": 0, "w": 1280, "h": 1024}]


def record(hwnd: int, title: str, program: str, x=100, y=100, w=800, h=600, **flags) -> dict:
    return {"id": f"0x{hwnd:x}", "hwnd": hwnd, "title": title, "cls": program, "pid": hwnd, "x": x, "y": y, "w": w, "h": h,
            "output": "", "minimized": False, "maximized": False, "active": False, "other_desktop": False, **flags}


class FakeApi:
    """Windows with invisible borders: the outer rectangle extends `delta` past the visible frame."""

    def __init__(self) -> None:
        self.delta = (7, 0, 7, 7)
        self.win = {
            0x100: record(0x100, "Notes - Notepad", "notepad.exe"),
            0x200: record(0x200, "Wikipedia - Chrome", "chrome.exe", x=1950, y=40, w=900, h=700),
            0x201: record(0x201, "Inbox - Chrome", "chrome.exe", x=300, y=300, w=700, h=500),
            0x300: record(0x300, "zsh - Terminal", "WindowsTerminal.exe", x=50, y=50, w=640, h=400, minimized=True),
            0x400: record(0x400, "Budget - Word", "winword.exe", x=200, y=200, w=700, h=500, other_desktop=True),
        }
        self.fg = 0x100
        self.log: list[tuple] = []
        self.deny: set[str] = set()  # focus steps Windows refuses
        self.ignore: set[str] = set()  # calls an elevated program's window ignores

    def screens(self):
        return [dict(s) for s in SCREENS]

    def windows(self):
        return [self.info(h) for h in self.win]

    def info(self, hwnd):
        return {**self.win[hwnd], "active": self.fg == hwnd}

    def is_iconic(self, hwnd):
        return self.win[hwnd]["minimized"]

    def is_zoomed(self, hwnd):
        return self.win[hwnd]["maximized"]

    def show(self, hwnd, command):
        self.log.append(("show", hwnd, command))
        if "show" in self.ignore:
            return
        w = self.win[hwnd]
        if command == SW_MINIMIZE:
            w["minimized"] = True
        elif command == SW_MAXIMIZE:
            w["maximized"], w["minimized"] = True, False
        elif command == SW_RESTORE:
            w["maximized"], w["minimized"] = False, False

    def bounds(self, hwnd):
        w = self.win[hwnd]
        return w["x"], w["y"], w["w"], w["h"]

    def frame_delta(self, hwnd):
        return self.delta

    def set_pos(self, hwnd, x, y, width, height):
        self.log.append(("set_pos", hwnd, x, y, width, height))
        left, top, right, bottom = self.delta
        w = self.win[hwnd]
        w["x"], w["y"], w["w"], w["h"] = x + left, y + top, width - left - right, height - top - bottom

    def foreground(self):
        return self.fg

    def _focus(self, name, hwnd):
        self.log.append((name, hwnd))
        if name not in self.deny:
            self.fg = hwnd

    def set_foreground(self, hwnd):
        self._focus("set_foreground", hwnd)

    def set_foreground_attached(self, hwnd):
        self._focus("set_foreground_attached", hwnd)

    def set_foreground_after_alt(self, hwnd):
        self._focus("set_foreground_after_alt", hwnd)


async def run(api: FakeApi, **tool_input) -> str:
    dw._api = lambda: api
    return await dw.execute("desktop_window", tool_input)


def state(api: FakeApi, hwnd: int) -> dict:
    return api.info(hwnd)


async def main_async() -> None:
    print("arguments")
    v = dw._validate

    def err(request: dict) -> str:
        return v(request)[2] or ""

    check("list needs nothing", v({"action": "list"}) == ("list", {}, None))
    check("unknown action", err({"action": "close"}).startswith("Error: `action` must be one of"))
    check("activate needs a window", err({"action": "activate"}).startswith("Error: this action needs `window`"))
    check("blank window is refused", v({"action": "activate", "window": "  "})[2] is not None)
    check("move needs geometry", err({"action": "move", "window": "x"}).startswith("Error: `move` needs"))
    check("move rejects a non-integer", err({"action": "move", "window": "x", "x": "1"}) == "Error: `x` must be an integer")
    check("move rejects a bool", err({"action": "move", "window": "x", "x": True}) == "Error: `x` must be an integer")
    check("move rejects zero width", err({"action": "move", "window": "x", "width": 0}) == "Error: `width` must be at least 1")
    check("move keeps only the given fields", v({"action": "move", "window": " w ", "x": 5})[1] == {"window": "w", "x": 5})
    check("fullscreen enabled must be a bool", err({"action": "fullscreen", "window": "x", "enabled": "no"}) == "Error: `enabled` must be true or false")

    print("the Win32 structures and the platform")
    check("RECT is 16 bytes", ctypes.sizeof(dw._RECT) == 16)
    info = dw._MONITORINFOEXW
    check("MONITORINFOEXW is 104 bytes with its fields where Windows expects them",
          ctypes.sizeof(info) == 104 and (info.rcMonitor.offset, info.rcWork.offset, info.dwFlags.offset, info.szDevice.offset) == (4, 20, 36, 40),
          str((ctypes.sizeof(info), info.rcMonitor.offset, info.rcWork.offset, info.dwFlags.offset, info.szDevice.offset)))
    if sys.platform != "win32":
        check("the tool is unavailable off Windows", REAL_AVAILABLE() == "it runs on Windows only")
        try:
            dw._Win32()
            check("the Win32 layer refuses to start off Windows", False)
        except RuntimeError as e:
            check("the Win32 layer refuses to start off Windows", "needs Windows" in str(e), str(e))

    print("the rectangle SetWindowPos needs")
    check("the invisible borders are added around the visible frame", dw._outer_rect((100, 100, 800, 600), (7, 0, 7, 7), {}) == (93, 100, 814, 607))
    check("only the given fields change", dw._outer_rect((100, 100, 800, 600), (7, 0, 7, 7), {"x": 50}) == (43, 100, 814, 607))
    check("all four fields", dw._outer_rect((100, 100, 800, 600), (7, 0, 7, 7), {"x": 0, "y": 10, "width": 500, "height": 300}) == (-7, 10, 514, 307))
    check("no borders means the same rectangle", dw._outer_rect((1, 2, 3, 4), (0, 0, 0, 0), {}) == (1, 2, 3, 4))

    print("matching and output")
    ws = [record(0x100, "Notes - Notepad", "notepad.exe"), record(0x200, "Wikipedia - Chrome", "chrome.exe"), record(0x201, "Inbox - Chrome", "chrome.exe")]
    check("an id matches exactly, in either case", [w["hwnd"] for w in dw._pick(ws, "0x200")] == [0x200] and [w["hwnd"] for w in dw._pick(ws, "0X200")] == [0x200])
    check("an id is not a substring match", dw._pick(ws, "0x20") == [])
    check("a title part matches", [w["hwnd"] for w in dw._pick(ws, "wiki")] == [0x200])
    check("a program name matches all its windows", [w["hwnd"] for w in dw._pick(ws, "CHROME")] == [0x200, 0x201])
    placed = [record(1, "a", "A", x=2000, y=0, w=200, h=200), record(2, "b", "B", x=50, y=50, w=100, h=100), record(3, "c", "C", x=9000, y=9000)]
    dw._attach_outputs(placed, SCREENS)
    check("a window is placed on the monitor under its centre", [w["output"] for w in placed] == [r"\\.\DISPLAY2", r"\\.\DISPLAY1", ""])
    text = dw._render("list", {"ok": True, "screens": SCREENS, "windows": [record(0x100, "Notes", "notepad.exe", active=True, output=r"\\.\DISPLAY1")]})
    check("list shows monitors and windows", text.startswith("Screens: \\\\.\\DISPLAY1 0,0 1920x1080; \\\\.\\DISPLAY2 1920,0 1280x1024\nWindows (1):") and "0x100" in text and "active" in text, text)
    check("a long title is cut", "..." in dw._row(record(1, "t" * 120, "A")))

    print("listing")
    api = FakeApi()
    out = await run(api, action="list")
    check("every window is listed with its monitor", out.count("\n") == 1 + len(api.win) and r"\\.\DISPLAY2" in out and "notepad.exe" in out, out)
    check("listing changes nothing", api.log == [])

    print("focus")
    api = FakeApi()
    out = await run(api, action="activate", window="terminal")
    check("a minimized window is restored, then focused", api.log == [("show", 0x300, SW_RESTORE), ("set_foreground", 0x300)], str(api.log))
    check("it is reported active, with no note", out.startswith("activate done:") and "active" in out and "Note:" not in out, out)

    api = FakeApi()
    api.deny = {"set_foreground"}
    out = await run(api, action="activate", window="inbox")
    check("when the plain call is refused, sharing the foreground thread's input is tried", [e[0] for e in api.log] == ["set_foreground", "set_foreground_attached"] and "Note:" not in out, str(api.log))

    api = FakeApi()
    api.deny = {"set_foreground", "set_foreground_attached"}
    out = await run(api, action="activate", window="inbox")
    check("a tap of Alt is the last resort", [e[0] for e in api.log] == ["set_foreground", "set_foreground_attached", "set_foreground_after_alt"], str(api.log))

    api = FakeApi()
    api.deny = {"set_foreground", "set_foreground_attached", "set_foreground_after_alt"}
    out = await run(api, action="activate", window="inbox")
    check("when Windows refuses every attempt the reply says so", "Note: Windows did not let this process take the focus" in out and "active" not in out.split("Note:")[0].split("\n")[1], out)

    print("minimize, restore, maximize")
    api = FakeApi()
    await run(api, action="minimize", window="notes")
    check("minimize", state(api, 0x100)["minimized"] is True)
    out = await run(api, action="restore", window="notes")
    check("restore un-minimizes", state(api, 0x100)["minimized"] is False and "Note:" not in out, out)
    await run(api, action="fullscreen", window="notes")
    check("fullscreen maximizes", state(api, 0x100)["maximized"] is True and ("show", 0x100, SW_MAXIMIZE) in api.log)
    await run(api, action="fullscreen", window="notes", enabled=False)
    check("fullscreen with enabled false un-maximizes", state(api, 0x100)["maximized"] is False)
    api.log.clear()
    await run(api, action="fullscreen", window="notes", enabled=False)
    check("un-maximizing a window that is not maximized does nothing", api.log == [], str(api.log))
    await run(api, action="fullscreen", window="notes")
    await run(api, action="restore", window="notes")
    check("restore also un-maximizes", state(api, 0x100)["maximized"] is False)

    print("move")
    api = FakeApi()
    await run(api, action="move", window="notes", x=300)
    check("move with only x keeps y and the size, as the visible frame", api.bounds(0x100) == (300, 100, 800, 600), str(api.bounds(0x100)))
    check("SetWindowPos got the outer rectangle, borders included", api.log[-1] == ("set_pos", 0x100, 293, 100, 814, 607), str(api.log[-1]))
    await run(api, action="move", window="notes", width=500, height=300)
    check("a size change keeps the position", api.bounds(0x100) == (300, 100, 500, 300), str(api.bounds(0x100)))
    await run(api, action="fullscreen", window="notes")
    await run(api, action="move", window="notes", x=10, y=20)
    check("moving a maximized window restores it first", state(api, 0x100)["maximized"] is False and api.bounds(0x100)[:2] == (10, 20), str(api.bounds(0x100)))
    api.win[0x300]["minimized"] = True
    out = await run(api, action="move", window="terminal", x=5, y=5)
    check("moving a minimized window restores it first", state(api, 0x300)["minimized"] is False and "Note:" not in out, out)

    print("a window that ignores the change")
    api = FakeApi()
    api.ignore = {"show"}
    out = await run(api, action="minimize", window="notes")
    check("the reply says the window did not change and why", "Note: the window did not change as asked" in out and "elevated" in out, out)
    api.ignore = set()
    check("a change that worked has no note", "Note:" not in await run(api, action="minimize", window="notes"))

    print("windows on other virtual desktops")
    check("a shell-cloaked window is on another desktop", dw._desktop_state(0x2) == "other")
    check("a window the user can see is here", dw._desktop_state(0) == "here")
    check("a window cloaked by its app or inherited is left out", dw._desktop_state(0x1) is None and dw._desktop_state(0x4) is None)
    check("any combination with another cloak reason is left out", dw._desktop_state(0x3) is None and dw._desktop_state(0x6) is None)
    api = FakeApi()
    out = await run(api, action="list")
    word_row = next(line for line in out.splitlines() if "winword.exe" in line)
    check("a window on another desktop is listed with its flag", "other-desktop" in word_row, word_row)
    check("windows on this desktop carry no such flag", all("other-desktop" not in line for line in out.splitlines() if "winword.exe" not in line))
    out = await run(api, action="activate", window="budget")
    check("asking for it works like any other window", out.startswith("activate done:") and "Note:" not in out and api.fg == 0x400, out)
    api = FakeApi()
    api.deny = {"set_foreground", "set_foreground_attached", "set_foreground_after_alt"}
    out = await run(api, action="activate", window="budget")
    check("when Windows refuses, the note says the window is on another virtual desktop", "Note: Windows did not let this process take the focus" in out and "another virtual desktop" in out, out)
    api = FakeApi()
    api.deny = {"set_foreground", "set_foreground_attached", "set_foreground_after_alt"}
    out = await run(api, action="activate", window="inbox")
    check("a window on this desktop gets no desktop advice", "virtual desktop" not in out, out)

    print("no match and too many")
    api = FakeApi()
    before = {h: dict(w) for h, w in api.win.items()}
    out = await run(api, action="minimize", window="nothing")
    check("no match is an error and acts on nothing", out == "Error: no window matches" and api.log == [], out)
    out = await run(api, action="minimize", window="chrome")
    check("an ambiguous name lists the candidates and acts on nothing", out.startswith("Error: more than one window matches\n") and out.count("\n") == 2 and api.log == [] and api.win == before, out)

    print("failures")
    class Broken(FakeApi):
        def windows(self):
            raise OSError("access denied")

    out = await run(Broken(), action="list")
    check("an error from the Win32 layer is reported, not raised", out.startswith("Error: desktop_window failed (OSError: access denied)"), out)
    saved = dw.available
    dw.available = lambda: "it runs on Windows only"
    check("an unavailable platform is named", (await dw.execute("desktop_window", {"action": "list"})).startswith("Error: desktop_window cannot run: it runs on Windows only"))
    dw.available = saved


def main() -> int:
    asyncio.run(main_async())
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
