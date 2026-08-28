"""Interactive processes — pywinpty/ConPTY.

The powershell tool pipes stdin from nowhere, so anything that asks a question
mid-run (an ssh host-key confirmation, a credential prompt, an installer, a
REPL) blocks until the timeout kills it. This spawns the program on a real
ConPTY pseudo-console and answers its prompts from a script the model supplies
up front. ConPTY is confirmed present and working on Windows 10
(KernelBase.dll exports CreatePseudoConsole/ClosePseudoConsole/
ResizePseudoConsole/CreatePseudoConsoleAsUser) and exercised end to end through
`pywinpty` (a clean prebuilt wheel, no compiler needed): spawn, read, write and
exit-code capture all confirmed, including a full expect-then-send round trip
against a real prompting child process.

Three properties of ConPTY that shape the code, none of them oversights:
  * `pywinpty`'s `read()` is blocking-only — confirmed live, an attempt at a
    non-blocking read raised TypeError immediately — and takes no timeout. So
    the read runs on its own thread feeding a queue, and this module's loop
    polls that queue against a deadline to get timeout-per-step behaviour.
  * Windows has no signal concept for a child like this, so `signal_status` in
    the response is always None — `exit_status` alone is the whole story.
  * **`secret: true` protects the transcript, not the console.** ConPTY is
    deliberately built to behave exactly like a real console from the child's
    point of view, which means the *child* owns its echo setting
    (`SetConsoleMode`/`ENABLE_ECHO_INPUT`) and the host side cannot turn it off
    out from under it. In practice this rarely matters, because real secret
    prompts — an `ssh` password prompt, `Read-Host -AsSecureString` — suppress
    their own echo before reading. It was confirmed to matter with a child that
    deliberately does not (a plain `input()`): the sent secret genuinely
    appeared in the raw ConPTY output. Mitigated as defence in depth rather
    than guaranteed: every literal `send` value from a `secret: true` step is
    scrubbed out of incoming buffer content the moment it arrives, before it
    can reach either pattern-matching or the transcript.

Commands are wrapped as `["cmd.exe", "/c", command]` — deliberately not
`pwsh`/`powershell.exe`, to avoid re-introducing the `-Command` quoting
complexity already dealt with in core/powershell.py. Ask for PowerShell as the
command itself if a given task needs it; cmd.exe is only the thin wrapper.

No ConPTY resize handling: this is a scripted expect/send tool, not a live
human-facing terminal, so there is no window for the caller to resize.

**Know what the transport is, because this tool handles passwords.** pywinpty
does not hand pty output back in-process: `PtyProcess.__init__` opens a
listening TCP socket on an ephemeral 127.0.0.1 port, starts a reader thread
that connects back to it, and `accept()`s the first connection (read its
ptyprocess.py — this is not an implementation detail that can be configured
away). Everything the child prints therefore crosses a loopback socket with no
authentication on it, and `accept()` takes whoever connects first, so another
local process could in principle win that race and receive the transcript
instead. Anything typed at a prompt that the program echoes is in that stream.

This is the same class of exposure core/kernel.py goes to the trouble of
CurveZMQ for, and it cannot be fixed from here — it is pywinpty's design, and
the alternative implementations are worse. Treat `secret: true` as protecting
the *returned transcript*, which is its documented job, and not the wire.

Requires:  pip install pywinpty  (API verified against 3.0.5)
"""

import asyncio
import json
import queue
import re
import threading
import time

from core.output import clip, strip_ansi

TOOLS = [
    {
        "name": "interactive_run",
        "description": (
            "Run a command that PROMPTS for input, answering its prompts from a "
            "script. Use this instead of powershell whenever a program asks a "
            "question mid-run — password challenges, ssh host-key confirmation, "
            "'Are you sure? [y/N]', winget/installer prompts, or a REPL — since "
            "the powershell tool has no stdin and will simply hang. Each step "
            "waits for a regex to appear, then sends a line. Returns JSON with the "
            "terminal transcript and the exit status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to spawn on a pty, e.g. "
                        "'ssh user@host uptime'."
                    ),
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "Prompt/response pairs, applied in order. Omit for a "
                        "command that only needs a pty and no answers."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "expect": {
                                "type": "string",
                                "description": (
                                    "Regex to wait for, e.g. 'password:' or "
                                    "'\\[y/N\\]'."
                                ),
                            },
                            "send": {
                                "type": "string",
                                "description": "Line to send once it matches.",
                            },
                            "secret": {
                                "type": "boolean",
                                "description": (
                                    "Redact this response from the returned "
                                    "transcript. Use for passwords."
                                ),
                            },
                        },
                        "required": ["expect", "send"],
                    },
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for each prompt and for the program to "
                        "finish (default 30)."
                    ),
                },
            },
            "required": ["command"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_TRANSCRIPT = 12000
_DEFAULT_TIMEOUT = 30
# How often the pump loop re-checks its deadline while waiting on the queue —
# short enough that a timeout is never overshot by more than this.
_POLL_INTERVAL = 0.2
# How long to wait for the child to actually be gone after terminate/close,
# before reporting exit_status as-is rather than blocking any longer.
_EXIT_GRACE = 5.0


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "interactive_run":
        return json.dumps({"error": f"unknown process tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


class _Reader:
    """Runs PtyProcess.read() on its own thread and feeds a queue.

    pywinpty's read() blocks until there is data or the pty closes — there is
    no timeout parameter to give it, so a caller that wants to honour a
    per-step deadline has to poll a queue instead of calling read() directly
    on the main thread. `None` on the queue is the EOF sentinel.
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
                # `continue`, NOT `break`: an empty string here does not mean
                # EOF. pywinpty's reader thread substitutes the literal marker
                # b'0011Ignore' whenever the underlying pty read came back with
                # nothing (ptyprocess.py: `pty.read(...) or '0011Ignore'`), and
                # PtyProcess.read turns that marker back into ''. It is a
                # routine "no data this time", not a closed terminal.
                #
                # Real EOF is unambiguous and arrives as an exception: the
                # reader thread sends a zero-length frame, recv returns b'',
                # and read() raises EOFError, which the handler below catches.
                # Treating '' as EOF made this loop stop at the first idle
                # moment — before the prompt being waited for had appeared —
                # so a step would report steps_matched: 0 with a truncated
                # transcript and no error at all. That fails intermittently,
                # depending on timing, which is why a single quick round trip
                # can pass and a real session cannot.
                if not data:
                    continue
                self._queue.put(data)
        except EOFError:
            pass
        except Exception as e:  # the pty vanished under us mid-read
            self._queue.put(f"\n[reader error: {e}]")
        self._queue.put(None)

    def get(self, timeout: float) -> str | None:
        """One chunk, or None if the queue was empty for the whole timeout.
        The EOF sentinel also surfaces as None — callers distinguish the two
        with `.eof`, set the instant the sentinel is actually seen."""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            self.eof = True
            return None
        return item


def _run(tool_input: dict) -> str:
    try:
        import winpty
    except ImportError:
        return json.dumps(
            {
                "error": "pywinpty is not installed — `pip install pywinpty` "
                "to enable interactive_run on Windows"
            }
        )

    command = tool_input.get("command", "")
    if not command.strip():
        return json.dumps({"error": "no command provided"})

    steps = tool_input.get("steps") or []
    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)

    try:
        # cmd.exe /c is a thin wrapper so `command` can use shell syntax; it
        # is not a requirement to use cmd.exe-specific features.
        proc = winpty.PtyProcess.spawn(["cmd.exe", "/c", command])
    except Exception as e:
        return json.dumps({"error": f"could not spawn command: {e}"})

    reader = _Reader(proc)
    buffer = ""
    transcript: list[str] = []
    matched = 0
    timed_out_at = None
    # Best-effort defense-in-depth for `secret: true` steps — see the module
    # docstring for why this is a mitigation, not a guarantee, on Windows.
    secrets_to_scrub: list[str] = []

    def _scrub(text: str) -> str:
        for secret in secrets_to_scrub:
            if secret:
                text = text.replace(secret, "***")
        return text

    def pump_until(
        pattern: "re.Pattern[str]", deadline: float
    ) -> tuple[str, "re.Match[str] | None"]:
        """Waits for `pattern` in the accumulating buffer, or the deadline.

        Returns ("matched"|"eof"|"timeout", match) — the three outcomes the
        step loop below needs to tell apart; there is no library doing it for
        us, so all three are spelled out. The match object is handed back rather than the caller
        re-running `search`: it is the same result by construction, and
        returning it means there is no second call whose None case has to be
        reasoned about.
        """
        nonlocal buffer
        while True:
            found = pattern.search(buffer)
            if found:
                return "matched", found
            if reader.eof:
                return "eof", None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            chunk = reader.get(min(remaining, _POLL_INTERVAL))
            if chunk:
                buffer += _scrub(chunk)

    try:
        for step in steps:
            pattern = re.compile(str(step.get("expect", "")))
            deadline = time.monotonic() + timeout
            outcome, match = pump_until(pattern, deadline)

            if outcome == "timeout":
                timed_out_at = str(step.get("expect", ""))
                transcript.append(buffer)
                buffer = ""
                break
            if outcome == "eof" or match is None:
                # The program finished before this prompt ever appeared —
                # not a timeout, just an early exit. Same distinction
                # an early exit does.
                timed_out_at = None
                transcript.append(buffer)
                buffer = ""
                break

            transcript.append(buffer[: match.end()])
            buffer = buffer[match.end() :]
            reply = str(step.get("send", ""))
            try:
                # "\r" alone, not "\r\n". Writing to a ConPTY is synthesised
                # typing, and the Enter key is a carriage return — the console
                # is what turns that into a line terminator for the program
                # reading it. Sending "\r\n" types Enter *and then* a Ctrl-J,
                # leaving a spare newline in the input buffer that the next
                # read consumes as an empty line. A single-step script cannot
                # show this (nothing reads again), which is exactly why it
                # survives a quick round-trip test; a two-step script answers
                # its second prompt with "" before the model's real answer
                # arrives.
                proc.write(reply + "\r")
            except EOFError:
                # The pty closed between matching the prompt and answering it
                # — seen live against cmd.exe's own `set /p` under emulation.
                # Not a timeout (we did match); just nothing left to send to.
                timed_out_at = None
                break
            if step.get("secret"):
                transcript.append("***\n")
                secrets_to_scrub.append(reply)  # scrub it out if echoed back
            else:
                transcript.append(reply + "\n")
            matched += 1

        # Drain whatever the program prints after the last answer, mirroring
        # the drain that follows the last answered prompt.
        drain_deadline = time.monotonic() + timeout
        while not reader.eof and time.monotonic() < drain_deadline:
            chunk = reader.get(min(drain_deadline - time.monotonic(), _POLL_INTERVAL))
            if chunk:
                buffer += _scrub(chunk)
        transcript.append(buffer)
    finally:
        # Two calls, and both are needed. `terminate` stops the child (it goes
        # straight to TerminateProcess here — pywinpty's `kill` is os.kill,
        # and on Windows os.kill is TerminateProcess for anything that is not
        # CTRL_C_EVENT/CTRL_BREAK_EVENT, so the SIGINT it names is not a
        # polite interrupt). `close` releases what PtyProcess allocated per
        # call: two sockets and a reader thread. Without it those are only
        # reclaimed when __del__ happens to run, and the reader thread holds a
        # reference to the process object, so a wedged read pins the lot for
        # the lifetime of this long-running app.
        for label, cleanup in (("terminate", proc.terminate), ("close", proc.close)):
            try:
                cleanup(force=True)
            except Exception as e:
                print(f"[processes] proc.{label} failed (ignored): {e}")

    # Bounded, because pywinpty's own wait() is `while isalive(): sleep(0.1)`
    # with no timeout — if the child somehow survived TerminateProcess above,
    # an unguarded wait() would hang this tool call forever and quietly break
    # the `timeout` this tool advertises. exitstatus is None while alive, which
    # is a truthful answer for a process that would not die.
    exit_deadline = time.monotonic() + _EXIT_GRACE
    while proc.isalive() and time.monotonic() < exit_deadline:
        time.sleep(0.05)

    return json.dumps(
        {
            "transcript": clip(strip_ansi("".join(transcript)), _MAX_TRANSCRIPT),
            "steps_matched": matched,
            "steps_total": len(steps),
            "exit_status": proc.exitstatus,
            "signal_status": None,  # Windows has no signal status for a child
            "timed_out_waiting_for": timed_out_at,
        }
    )
