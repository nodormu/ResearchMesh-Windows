"""Behavioural tests for browser modes, profiles, tabs and downloads.

    python test_browser_mode.py

Drives the real `browser.execute` against a local server. The headed and real
steps open a window on the desktop, so they run only on Windows from a signed-in
session, and the real steps also need Google Chrome installed. The virtual steps
run wherever Chrome is found: on Windows they use a hidden desktop; elsewhere a
stand-in for the launch starts the same Chrome headless with the same flags,
which covers the launch, attach and shutdown logic but not what Windows does
with a window on another desktop.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORK = Path(tempfile.mkdtemp(prefix="rm-browser-test-"))
os.environ["RESEARCHMESH_DOWNLOAD_DIR"] = str(WORK / "downloads")

FAILURES: list[str] = []
PAGES = {
    "/": b'<title>home</title><a id="tab" href="/second" target="_blank">s</a>'
         b'<a id="dl" href="/file">f</a>',
    "/second": b"<title>second</title>second page",
    "/wall": b"<title>Just a moment...</title>checking",
    "/solved": b'<title>c</title><input name="cf-turnstile-response" value="">'
               b"<script>setTimeout(()=>{document.querySelector('[name=cf-turnstile-response]')"
               b".value='t'.repeat(40)},700)</script>",
    "/pending": b'<title>p</title><input name="cf-turnstile-response" value="">',
    "/otp": b'<title>otp</title><form action="/done" method="get"><input id="code" name="code"></form>',
    "/done": b"<title>done</title>verified",
}


SERVED = [0]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/file":
            SERVED[0] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="x.bin"')
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(f"{SERVED[0]:010d}".encode())
            return
        body = PAGES.get(self.path.split("?")[0], PAGES["/"])
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


async def js(mod, expression: str):
    return await mod._page.evaluate(expression)


class _StandInHidden:
    """Starts Chrome headless with the flags it was given, standing in for a hidden desktop."""

    def __init__(self, command: list[str]) -> None:
        import subprocess

        flags = [c for c in command[1:]]
        self._proc = subprocess.Popen([command[0], "--headless=new", *flags], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pid = self._proc.pid

    def alive(self) -> bool:
        return self._proc.poll() is None

    def terminate(self) -> None:
        self._proc.kill()

    def close(self) -> None:
        self._proc.wait(timeout=10)


def prepare_hidden(sess, mod, chrome) -> bool:
    """Make a hidden launch possible. On Windows it is the real thing. Elsewhere,
    replace the launch with a stand-in that starts the same Chrome headless."""
    if chrome is None:
        return False
    if sys.platform == "win32":
        return True
    sess._launch_hidden = _StandInHidden
    sess.hidden_unavailable = lambda: None
    mod.hidden_unavailable = lambda: None
    return True


def windows_pieces(sess) -> None:
    """The parts of the Windows code that need no Windows: path lookup, version
    detection, the user agent and the Win32 structure layouts."""
    import ctypes

    print("Chrome lookup, version and user agent")
    saved = {v: os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")}
    try:
        for v in saved:
            os.environ.pop(v, None)
        check("no install is None", sess.find_chrome() is None)
        app = WORK / "pf" / "Google" / "Chrome" / "Application"
        app.mkdir(parents=True)
        (app / "chrome.exe").write_bytes(b"")
        os.environ["PROGRAMFILES(X86)"] = str(WORK / "pf")
        check("Program Files (x86) is searched", sess.find_chrome() == str(app / "chrome.exe"), str(sess.find_chrome()))
        os.environ["PROGRAMFILES"] = str(WORK / "none")
        check("a missing folder is skipped", sess.find_chrome() == str(app / "chrome.exe"))
        for name in ("99.0.1.2", "131.0.6778.86", "130.9.9.9"):
            (app / name).mkdir()
        (app / "Locales").mkdir()
        check("the version is the highest numbered folder, compared as numbers", sess._chrome_version(str(app / "chrome.exe")) == "131.0.6778.86", str(sess._chrome_version(str(app / "chrome.exe"))))
        agent = sess._chrome_user_agent(str(app / "chrome.exe")) or ""
        check("the user agent is the Windows one for that major version", "Windows NT 10.0; Win64; x64" in agent and "Chrome/131.0.0.0" in agent and "Headless" not in agent, agent)
        lone = WORK / "pf2" / "Google" / "Chrome" / "Application"
        lone.mkdir(parents=True)
        (lone / "chrome.exe").write_bytes(b"")
        check("no version folder and no answer from the binary is no user agent", sess._chrome_user_agent(str(lone / "chrome.exe")) is None)
        local = WORK / "local"
        (local / "Google" / "Chrome" / "Application").mkdir(parents=True)
        (local / "Google" / "Chrome" / "Application" / "chrome.exe").write_bytes(b"")
        for v in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            os.environ.pop(v, None)
        os.environ["LOCALAPPDATA"] = str(local)
        check("a per-user install is found", sess.find_chrome() == str(local / "Google" / "Chrome" / "Application" / "chrome.exe"))
        check("profiles live under LOCALAPPDATA", sess.profile_root() == local / "researchmesh" / "browser-profiles", str(sess.profile_root()))
    finally:
        for v, value in saved.items():
            if value is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = value

    print("Win32 structure layouts (64-bit Windows, from the documented definitions)")
    check("STARTUPINFOW is 104 bytes", ctypes.sizeof(sess._STARTUPINFOW) == 104, str(ctypes.sizeof(sess._STARTUPINFOW)))
    offsets = {n: getattr(sess._STARTUPINFOW, n).offset for n in ("lpDesktop", "dwFlags", "wShowWindow", "lpReserved2", "hStdInput", "hStdError")}
    check("the fields the launch sets are where Windows expects them", offsets == {"lpDesktop": 16, "dwFlags": 60, "wShowWindow": 64, "lpReserved2": 72, "hStdInput": 80, "hStdError": 96}, str(offsets))
    check("PROCESS_INFORMATION is 24 bytes", ctypes.sizeof(sess._PROCESS_INFORMATION) == 24, str(ctypes.sizeof(sess._PROCESS_INFORMATION)))
    if sys.platform != "win32":
        check("a hidden desktop is refused off Windows", sess.hidden_unavailable() == "virtual mode runs on Windows only")
        try:
            sess._launch_hidden(["x"])
            check("the Win32 launch refuses to run off Windows", False)
        except RuntimeError as e:
            check("the Win32 launch refuses to run off Windows", "needs Windows" in str(e), str(e))


async def main_async(mod, sess, base: str) -> None:
    chrome = sess.find_chrome()
    have_display = sys.platform == "win32"
    downloads = WORK / "downloads"
    hidden = prepare_hidden(sess, mod, chrome)

    print("default is headless, and the automation flag is off")
    out = await mod.execute("browser_navigate", {"url": base})
    check("navigate works", out.startswith("Title: home"), out)
    check("report names the mode", "Mode: headless" in out, out[:160])
    check("session mode is headless", mod._session.mode == "headless")
    check("navigator.webdriver is false", await js(mod, "navigator.webdriver") is False)
    agent = await js(mod, "navigator.userAgent")
    check("headless marker only without installed Chrome", ("Headless" in agent) == (chrome is None), agent)

    print("bad options are rejected without touching the browser")
    page_before = mod._page
    for label, call, expected in (
        ("string headed", {"headed": "false"}, "Error: `headed` must be true or false"),
        ("mode and headed", {"mode": "headed", "headed": True}, "Error: give `mode` or `headed`, not both"),
        ("unknown mode", {"mode": "stealth"}, "Error: `mode` must be one of headless, headed, virtual, real"),
        ("bad profile", {"profile": "../x"}, "Error: `profile` must be 1-40 characters: letters, digits, '-' or '_'"),
    ):
        out = await mod.execute("browser_navigate", {"url": base, **call})
        check(f"{label} is an error", out == expected, out)
    check("browser untouched", mod._page is page_before and mod._session.mode == "headless")

    print("the same mode keeps the page; a different one restarts the browser")
    await mod.execute("browser_navigate", {"url": base, "mode": "headless"})
    check("same mode, same page object", mod._page is page_before)

    print("human check line")
    out = await mod.execute("browser_navigate", {"url": base + "/solved"})
    check("a check that solves is reported solved", "Human check: solved." in out, out[:200])
    await mod.execute("browser_navigate", {"url": base + "/pending"})
    pending = await mod._human_check(mod._page, wait=0.5)
    check("a check that never solves is reported pending", pending.startswith("Human check: pending"), pending)
    await mod.execute("browser_navigate", {"url": base + "/wall"})
    wall = await mod._human_check(mod._page, wait=0.5)
    check("a challenge page is reported", wall.startswith("Human check: Cloudflare challenge page"), wall)
    out = await mod.execute("browser_navigate", {"url": base + "/second"})
    check("a page without a check has no line", "Human check" not in out, out[:200])

    print("a fresh default-mode visit stopped by a check is reopened in virtual mode")
    mod._CHECK_WAIT = 1.0
    await mod.shutdown()
    out = await mod.execute("browser_navigate", {"url": base + "/pending"})
    if hidden:
        check("reopened in virtual mode", "Mode: virtual" in out and "reopened from headless" in out, out[:260])
        check("the session is now virtual", mod._session.mode == "virtual")
        check("the hint points at real mode", "Navigate again with mode `real`" in out, out[:400])
    else:
        check("without Chrome it stays headless", "Mode: headless" in out and "Human check: pending" in out, out[:260])
    await mod.shutdown()
    out = await mod.execute("browser_navigate", {"url": base + "/pending", "mode": "headless"})
    check("an explicit mode is never reopened", "Mode: headless" in out and "reopened" not in out, out[:200])
    out = await mod.execute("browser_navigate", {"url": base + "/pending"})
    check("an open session is never reopened", "Mode: headless" in out and "reopened" not in out, out[:200])
    await mod.shutdown()
    out = await mod.execute("browser_navigate", {"url": base + "/second"})
    check("a page with no check stays headless", "Mode: headless" in out and "reopened" not in out, out[:200])
    saved_hidden = mod.hidden_unavailable
    mod.hidden_unavailable = lambda: "not available"
    try:
        check("no hidden launch means no reopen", await mod._reopen_virtual(base) is None)
    finally:
        mod.hidden_unavailable = saved_hidden
    check("and the open session is untouched", mod._session is not None and mod._session.mode == "headless")
    mod._CHECK_WAIT = 8.0

    print("a click that opens a tab switches to it; browser_tab lists, switches, closes")
    await mod.execute("browser_navigate", {"url": base})
    out = await mod.execute("browser_click", {"selector": "#tab"})
    check("new tab is followed", out.startswith("Clicked. Opened a new tab. Now on: second"), out[:120])
    listing = await mod.execute("browser_tab", {"action": "list"})
    rows = listing.splitlines()
    check("two tabs listed, second is current", len(rows) == 2 and "<- current" in rows[1], listing)
    out = await mod.execute("browser_tab", {"action": "switch", "index": 0})
    check("switch goes to tab 0", out.startswith("Switched to tab 0: home"), out[:80])
    out = await mod.execute("browser_tab", {"action": "close", "index": 1})
    check("close removes the other tab", out.startswith("Closed tab 1. Now on: home"), out[:80])
    check("one tab left", len(mod._session.context.pages) == 1)
    out = await mod.execute("browser_tab", {"action": "switch", "index": 9})
    check("bad index is an error", out.startswith("Error:"), out)

    print("downloads are saved and reported")
    out = await mod.execute("browser_click", {"selector": "#dl"})
    saved = downloads / "x.bin"
    check("report lists the file", f"Downloaded: {saved} (10 bytes)" in out, out[-160:])
    check("file content is right", saved.exists() and saved.read_bytes() == b"0000000001")

    print("browser_fill submit: a one-time code goes in and the next page comes back")
    await mod.execute("browser_navigate", {"url": base + "/otp"})
    out = await mod.execute("browser_fill", {"selector": "#code", "value": "123456", "submit": True})
    check("fill then Enter lands on the next page", out.startswith("Filled '#code', then pressed Enter. Now on: done"), out[:160])
    check("the form received the code", "code=123456" in out, out[:260])
    await mod.execute("browser_navigate", {"url": base + "/otp"})
    out = await mod.execute("browser_fill", {"selector": "#code", "value": "123456"})
    check("without submit it only fills", out == "Filled '#code'", out)
    out = await mod.execute("browser_fill", {"selector": "#code", "value": "1", "submit": "yes"})
    check("a non-boolean submit is refused", out == "Error: `submit` must be true or false", out)

    from core import chat

    check("the prompt allows a one-time code in chat and says to submit it",
          "one-time code" in chat.SYSTEM_PROMPT and "`submit: true`" in chat.SYSTEM_PROMPT)
    check("the prompt does not tell the user to type it themselves",
          "Never ask\nthe user to type it into the browser" in chat.SYSTEM_PROMPT)

    print("a profile keeps cookies across restarts")
    await mod.execute("browser_navigate", {"url": base, "profile": "tprof"})
    await js(mod, "document.cookie = 'k=v; max-age=3600'")
    await mod.shutdown()
    await mod.execute("browser_navigate", {"url": base, "profile": "tprof"})
    check("cookie survived the restart", "k=v" in await js(mod, "document.cookie"))
    await mod.execute("browser_navigate", {"url": base, "profile": "other"})
    check("another profile starts empty", "k=v" not in await js(mod, "document.cookie"))
    await mod.shutdown()
    await mod.execute("browser_navigate", {"url": base})
    check("no profile starts empty", "k=v" not in await js(mod, "document.cookie"))

    print("virtual mode: Chrome on a hidden desktop")
    if hidden:
        out = await mod.execute("browser_navigate", {"url": base, "mode": "virtual"})
        check("navigate works in virtual mode", out.startswith("Title: home") and "Mode: virtual" in out, out[:200])
        check("the report says the browser is hidden", "hidden" in out, out[:200])
        check("webdriver is false", await js(mod, "navigator.webdriver") is False)
        process = mod._session._hidden
        check("the hidden process is running", process is not None and process.alive())
        out = await mod.execute("browser_click", {"selector": "#dl"})
        check("downloads are staged and reported", "Downloaded:" in out, out[-200:])
        tmpdir = mod._session._tmpdir
        await mod.shutdown()
        check("the hidden Chrome exited on shutdown", not process.alive())
        check("its profile directory is removed", tmpdir is not None and not tmpdir.exists())
    else:
        print("  skipped: needs Google Chrome")

    if have_display:
        print("headed: true is the headed mode")
        out = await mod.execute("browser_navigate", {"url": base, "headed": True})
        check("navigate works headed", out.startswith("Title: home") and "Mode: headed" in out, out[:160])
        check("session mode is headed", mod._session.mode == "headed")
        agent = await js(mod, "navigator.userAgent")
        check("user agent has no headless marker", "Headless" not in agent, agent)

        if chrome:
            print("real mode: installed Chrome over CDP")
            await mod.execute("browser_navigate", {"url": base, "mode": "headless"})
            out = await mod.execute("browser_navigate", {"url": base, "mode": "real", "profile": "treal"})
            check("navigate works in real mode", out.startswith("Title: home") and "Mode: real" in out, out[:200])
            check("webdriver is false", await js(mod, "navigator.webdriver") is False)
            out = await mod.execute("browser_click", {"selector": "#dl"})
            check("download reported over CDP", "Downloaded:" in out and "x (1).bin" in out, out[-200:])
            check("an earlier file of the same name is not overwritten", saved.read_bytes() == b"0000000001")
            check("the new file has its own content", (downloads / "x (1).bin").read_bytes() == b"0000000002")
            proc = mod._session._procs[0]
            await mod.shutdown()
            check("Chrome exited on shutdown", proc.returncode is not None)
            prefs = json.loads((sess.profile_root() / "treal" / "Default" / "Preferences").read_text())
            check("new profile has password saving off", prefs.get("credentials_enable_service") is False)
            await mod.execute("browser_navigate", {"url": base, "mode": "real"})
            tmpdir = mod._session._tmpdir
            check("a profile-less real session uses a temporary dir", tmpdir is not None and tmpdir.exists())
            await mod.shutdown()
            check("temporary dir removed on shutdown", tmpdir is not None and not tmpdir.exists())
    else:
        print("no display available: headed and real steps skipped")

    print("a failed launch is reported and the next call recovers")
    saved_find = sess.find_chrome
    sess.find_chrome = lambda: None
    try:
        out = await mod.execute("browser_navigate", {"url": base, "mode": "real"})
    finally:
        sess.find_chrome = saved_find
    check("error is reported", out.startswith("Browser error in browser_navigate"), out[:120])
    check("no half-started browser is left", mod._session is None and mod._page is None)
    out = await mod.execute("browser_navigate", {"url": base})
    check("next call relaunches headless", out.startswith("Title: home") and mod._session.mode == "headless", out)

    await mod.shutdown()


def main() -> int:
    import core.browser as mod
    import core.browser_session as sess

    windows_pieces(sess)
    sess.profile_root = lambda: WORK / "cache" / "researchmesh" / "browser-profiles"

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asyncio.run(main_async(mod, sess, f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        shutil.rmtree(WORK, ignore_errors=True)
    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
