"""Browser lifecycle for core/browser.py: launch modes, profiles, downloads.

Modes (Chromium only):
  headless  no window. Installed Google Chrome, else Microsoft Edge, when present
            (user agent corrected), else bundled Chromium.
  headed    visible window on the user's desktop.
  virtual   the same installed Chrome as `real`, started on a hidden desktop of
            its own, so no window appears on the user's desktop. Needs Google
            Chrome or Microsoft Edge installed.
  real      installed Chrome or Edge started as a normal program and attached over CDP
            on 127.0.0.1. No automation flags: the least detectable.

`profile` names a persistent profile directory, so cookies and logins survive.
Without it the session's profile is deleted when it closes. Profiles live under
%LOCALAPPDATA%\\researchmesh\\browser-profiles, inside the user's own profile
folder.
"""

import asyncio
import contextlib
import ctypes
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

MODES = ("headless", "headed", "virtual", "real")
_CDP_MODES = ("virtual", "real")
_VERSION_DIR = re.compile(r"\d+\.\d+\.\d+\.\d+")
# Playwright launches set navigator.webdriver to true; this turns it off.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
DOWNLOAD_DIR = Path(os.environ.get("RESEARCHMESH_DOWNLOAD_DIR", "~/Downloads")).expanduser()
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
_PARTIAL = (".crdownload", ".tmp")


def profile_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or "~/AppData/Local").expanduser()
    return base / "researchmesh" / "browser-profiles"


def profile_dir(name: str) -> Path:
    if not PROFILE_NAME.fullmatch(name):
        raise ValueError("profile must be 1-40 characters: letters, digits, '-' or '_'")
    path = profile_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_chrome() -> str | None:
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            path = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if path.is_file():
                return str(path)
    return None


def find_edge() -> str | None:
    """Microsoft Edge, which every Windows install has. It lives in Program Files
    (x86) even on 64-bit Windows."""
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            path = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if path.is_file():
                return str(path)
    return None


def find_browser() -> str | None:
    """The installed browser the tool drives: Google Chrome, else Microsoft Edge."""
    return find_chrome() or find_edge()


def _is_edge(path: str) -> bool:
    return Path(path).name.lower() == "msedge.exe"


def _browser_name(path: str) -> str:
    return "Edge" if _is_edge(path) else "Chrome"


def _chrome_version(chrome: str) -> str | None:
    """The browser's version: the numbered folder it installs beside its .exe, since
    `chrome.exe --version` prints nothing on Windows. Falls back to asking it."""
    try:
        folders = [p.name for p in Path(chrome).parent.iterdir() if p.is_dir() and _VERSION_DIR.fullmatch(p.name)]
    except OSError:
        folders = []
    if folders:
        return max(folders, key=lambda v: tuple(int(n) for n in v.split(".")))
    try:
        out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_DIR.search(out)
    return match.group(0) if match else None


def _chrome_user_agent(chrome: str) -> str | None:
    """The user agent a normal Chrome or Edge sends, without the 'Headless' marker."""
    version = _chrome_version(chrome)
    if version is None:
        return None
    major = version.split(".")[0]
    suffix = f" Edg/{major}.0.0.0" if _is_edge(chrome) else ""
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36{suffix}"
    )


_HIDDEN_FLAGS = [
    # A window on another desktop counts as occluded; keep its pages from being throttled.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
]
# Fixed-width types, so the structure sizes below can be checked on any platform.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_HANDLE = ctypes.c_void_p


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p), ("dwX", _DWORD), ("dwY", _DWORD), ("dwXSize", _DWORD),
        ("dwYSize", _DWORD), ("dwXCountChars", _DWORD), ("dwYCountChars", _DWORD),
        ("dwFillAttribute", _DWORD), ("dwFlags", _DWORD), ("wShowWindow", _WORD),
        ("cbReserved2", _WORD), ("lpReserved2", ctypes.c_void_p), ("hStdInput", _HANDLE),
        ("hStdOutput", _HANDLE), ("hStdError", _HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", _HANDLE), ("hThread", _HANDLE), ("dwProcessId", _DWORD), ("dwThreadId", _DWORD)]


class _HiddenProcess:
    """A Chrome process on a desktop of its own: no window on the user's desktop
    and no taskbar button."""

    def __init__(self, kernel32: Any, user32: Any, process: int, desktop: int, pid: int) -> None:
        self._kernel32, self._user32 = kernel32, user32
        self._process, self._desktop = process, desktop
        self.pid = pid

    def alive(self) -> bool:
        return bool(self._kernel32.WaitForSingleObject(self._process, 0) == 0x102)  # WAIT_TIMEOUT

    def terminate(self) -> None:
        _kill_tree(self.pid)
        self._kernel32.TerminateProcess(self._process, 1)

    def close(self) -> None:
        self._kernel32.CloseHandle(self._process)
        self._user32.CloseDesktop(self._desktop)


def hidden_unavailable() -> str | None:
    """Why a hidden Chrome cannot be started here, or None."""
    if sys.platform != "win32":
        return "virtual mode runs on Windows only"
    if find_browser() is None:
        return "virtual mode needs Google Chrome or Microsoft Edge installed"
    return None


def _launch_hidden(command: list[str]) -> Any:
    """Start `command` on a new desktop of the user's window station. Nothing is
    drawn on the desktop the user is looking at."""
    if sys.platform != "win32":
        raise RuntimeError("a hidden desktop needs Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.restype = ctypes.c_void_p
    user32.CreateDesktopW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, _DWORD, _DWORD, ctypes.c_void_p,
    ]
    user32.CloseDesktop.argtypes = [_HANDLE]
    kernel32.CreateProcessW.restype = ctypes.c_int
    kernel32.CreateProcessW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, _DWORD,
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.WaitForSingleObject.restype = _DWORD
    kernel32.WaitForSingleObject.argtypes = [_HANDLE, _DWORD]
    kernel32.TerminateProcess.argtypes = [_HANDLE, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [_HANDLE]
    name = f"rm-hidden-{uuid.uuid4().hex[:12]}"
    desktop = user32.CreateDesktopW(name, None, None, 0, 0x10000000, None)  # GENERIC_ALL
    if not desktop:
        raise OSError(f"CreateDesktopW failed (error {ctypes.get_last_error()})")
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    startup.lpDesktop = f"WinSta0\\{name}"
    info = _PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    created = kernel32.CreateProcessW(
        None, command_line, None, None, 0, 0x00000200, None, None, ctypes.byref(startup), ctypes.byref(info)
    )  # CREATE_NEW_PROCESS_GROUP
    if not created:
        error = ctypes.get_last_error()
        user32.CloseDesktop(desktop)
        raise OSError(f"CreateProcessW failed (error {error})")
    kernel32.CloseHandle(info.hThread)
    return _HiddenProcess(kernel32, user32, info.hProcess, desktop, info.dwProcessId)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed_preferences(pdir: Path) -> None:
    """A new profile starts with password saving off, so no save prompt appears."""
    prefs = pdir / "Default" / "Preferences"
    if prefs.exists():
        return
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps({
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False},
    }))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(str(path))


def _downloads_present() -> set[str]:
    return {p.name for p in DOWNLOAD_DIR.iterdir()} if DOWNLOAD_DIR.is_dir() else set()


def _taskkill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)


def _kill_tree(pid: int) -> None:
    """End a browser and every process it started. TerminateProcess ends only the
    one process, and a browser's renderers and helpers can outlive it."""
    if sys.platform == "win32":
        _taskkill(pid)


def _clear_readonly(path: Path) -> None:
    """Windows will not delete a read-only file, so clear the flag first. Only
    files are touched: elsewhere a directory without its search bit is unreadable."""
    for item in path.rglob("*"):
        if item.is_file():
            with contextlib.suppress(OSError):
                os.chmod(item, stat.S_IWRITE | stat.S_IREAD)


class Session:
    def __init__(self, mode: str, profile: str | None):
        self.mode = mode
        self.profile = profile
        self.detail = ""
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.opened_pages: list[Any] = []
        self._procs: list[asyncio.subprocess.Process] = []
        self._hidden: Any = None
        self._tmpdir: Path | None = None
        self._staging: Path | None = None
        self._tasks: set[asyncio.Task] = set()
        self._known_downloads: set[str] = _downloads_present()

    def describe(self) -> str:
        profile = f", profile {self.profile!r}" if self.profile else ""
        return f"{self.mode} ({self.detail}{profile})"

    async def first_page(self):
        return self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _open_playwright(self) -> None:
        chrome = find_browser()
        args = list(LAUNCH_ARGS)
        launch: dict = {"headless": self.mode == "headless", "args": args}
        if chrome:
            launch["channel"] = "msedge" if _is_edge(chrome) else "chrome"
        context_opts: dict = {"accept_downloads": True}
        if self.mode == "headless" and chrome:
            agent = await asyncio.to_thread(_chrome_user_agent, chrome)
            if agent:
                context_opts["user_agent"] = agent
        self.detail = f"installed {_browser_name(chrome)}" if chrome else "bundled Chromium"
        chromium = self.playwright.chromium
        if self.profile:
            self.context = await chromium.launch_persistent_context(
                str(profile_dir(self.profile)), **launch, **context_opts
            )
        else:
            self.browser = await chromium.launch(**launch)
            self.context = await self.browser.new_context(**context_opts)

    async def _open_real(self) -> None:
        chrome = find_browser()
        if not chrome:
            raise RuntimeError(f"{self.mode} mode needs Google Chrome or Microsoft Edge installed")
        name = _browser_name(chrome)
        hidden = self.mode == "virtual"
        if hidden:
            reason = hidden_unavailable()
            if reason:
                raise RuntimeError(reason)
        if self.profile:
            pdir = profile_dir(self.profile)
        else:
            self._tmpdir = pdir = Path(tempfile.mkdtemp(prefix="rm-chrome-"))
        _seed_preferences(pdir)
        # A port chosen in advance, not 0: Chrome treats --remote-debugging-port=0
        # as automation and sets navigator.webdriver to true.
        port = _free_port()
        flags = [
            f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={pdir}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1400,950",
        ]
        proc = None
        if hidden:
            self._hidden = await asyncio.to_thread(_launch_hidden, [chrome, *flags, *_HIDDEN_FLAGS, "about:blank"])
        else:
            proc = await asyncio.create_subprocess_exec(
                chrome, *flags, "about:blank",
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._procs.append(proc)
        for _ in range(60):
            exited = (not self._hidden.alive()) if hidden else (proc is not None and proc.returncode is not None)
            if exited:
                raise RuntimeError(
                    f"{name} exited at startup; the profile may already be open in another {name}"
                )
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                break
            except Exception:
                await asyncio.sleep(0.4)
        else:
            raise RuntimeError(f"{name} did not open its debug port")
        self.context = self.browser.contexts[0]
        # Chrome overwrites a same-name file when told where to save, so it
        # writes to a private staging dir and new_downloads() moves each
        # finished file into DOWNLOAD_DIR under a unique name.
        self._staging = Path(tempfile.mkdtemp(prefix="rm-dl-"))
        cdp = await self.browser.new_browser_cdp_session()
        await cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": str(self._staging), "eventsEnabled": True,
        })
        self.detail = f"installed {name}, on a hidden desktop, over CDP" if hidden else f"installed {name} over CDP"

    def _watch_downloads(self, page) -> None:
        """Playwright-launched browsers deliver downloads as events; a CDP-attached
        Chrome writes them straight into DOWNLOAD_DIR."""
        if self.mode in _CDP_MODES:
            return

        def on_download(download) -> None:
            task = asyncio.ensure_future(_save_download(download))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        page.on("download", on_download)

    def _watch_pages(self) -> None:
        def on_page(page) -> None:
            self.opened_pages.append(page)
            self._watch_downloads(page)

        self.context.on("page", on_page)
        for page in self.context.pages:
            self._watch_downloads(page)

    async def new_downloads(self, wait: float = 8.0) -> list[Path]:
        """Files that appeared in DOWNLOAD_DIR since the last call. Waits while
        one is still being written."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=wait)
        await self._collect_staged(deadline)
        while True:
            fresh = _downloads_present() - self._known_downloads
            partial = [n for n in fresh if n.endswith(_PARTIAL)]
            if not partial or loop.time() >= deadline:
                break
            await asyncio.sleep(0.4)
        done = sorted(n for n in fresh if not n.endswith(_PARTIAL))
        self._known_downloads |= set(done)
        return [DOWNLOAD_DIR / n for n in done]

    async def _collect_staged(self, deadline: float) -> None:
        """Move finished files from the staging dir into DOWNLOAD_DIR."""
        if self._staging is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            names = [p.name for p in self._staging.iterdir()]
            if not any(n.endswith(_PARTIAL) for n in names) or loop.time() >= deadline:
                break
            await asyncio.sleep(0.4)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for name in names:
            if not name.endswith(_PARTIAL):
                shutil.move(str(self._staging / name), _unique(DOWNLOAD_DIR / name))

    async def close(self) -> None:
        if self.mode in _CDP_MODES and self.browser is not None:
            with contextlib.suppress(Exception):
                await (await self.browser.new_browser_cdp_session()).send("Browser.close")
        for closer in (self.context, self.browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
        for proc in self._procs:
            if proc.returncode is None:
                _kill_tree(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
        if self._hidden is not None:
            for _ in range(20):
                if not self._hidden.alive():
                    break
                await asyncio.sleep(0.25)
            else:
                self._hidden.terminate()
            self._hidden.close()
        for leftover in (self._tmpdir, self._staging):
            if leftover is not None:
                # The browser's helpers can hold files for a few seconds after it exits.
                for attempt in range(15):
                    shutil.rmtree(leftover, ignore_errors=True)
                    if not leftover.exists():
                        break
                    if attempt:
                        _clear_readonly(leftover)
                    await asyncio.sleep(0.4)


async def _save_download(download) -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(download.suggested_filename).name or "download"
    await download.save_as(_unique(DOWNLOAD_DIR / name))


async def open_session(mode: str, profile: str | None) -> Session:
    from playwright.async_api import async_playwright

    session = Session(mode, profile)
    try:
        session.playwright = await async_playwright().start()
        if mode in _CDP_MODES:
            await session._open_real()
        else:
            await session._open_playwright()
        session._watch_pages()
    except BaseException:
        await session.close()
        raise
    return session
