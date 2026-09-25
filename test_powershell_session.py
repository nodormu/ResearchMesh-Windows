"""Behavioural regression tests for core/powershell_session.py.

    python test_powershell_session.py

`core/powershell_session.py` imports `winpty` (pywinpty), a Windows-only
wheel that cannot be installed or exercised on this dev sandbox (Linux) at
all — confirmed: `pip show pywinpty` finds nothing here, `import winpty`
raises `ModuleNotFoundError`. So this suite does NOT test the real
pywinpty/ConPTY transport — that genuinely needs a real Windows run, and is
explicitly flagged as unverified in the module's own docstring.

What this DOES test, with real confidence: every line of ACTUAL module logic
in core/powershell_session.py other than the literal `import winpty`
statement — the DSR-answering loop, the temp-file staging + dot-source
protocol, the `$?`/`$LASTEXITCODE` exit-code formula, the `prompt`-reset
trailer, timeout->force-kill+respawn recovery, and restart. This works
because pywinpty's
`PtyProcess` API surface (`spawn(argv)`, `.read(size)` blocking, `.write(s)`,
`.terminate(force=True)`, `.close(force=True)`, `.isalive()` — the exact
calls core/processes.py's own pywinpty usage and this module both make) is
close enough to `pexpect.spawn`'s own API that a thin shim can wrap a REAL
`pexpect`-spawned `pwsh` process (PowerShell 7 is the same interpreter on
Linux and Windows) and stand in for `winpty` via `sys.modules` injection,
BEFORE `core.powershell_session` is ever imported. Every behavioural finding
this module's design rests on (the DSR query, the dropped-`\\n` multi-line
limitation, dot-sourcing's scope-preserving semantics, the exit-code
stickiness trap, `prompt`-function-as-precmd-analog) was originally
discovered THIS way — this suite formalizes that same live-verification
technique into a repeatable regression battery, rather than testing a
re-implementation of the module's logic.

Spawns a real `pwsh` (this box's `~/.local/bin/pwsh`) per test scenario via
the shim, and calls `powershell_session.shutdown()` at the end. Confirmed
present at suite start; skips with a clear message if it is not.
"""

import asyncio
import json
import shutil
import sys
import types

ROOT_CHECKS_PASSED = 0
ROOT_CHECKS_FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global ROOT_CHECKS_PASSED, ROOT_CHECKS_FAILED
    if ok:
        ROOT_CHECKS_PASSED += 1
        print(f"  ok    {label}")
    else:
        ROOT_CHECKS_FAILED += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------
# The winpty shim: a real pexpect-spawned pwsh, wrapped to expose exactly the
# PtyProcess surface core/powershell_session.py calls.
# --------------------------------------------------------------------------

def _install_fake_winpty():
    import pexpect

    class _FakePtyProcess:
        def __init__(self, child):
            self._child = child

        @classmethod
        def spawn(cls, argv):
            child = pexpect.spawn(
                argv[0], argv[1:], encoding="utf-8", codec_errors="replace",
                echo=False, timeout=None,
            )
            return cls(child)

        def read(self, size):
            # winpty's read() is blocking-only with no timeout parameter and
            # raises EOFError at genuine end of stream (see
            # core/processes.py's own _Reader._pump for the documented
            # contract this module is written against). pexpect's own
            # `.read(size)` can raise its OWN pexpect.TIMEOUT (if a
            # per-child .timeout is set) or pexpect.EOF — translated here so
            # the real module, which only knows the winpty contract, sees
            # exactly what it expects.
            while True:
                try:
                    data = self._child.read_nonblocking(size=size, timeout=0.2)
                    if data:
                        return data
                    continue
                except pexpect.exceptions.TIMEOUT:
                    continue
                except pexpect.exceptions.EOF as e:
                    raise EOFError(str(e)) from e

        def write(self, s):
            self._child.send(s)

        def terminate(self, force=True):
            try:
                self._child.terminate(force=force)
            except Exception as e:
                print(f"[test shim] terminate failed (ignored): {e}")

        def close(self, force=True):
            try:
                self._child.close(force=force)
            except Exception as e:
                print(f"[test shim] close failed (ignored): {e}")

        def isalive(self):
            return self._child.isalive()

    fake_module = types.ModuleType("winpty")
    fake_module.PtyProcess = _FakePtyProcess
    sys.modules["winpty"] = fake_module


def _load_module():
    """Import core.powershell_session fresh, with the fake winpty already
    installed so its own `import winpty` (inside `_spawn()`) resolves to the
    shim rather than failing."""
    _install_fake_winpty()
    import importlib

    import core.powershell_session as mod
    importlib.reload(mod)  # in case an earlier test run left globals set
    return mod


def _call(mod, tool_input: dict) -> dict:
    result = asyncio.run(mod.execute("powershell_session", tool_input))
    return json.loads(result)


def main() -> int:
    if not shutil.which("pwsh"):
        print("pwsh not found on PATH — skipping (see module docstring: "
              "this suite needs a real pwsh, PowerShell 7 being the same "
              "interpreter cross-platform, to validate against)")
        return 0

    mod = _load_module()

    print("basic command")
    r = _call(mod, {"command": 'Write-Output "hello from powershell_session"'})
    check("no error", "error" not in r, str(r))
    check("output contains real text", "hello from powershell_session" in r.get("output", ""))
    check("return_code is 0", r.get("return_code") == 0, str(r))

    print("\necho-strip: our own typed dot-source line and PSReadLine's redraw noise are removed")
    r = _call(mod, {"command": 'Write-Output "echo-strip-marker"'})
    out = r.get("output", "")
    check("real output present", "echo-strip-marker" in out)
    check("no leaked dot-source invocation", ". '" not in out, out)
    check("no leaked ANSI escape bytes", "\x1b" not in out, repr(out))
    check("no leaked sentinel plumbing", "__rc_" not in out, out)

    print("\nexit code fidelity (native success/failure, cmdlet failure, sticky-LASTEXITCODE trap)")
    r = _call(mod, {"command": "pwsh -c 'exit 0'"})
    check("native exit 0 -> return_code 0", r.get("return_code") == 0, str(r))

    r = _call(mod, {"command": "pwsh -c 'exit 7'"})
    check("native exit 7 -> return_code 7", r.get("return_code") == 7, str(r))

    r = _call(mod, {"command": "Get-Item /no/such/path -ErrorAction SilentlyContinue"})
    check("cmdlet failure (suppressed error) -> return_code 1", r.get("return_code") == 1, str(r))

    # The sticky-LASTEXITCODE trap: a successful cmdlet run right after a
    # native failure must NOT inherit the stale nonzero LASTEXITCODE.
    _call(mod, {"command": "pwsh -c 'exit 9'"})
    r = _call(mod, {"command": 'Write-Output "should be zero"'})
    check("successful cmdlet after a native failure -> return_code 0, not sticky", r.get("return_code") == 0, str(r))

    print("\ncd/variable/function persistence across separate calls")
    r1 = _call(mod, {"command": "Set-Location /tmp\n$sessionVar = 'persisted-value'\nfunction SessionFunc { 'called' }"})
    check("setup call ok", "error" not in r1, str(r1))
    r2 = _call(mod, {"command": 'Write-Output "CWD=$($PWD.Path) VAR=$sessionVar FUNC=$(SessionFunc)"'})
    out2 = r2.get("output", "")
    check("cwd persisted", "CWD=/tmp" in out2, out2)
    check("variable persisted", "VAR=persisted-value" in out2, out2)
    check("function persisted", "FUNC=called" in out2, out2)

    print("\nmulti-line construct (for-loop, no live pty-typing involved — staged through a file)")
    multiline = (
        "$total = 0\n"
        "foreach ($i in 1..5) {\n"
        "    $total += $i\n"
        "}\n"
        'Write-Output "TOTAL=$total"'
    )
    r = _call(mod, {"command": multiline})
    check("for-loop output correct", "TOTAL=15" in r.get("output", ""), str(r))
    check("for-loop return_code 0", r.get("return_code") == 0, str(r))

    print("\nprompt-stomp resistance: a command that redefines `prompt` itself")
    r1 = _call(mod, {"command": "function prompt { 'STOMPED>' }\n\"activation-like output\""})
    check("stomp call itself has no leaked prompt text", "STOMPED" not in r1.get("output", ""), str(r1))
    r2 = _call(mod, {"command": 'Write-Output "after-stomp"'})
    check("next call has no leaked prompt text either", "STOMPED" not in r2.get("output", ""), str(r2))
    check("next call's real output intact", "after-stomp" in r2.get("output", ""), str(r2))

    print("\ntimeout -> always force-kill + full respawn (Ctrl-C doesn't "
          "reach a dot-sourced script; see _handle_timeout()'s docstring)")
    _call(mod, {"command": "$timeoutProbe = 'should-not-survive'"})
    r = _call(mod, {"command": "Start-Sleep -Seconds 30", "timeout": 3})
    check("reports timed_out", r.get("timed_out") is True, str(r))
    check("reports recovered", r.get("recovered") is True, str(r))
    check("reports force_killed", r.get("force_killed") is True, str(r))
    check("reports state_reset", r.get("state_reset") is True, str(r))
    r2 = _call(mod, {"command": 'Write-Output "VAR=[$timeoutProbe]"'})
    check("variable did NOT survive the timeout/respawn", "VAR=[]" in r2.get("output", ""), str(r2))
    check("shell genuinely responsive after respawn", r2.get("return_code") == 0, str(r2))

    print("\nrestart wipes state, self-heals on next use")
    _call(mod, {"command": "$restartProbe = 'before-restart'"})
    r = _call(mod, {"restart": True})
    check("restart reports restarted:true", r.get("restarted") is True, str(r))
    r2 = _call(mod, {"command": 'Write-Output "VAR=[$restartProbe]"'})
    check("variable did NOT survive restart", "VAR=[]" in r2.get("output", ""), str(r2))

    asyncio.run(mod.shutdown())

    print(f"\n{ROOT_CHECKS_PASSED} passed, {ROOT_CHECKS_FAILED} failed")
    return 1 if ROOT_CHECKS_FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
