"""Persistent PowerShell — a real pwsh process that survives across tool calls.

The `powershell` tool (core/powershell.py) spawns a fresh process every call, so
`cd`, `$env:` changes, and anything a script defines (variables, functions,
imported modules) all die at the end of that call. This module keeps ONE
`pwsh` process alive for the life of the session instead — same singleton
pattern core/kernel.py uses for the IPython kernel — driven over a real
Windows pseudo-console (ConPTY) via `pywinpty`, the same library and the same
`_Reader`-polling-a-queue technique core/processes.py already uses for
`interactive_run` (pywinpty's own `read()` is blocking-only, with no timeout
parameter, so a background thread feeds a queue that this module polls
against a deadline).

Not a port of core/bash_session.py's mechanism — PowerShell's line editor
(PSReadLine) differs enough to need its own design:

1. **PSReadLine fires an ANSI cursor-position query (`\\x1b[6n`) at startup
   and periodically after, and stalls without a reply**, corrupting later
   output (phantom escape bytes read back as source, spurious ParserErrors).
   There's no zsh-`unsetopt zle`-style one-liner to suppress the query.
   Fix: `_pump_until()` answers every `\\x1b[6n` it sees with a synthetic
   `\\x1b[1;1R`, continuously, for the life of the session — the row/col
   values don't matter, only that some reply arrives.

2. **A bare `\\n` sent to an interactive PSReadLine session is silently
   dropped**, not inserted and not treated as Enter — only `\\r` submits a
   line. This rules out sending a multi-line construct as one payload the
   way bash_session does.

3. **Fix for (2) also solves scoping**: each command is staged into a temp
   `.ps1` file (ordinary file, real newlines, no pty quirks), a reset/exit-
   code trailer is appended to the same file, and exactly one line is typed
   live: `. '<path>'`. Dot-sourcing runs in the caller's own scope (a
   variable, `Set-Location`, and a function defined inside all persist
   afterward), and since the trailer is the file's last lines it always
   runs before the host calls `prompt()` again. `prompt` is PowerShell's
   analog of bash's PROMPT_COMMAND / zsh's `precmd` (invoked before every
   top-level read, even after a script redefines it) — resetting it in the
   trailer closes the same prompt-leak class the POSIX forks fixed.

Exit-code capture needs its own formula: `$LASTEXITCODE` is set only by
native executables and is sticky (a later successful cmdlet does not reset
it). Idiom used here, only consulting `$LASTEXITCODE` when `$?` says the
last thing failed:

    $rc = if (-not $?) { if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 } } else { 0 }

Ctrl-C (`\\x03`) does not interrupt anything running inside the dot-sourced
staging path — a `Start-Sleep`/busy-loop under it just runs to completion
regardless of the signal. (A bare, top-level typed command still interrupts
normally; that's just not the path any real command here takes.) So
`_handle_timeout()` skips Ctrl-C and force-kills + respawns on every
timeout; `state_reset` is therefore always `true`, unlike bash_session/
zsh_session's plain-recovery case. Whether this is specific to the
pexpect/PowerShell-Core transport used for testing or also holds on real
ConPTY (whose Ctrl+C delivery — `GenerateConsoleCtrlEvent` — is a
different mechanism from Unix's tty-byte-to-SIGINT) is unverified.

**Genuinely unverified: the pywinpty/ConPTY transport itself.** Everything
above was validated against a real `pwsh` via `pexpect` on Linux (same
interpreter cross-platform), confirming the PowerShell-language findings —
not the transport, since `pywinpty` is a Windows-only wheel. Real ConPTY
may already answer the DSR query itself; the answering logic here is kept
regardless (harmless if so, load-bearing if not).

Not a replacement for `powershell`: use that for one-off commands, this for
anything that needs state (cd, variables, functions, imported modules) to
survive across multiple calls. Also not a replacement for `interactive_run`
— a foreground command that blocks on its own stdin will still hang here for
the full per-call timeout, exactly as it would in `powershell`.
"""

import asyncio
import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from core.output import clip

TOOLS = [
    {
        "name": "powershell_session",
        "description": (
            "Run a command in a PERSISTENT PowerShell session. Unlike the "
            "`powershell` tool, which spawns a fresh process every call, this "
            "keeps the SAME pwsh process alive across calls — `cd`/"
            "Set-Location, `$env:` changes, variables, functions, and imported "
            "modules all survive from one call to the next. Use plain "
            "`powershell` for quick one-off commands; use this when you need "
            "that persistence (e.g. `cd` into a project once and run several "
            "commands relative to it, or dot-source a script and keep using "
            "what it defines). Write real PowerShell — cmdlets, the object "
            "pipeline, multi-line scripts are fine (they're staged through a "
            "temp file, not typed live, so heredoc-style here-strings and "
            "for-loops work normally). A command that blocks waiting on its "
            "own stdin (a credential prompt, `Read-Host`, an installer) will "
            "hang until `timeout` — use `interactive_run` for those instead. "
            "Pass `restart: true` (alone, as its own call) to kill and "
            "respawn the session, discarding all state, if it gets wedged or "
            "you want a clean environment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "PowerShell command or script to run. May be "
                        "multi-line (a for-loop, a here-string, several "
                        "statements) — it's written to a temp .ps1 file and "
                        "dot-sourced, not typed character-by-character, so "
                        "normal PowerShell script syntax applies."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for this command to finish (default "
                        "120). On timeout, the session is force-killed and "
                        "respawned fresh so the NEXT call isn't stuck too — "
                        "check the `recovered` field. State (cd/variables/"
                        "functions) does NOT survive a timeout, unlike a "
                        "normal completed call."
                    ),
                },
                "restart": {
                    "type": "boolean",
                    "description": (
                        "Kill and respawn the session, discarding cwd/"
                        "variables/functions/modules. Send this alone, "
                        "without `command`, in its own call."
                    ),
                },
            },
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_OUTPUT = 12000
_DEFAULT_TIMEOUT = 120
# How often the read loop re-checks its deadline while waiting on the queue —
# mirrors core/processes.py's own `_POLL_INTERVAL` (pywinpty's read() has no
# timeout parameter, so this is what turns it into a bounded-wait operation).
_POLL_INTERVAL = 0.2

# PSReadLine's cursor-position query (ANSI Device Status Report, `ESC [ 6 n`).
# Answered continuously, every time it appears — see the module docstring's
# Finding 1. The reply's actual row/col numbers do not appear to matter to
# PSReadLine, confirmed live; `1;1` is as good as any other well-formed value.
_DSR_QUERY = "\x1b[6n"
_DSR_REPLY = "\x1b[1;1R"

# Strips ANSI/OSC escapes the same way core/output.strip_ansi does, plus CRLF
# normalization — PSReadLine's syntax-highlighting redraw and the Windows
# console's own CRLF line endings both need cleaning before a caller sees
# `output`, same reasoning as core/bash_session.py's own `_clean()`.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[a-zA-Z]")

_shell = None  # winpty.PtyProcess | None
_reader = None  # _Reader | None
_sentinel: str | None = None
_buffer = ""  # unconsumed bytes carried over between calls (rare, defensive)


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "powershell_session":
        return json.dumps({"error": f"unknown powershell_session tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


class _Reader:
    """Runs PtyProcess.read() on its own thread and feeds a queue.

    Identical technique and identical reasoning to core/processes.py's own
    `_Reader` — duplicated here rather than imported, matching this
    project's convention of not sharing small pieces of tool-specific
    machinery between modules (see core/processes.py's own module
    docstring). pywinpty's `read()` is blocking-only with no timeout
    parameter, so a caller that wants a bounded wait has to poll a queue
    fed by a background thread instead of calling `read()` on the main
    thread directly.
    """

    def __init__(self, proc):
        self._proc = proc
        self._queue: queue.Queue = queue.Queue()
        self.eof = False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            while True:
                data = self._proc.read(4096)
                # See core/processes.py's own `_Reader._pump` for why an
                # empty string here is NOT eof (pywinpty substitutes a
                # literal '0011Ignore' marker for a routine "no data yet"
                # read, which PtyProcess.read turns back into '').
                if not data:
                    continue
                self._queue.put(data)
        except EOFError:
            pass
        except Exception as e:  # the pty vanished under us mid-read
            self._queue.put(f"\n[reader error: {e}]")
        self._queue.put(None)

    def get(self, timeout: float) -> str | None:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            self.eof = True
            return None
        return item


def _sentinel_pattern() -> "re.Pattern[str]":
    assert _sentinel is not None
    return re.compile(re.escape(_sentinel) + r":(-?\d+)")


def _clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


def _pump_until(deadline: float, pattern: "re.Pattern[str]") -> tuple[str, "re.Match[str] | None"]:
    """Accumulate output into the module-level `_buffer`, answering every DSR
    query as it arrives, until `pattern` matches or the deadline passes.

    Returns ("matched"|"eof"|"timeout", match). The DSR-answering has to
    happen INSIDE this loop (not as a post-hoc cleanup pass) — see the module
    docstring's Finding 1: PSReadLine will not proceed past a query it never
    got an answer to, so the reply has to go out the moment the query bytes
    are seen, not after accumulation finishes.
    """
    global _buffer
    assert _reader is not None
    assert _shell is not None

    def _answer_dsr(chunk: str) -> str:
        while _DSR_QUERY in chunk:
            try:
                _shell.write(_DSR_REPLY)
            except Exception as e:
                # Best-effort — if the pty is already gone, the caller's own
                # EOF/timeout handling downstream is what actually matters,
                # not this reply landing. Logged rather than a bare `pass`
                # for the same reason every other silent catch in this
                # project family logs (see core/bash_session.py's own
                # `_shutdown_sync`) rather than truly swallowing it.
                print(f"[powershell_session] DSR reply write failed (ignored): {e}")
            chunk = chunk.replace(_DSR_QUERY, "", 1)
        return chunk

    while True:
        m = pattern.search(_buffer)
        if m:
            # The background reader thread can deliver a fast burst of
            # chunks, the first of which already satisfies `pattern` while
            # later ones from the same burst are still queued, unread.
            # Returning immediately would leave those for the NEXT call's
            # `_pump_until` to drain instead, contaminating the start of
            # the next command's output (which always begins from
            # `_buffer = ""`). Drain anything already available now first.
            while True:
                extra = _reader.get(0)
                if not extra:
                    break
                _buffer += _answer_dsr(extra)
            # `m`'s start/end positions are still valid — the drain above
            # only ever APPENDS to `_buffer`, never rewrites the prefix `m`
            # was found in.
            return "matched", m
        if _reader.eof:
            return "eof", None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout", None
        chunk = _reader.get(min(remaining, _POLL_INTERVAL))
        if not chunk:
            continue
        _buffer += _answer_dsr(chunk)


def _write_temp_script(command: str, trailer: str) -> str:
    """Write `command` + `trailer` to a fresh temp .ps1 file, return its path.

    Staging through a real file — not typing multi-line text into the live
    session — is the design that resolves both Finding 2 (raw `\\n` bytes are
    silently dropped by PSReadLine) and the scoping question (dot-sourcing a
    FILE runs in the caller's scope; see the module docstring's Finding 3).
    A fresh filename per call avoids any risk of a slow caller's write racing
    a fast one's read on the same path.

    `$LASTEXITCODE = $null` is prepended BEFORE the user's own command, not
    just consulted after it — this is load-bearing, not defensive styling.
    `$LASTEXITCODE` is a SESSION-level variable in PowerShell, sticky across
    calls to this tool too, not just within one script (bash's `$?` has no
    equivalent cross-call leakage — it is always freshly scoped to the
    immediately preceding command). Confirmed live this is a real bug, not
    theoretical: a native failure in one call (`exit 7`) leaked its stale
    `$LASTEXITCODE` into a LATER, unrelated call's cmdlet-only failure two
    calls afterward, reporting a misleading `7` instead of the sensible `1`
    fallback the exit-code formula's `else` branch is supposed to produce
    when no native command actually ran in THIS call. Resetting it fresh at
    the start of every per-call script closes that leak.
    """
    fd, path = tempfile.mkstemp(prefix="rm_pwsh_session_", suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("$LASTEXITCODE = $null\n")
            f.write(command)
            if not command.endswith("\n"):
                f.write("\n")
            f.write(trailer)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return path


def _trailer(sentinel: str) -> str:
    """The lines appended after the user's own command, in the SAME temp
    file — see the module docstring's Finding 3 for why same-file placement
    is what gives this the same atomicity bash's brace-group trick has, and
    Finding 4 for why the exit-code line isn't simply `$LASTEXITCODE`.
    """
    return (
        f"\n$__rc_{sentinel} = if (-not $?) {{ if ($LASTEXITCODE) {{ $LASTEXITCODE }} "
        f"else {{ 1 }} }} else {{ 0 }}\n"
        f"function prompt {{ '' }}\n"
        f'Write-Output "{sentinel}:$($__rc_{sentinel})"\n'
    )


def _spawn() -> str | None:
    """(Re)start the persistent PowerShell session. Returns an error string,
    or None on success."""
    global _shell, _reader, _sentinel, _buffer

    try:
        import winpty
    except ImportError:
        return (
            "pywinpty is not installed — `pip install pywinpty` to enable "
            "the powershell_session tool"
        )

    _shutdown_sync()
    _sentinel = uuid.uuid4().hex
    _buffer = ""

    executable = _powershell_executable()
    if executable is None:
        return (
            "no PowerShell interpreter found on PATH — this means PATH is "
            "broken, since Windows PowerShell 5.1 ships with every Windows "
            "install. Install PowerShell 7+ from "
            "https://github.com/PowerShell/PowerShell#get-powershell or "
            "repair PATH."
        )

    try:
        proc = winpty.PtyProcess.spawn([executable, "-NoLogo", "-NoProfile"])
    except Exception as e:
        return f"could not spawn persistent PowerShell: {e}"

    _shell = proc
    _reader = _Reader(proc)

    # Prime the session: wait for the first prompt, answering DSR queries
    # along the way, then send our own `function prompt { '' }` reset so a
    # stray banner never leaks into command #1's output (mirrors
    # core/bash_session.py's own priming round trip).
    #
    # No `$` anchor on the prompt pattern — deliberately. PSReadLine's
    # terminal mode-set escape (`\x1b[?1h`) can arrive AFTER the visible
    # "> " prompt text rather than before it, which breaks an anchored
    # `r"> $"` (the buffer no longer truly ends with "> "). A bare `"> "`
    # search matches regardless of what harmless escape noise follows.
    deadline = time.monotonic() + _DEFAULT_TIMEOUT
    outcome, _ = _pump_until(deadline, re.compile(r"> "))
    if outcome != "matched":
        _shutdown_sync()
        return "persistent PowerShell did not reach an initial prompt in time"

    # Reset+sentinel sent as one round trip, matched against its OWN
    # one-off, throwaway sentinel — deliberately NOT `_sentinel_pattern()`
    # (the shared, session-wide sentinel every regular command also
    # searches for). Reusing the shared sentinel here would let leftover,
    # not-yet-drained priming bytes satisfy a REGULAR command's later
    # search against that same pattern. A one-off probe, never searched
    # for again, makes that collision structurally impossible.
    priming_sentinel = uuid.uuid4().hex
    try:
        _buffer = ""
        _shell.write(f"function prompt {{ '' }}; Write-Output \"{priming_sentinel}:0\"")
        _shell.write("\r")
    except Exception as e:
        _shutdown_sync()
        return f"could not prime persistent PowerShell: {e}"
    deadline = time.monotonic() + _DEFAULT_TIMEOUT
    outcome, _ = _pump_until(
        deadline, re.compile(re.escape(priming_sentinel) + r":(-?\d+)")
    )
    if outcome != "matched":
        _shutdown_sync()
        return "persistent PowerShell did not settle after priming in time"
    _buffer = ""

    return None


def _powershell_executable() -> str | None:
    """pwsh (PowerShell 7+) preferred, Windows PowerShell 5.1 as a fallback —
    same preference order and same reasoning as core/powershell.py's own
    `_powershell_executable()`, duplicated rather than imported (this
    project's own convention — see core/processes.py's module docstring).
    Not reporting `flavor` back to the caller the way the stateless tool
    does: a persistent session's `restart` response would be the only place
    to surface it, and the value is much lower here since a script that
    trips over a version difference will simply error legibly on its own.
    """
    import shutil

    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh
    legacy = shutil.which("powershell")
    if legacy:
        return legacy
    fixed = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if Path(fixed).is_file():
        return fixed
    return None


def _run(tool_input: dict) -> str:
    if tool_input.get("restart"):
        error = _spawn()
        if error:
            return json.dumps({"error": error})
        return json.dumps({"restarted": True})

    if _shell is None:
        error = _spawn()
        if error:
            return json.dumps({"error": error})

    if _shell is None:  # pragma: no cover - _spawn() above already returns
        # an error string if it fails; spelled out for mypy the same way
        # core/bash_session.py's own `_run()` does before dereferencing its
        # globals directly below.
        return json.dumps({"error": "session is not running"})

    command = tool_input.get("command", "")
    if not command.strip():
        return json.dumps({"error": "no command provided"})

    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)

    assert _sentinel is not None
    script_path = _write_temp_script(command, _trailer(_sentinel))
    try:
        global _buffer
        _buffer = ""
        line = f". '{script_path}'"
        try:
            assert _shell is not None
            _shell.write(line)
            _shell.write("\r")
        except Exception as e:
            _shutdown_sync()
            return json.dumps(
                {
                    "error": f"the persistent PowerShell session vanished "
                    f"mid-write ({e}); it will respawn fresh on the next call"
                }
            )

        deadline = time.monotonic() + timeout
        outcome, match = _pump_until(deadline, _sentinel_pattern())

        if outcome == "timeout":
            return json.dumps(_handle_timeout(script_path))
        if outcome == "eof":
            partial = _strip_echo(_clean(_buffer), line)
            _shutdown_sync()
            return json.dumps(
                {
                    "output": clip(partial, _MAX_OUTPUT),
                    "error": "the persistent PowerShell session exited "
                    "unexpectedly; it will respawn fresh on the next call",
                }
            )

        assert match is not None
        # Clean FIRST, then strip the echo — not the reverse. PowerShell's
        # per-token syntax-highlighting wraps individual characters in
        # SEPARATE colour-escape spans (confirmed live: the `.` and the
        # following space of our own `. '<path>'` invocation land in
        # different `\x1b[...m` spans), so the literal `line` string we
        # sent essentially never appears as one contiguous run in the RAW,
        # still-escaped buffer — `_strip_echo` on raw bytes reliably found
        # nothing (`rfind` returning -1) and left the whole redraw mess in
        # `output`. Once `_clean()` has stripped every escape code, the
        # plain characters ARE contiguous, and `_strip_echo`'s `rfind`
        # correctly lands on the one clean, complete final redraw.
        output = _strip_echo(_clean(_buffer[: match.start()]), line)
        return_code = int(match.group(1))
        return json.dumps(
            {
                "output": clip(output, _MAX_OUTPUT) or "(no output)",
                "return_code": return_code,
            }
        )
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


def _strip_echo(text: str, sent_line: str) -> str:
    """Remove PSReadLine's own echoed/redrawn rendering of the line we typed
    (the `. '<path>'` dot-source invocation) from the front of the captured
    output, leaving just the real command output. PSReadLine echoes input
    back, same class of behavior core/bash_session.py's zsh support already
    documents for zsh's non-ZLE reader.
    """
    # `rfind`, not `find`: PSReadLine's syntax-highlighting redraw re-emits
    # the buffer on every character processed, so the raw stream contains
    # many overlapping, progressively-longer prefixes of `sent_line`, not
    # one clean copy. The LAST occurrence is the final, complete redraw
    # right before submission — everything genuinely new only appears
    # after it.
    idx = text.rfind(sent_line)
    if idx == -1:
        return text
    return text[idx + len(sent_line) :]


def _handle_timeout(script_path: str) -> dict:
    """A command blew past its timeout. Force-kill the session and respawn
    fresh — ALWAYS, unconditionally. No Ctrl-C attempt first.

    core/bash_session.py/core/zsh_session.py try a plain Ctrl-C first,
    since on a real pty a blocking command with no handler of its own dies
    to it cleanly, and only a raw-mode program needs the harder escalation
    path. That premise doesn't hold here: Ctrl-C (`\\x03`) does not
    propagate into anything executing inside the dot-sourced staging path
    (a `Start-Sleep`/busy-loop under it just runs to completion regardless).
    A bare, top-level typed command still interrupts normally — that's just
    not the path any real command here takes, since every command is
    dot-sourced (see the module docstring).

    Attempting Ctrl-C first would therefore either silently wait out the
    stuck command (defeating the point of a responsive timeout, while
    misreporting `recovered: true, not force_killed`) or, if time-boxed
    short, escalate anyway with the attempt contributing nothing. So this
    skips straight to force-kill + respawn. `state_reset: true` always,
    honestly — there is no "plain recovery keeps state" case here, unlike
    the POSIX forks. Whether the Ctrl-C non-propagation is specific to the
    pexpect/PowerShell-Core transport used for testing or also holds on
    real ConPTY (a different signal-delivery mechanism —
    `GenerateConsoleCtrlEvent` vs. Unix's tty-byte-to-SIGINT) is
    unverified.
    """
    assert _shell is not None
    partial = _clean(_buffer)

    force_killed = False
    try:
        _shell.terminate(force=True)
        force_killed = True
    except Exception as e:
        print(f"[powershell_session] force-kill on timeout failed (ignored): {e}")

    error = _spawn()
    recovered = error is None

    try:
        os.remove(script_path)
    except OSError:
        pass

    return {
        "output": clip(partial, _MAX_OUTPUT),
        "timed_out": True,
        "recovered": recovered,
        "force_killed": force_killed,
        "state_reset": True,
        "return_code": None,
    }


def _shutdown_sync():
    global _shell, _reader, _sentinel, _buffer
    if _shell is not None:
        for label, cleanup in (
            ("terminate", _shell.terminate),
            ("close", _shell.close),
        ):
            try:
                cleanup(force=True)
            except Exception as e:
                print(
                    f"[powershell_session] {label} on shutdown failed "
                    f"(ignored): {e}"
                )
    _shell = None
    _reader = None
    _sentinel = None
    _buffer = ""


async def shutdown():
    """Kill the persistent session. Safe to call even if never started."""
    if _shell is None:
        return
    await asyncio.to_thread(_shutdown_sync)
