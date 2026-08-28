"""PowerShell — the shell tool for this client.

This is a custom schema rather than one of Anthropic's built-in ones, and the
description below carries more of the contract than a built-in tool's would
need to: it is what teaches the model to write cmdlets and the object pipeline
instead of whatever it would otherwise reach for.

Interpreter resolution, newest first:
  1. `pwsh` on PATH — PowerShell 7+.
  2. Windows PowerShell 5.1 (`powershell.exe`), first via PATH, then its fixed
     System32 location. Every Windows box ships this even when 7 was never
     installed, so it is a reasonable degrade rather than an error — the two
     are close enough that most scripts run unmodified, and the response
     reports which one actually ran (`flavor`) so a script relying on a
     7-only cmdlet fails legibly instead of silently.
  Neither existing means PATH is broken rather than that PowerShell is
  missing, and the error says so.

Process invocation uses an explicit argv list
(`[executable, "-NoProfile", "-NonInteractive", "-Command", command]`), never
`shell=True`. This matters more than it looks: `subprocess` with `shell=True`
hard-codes `<executable> /c "<args>"` — cmd.exe's flag — onto whatever
`executable=` is set to, so it would hand PowerShell a `/c` it does not
understand instead of the `-Command` it wants. Passing the argv list ourselves
sidesteps that entirely.

`-NoProfile` skips the user's profile script (faster, and deterministic
regardless of whatever a profile happens to customize). `-NonInteractive`
makes PowerShell error out instead of blocking if a command somehow prompts —
this tool has no stdin to answer with; use `interactive_run` for anything that
asks a question mid-run.

Each call is a fresh process: no variables, cwd, or session state carries over
between calls. Chain within one call with `;` (or build a real pipeline with
`|`) rather than relying on state surviving to the next call. `python` is the
tool to reach for when state genuinely has to persist.

Why `flavor` is reported rather than being an implementation detail: the two
interpreters ship different cmdlet sets, so a script using a 7-only cmdlet
should fail with something that names the interpreter it ran under. They also
used to disagree about `curl`/`wget` — aliases for `Invoke-WebRequest` on 5.1,
absent on 7 — which `_ALIAS_PRELUDE` now settles by removing them on both, so
each resolves to the real `curl.exe` that ships with Windows 10+.

Behaviour confirmed by running the tool against a real pwsh rather than assumed:
a cmdlet error and a failing native program both give exit code 1 (a non-zero
exit is not swallowed by the host), `-NonInteractive` makes `Read-Host` fail
rather than block, and an unrecognised command produces an explicit "The term
'x' is not recognized as a name of a cmdlet..." — which is what makes a wrong
reach self-correcting rather than silently wrong.

Requires:  nothing to install — Windows PowerShell 5.1 ships with the OS.
           pwsh (PowerShell 7+) is preferred if present:
           https://github.com/PowerShell/PowerShell#get-powershell
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from core.output import clip, strip_ansi

TOOLS = [
    {
        "name": "powershell",
        "description": (
            "Run a command in PowerShell. This is the shell on this machine, so "
            "write real PowerShell cmdlets and use the object pipeline. "
            "Get-ChildItem, Get-Content, Select-String, Test-Path, Remove-Item, "
            "Copy-Item, Get-Process, Select-Object, Where-Object, ForEach-Object, "
            "Measure-Object. The compatibility aliases PowerShell ships with "
            "(ls, cat, rm, cp, mv, ps, kill, diff, tee, pwd, curl, wget, man, "
            "sleep, clear, history) are REMOVED before your command runs, so "
            "they raise 'not recognized as a name of a cmdlet' rather than "
            "half-working — write the cmdlet. There is no grep, sed, awk, "
            "which, touch, or `$(...)` substitution; the equivalents are "
            "Select-String, -replace, ForEach-Object, Get-Command, New-Item, "
            "and $(...) is a subexpression rather than command substitution. "
            "Paths are native Windows paths (C:\\Users\\...), which is what "
            "every other tool here returns and accepts. Prefers PowerShell 7+ "
            "(pwsh) and falls back to the Windows PowerShell 5.1 that ships "
            "with Windows; the response says which one ran, so a script using a "
            "7-only cmdlet fails legibly. Returns JSON with the interpreter, "
            "exit code, and stdout/stderr. Each call is a fresh process: "
            "nothing persists between calls (no variables, no cwd) — chain with "
            "';' or a pipeline within one call, and use the python tool when "
            "state must survive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "PowerShell command or script to run, e.g. "
                        "'Get-Process | Select-Object -First 5 Name,Id'."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait before killing the process (default "
                        "120)."
                    ),
                },
            },
            "required": ["command"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_OUTPUT = 12000
_DEFAULT_TIMEOUT = 120

# Aliases PowerShell carries for compatibility with other shells, removed at the
# start of every command so that reaching for one fails loudly rather than
# half-working.
#
# The point is not tidiness. `ls` and `cat` are aliases, so `ls -la` and
# `cat -n f` reach a real cmdlet and fail on the *parameter* — an error about
# `-la`, which says nothing about the actual problem: that the command should
# have been `Get-ChildItem -Force` in the first place. With the alias gone the
# error is "The term 'ls' is not recognized as a name of a cmdlet", which names
# the real problem and is self-correcting on the next turn. It also removes the 5.1-vs-7 divergence for `curl`/`wget`:
# those are Invoke-WebRequest aliases on 5.1 and absent on 7, so dropping them
# makes both resolve to the real curl.exe that ships with Windows 10+.
#
# DOS-lineage aliases are deliberately kept — `dir`, `type`, `copy`, `move`,
# `del`, `cls`, `md`, `rd`, `echo`, `cd`, `pushd`/`popd` are Windows-standard,
# not imports from somewhere else.
#
# `sort` is the one such alias deliberately left in place, and the
# reason is worth keeping: Windows ships a real `sort.exe`. Removing the alias
# would not produce an error, it would silently resolve `... | sort` to a
# line-based text sorter operating on formatted output instead of Sort-Object
# operating on objects — a wrong answer rather than a loud failure, which is
# the trade this project refuses everywhere else. `diff`, `tee`, `ps`, `kill`
# and the rest have no such Windows binary, so removing them yields the clean
# CommandNotFound.
_REMOVED_ALIASES = (
    "ls", "cat", "rm", "cp", "mv", "ps", "kill", "man", "mount", "lp",
    "diff", "tee", "pwd", "curl", "wget", "sleep", "clear", "history",
)

# `-Force` because the built-ins are ReadOnly; `-ErrorAction SilentlyContinue`
# because which of these exists varies by PowerShell version and platform, and
# removing one that was never there must not be an error.
_ALIAS_PRELUDE = (
    "Remove-Item -Force -ErrorAction SilentlyContinue "
    + ",".join(f"Alias:{name}" for name in _REMOVED_ALIASES)
)

# Fixed fallback locations, checked only when PATH lookup fails.
_WIN_LEGACY_PATH = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "powershell":
        return json.dumps({"error": f"unknown powershell tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _powershell_executable() -> tuple[str, str] | None:
    """(path, flavor) for a real PowerShell, or None if none is installed.

    flavor is "pwsh" (PowerShell 7+, Core) or "windows_powershell" (legacy
    5.1) — callers report this back so a script that leans on a PS7-only
    feature fails with a legible reason rather than a mystery error.

    The 5.1 fallback is checked at its fixed System32 location as well as on
    PATH, because a PATH that has been trimmed or replaced is exactly the kind
    of box where this tool still needs to work: 5.1 is a Windows component and
    is present whether or not anything points at it.
    """
    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh, "pwsh"
    legacy = shutil.which("powershell")
    if legacy:
        return legacy, "windows_powershell"
    if Path(_WIN_LEGACY_PATH).is_file():
        return _WIN_LEGACY_PATH, "windows_powershell"
    return None


def _run(tool_input: dict) -> str:
    command = tool_input.get("command", "")
    if not command.strip():
        return json.dumps({"error": "no command provided"})

    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)

    found = _powershell_executable()
    if found is None:
        return json.dumps(
            {
                "error": (
                    "no PowerShell interpreter found, which on Windows means "
                    "PATH is broken rather than that PowerShell is missing: "
                    "5.1 ships with the OS and lives at "
                    f"{_WIN_LEGACY_PATH}, which was also checked directly. "
                    "Verify that file exists; otherwise install PowerShell 7+ "
                    "from https://github.com/PowerShell/PowerShell#get-powershell"
                )
            }
        )
    executable, flavor = found

    # PowerShell 7 colourises its error output with ANSI escapes *even when
    # stdout and stderr are pipes rather than a terminal* — confirmed by running
    # it. A one-line "command not found" arrives as several hundred bytes of
    # `\x1b[31;1m`/`\x1b[36;1m` wrapped around the words, and a multi-line
    # parser error is mostly escape codes by volume. All of it is context spent
    # to say nothing, on the tool the model reaches for most.
    #
    # NO_COLOR suppresses it at source, which beats stripping it afterwards, and
    # it is a broadly honoured convention so most native programs invoked inside
    # the command respect it too. Windows PowerShell 5.1 predates both the
    # convention and this style of colouring, and simply ignores the variable.
    # `strip_ansi` on the way out is the backstop for whatever colours itself
    # anyway.
    child_env = dict(os.environ, NO_COLOR="1")

    started = time.monotonic()
    try:
        # Explicit argv list, not shell=True — see the module docstring for
        # why: shell=True on Windows would hand this "/c" instead of the
        # "-Command" PowerShell actually wants.
        result = subprocess.run(
            [
                executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                # Newline rather than "; " to join them: the command may open
                # with a comment or a line of its own that a leading statement
                # separator would swallow.
                "-Command", f"{_ALIAS_PRELUDE}\n{command}",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "error": f"command timed out after {timeout}s",
                "interpreter": executable,
                "flavor": flavor,
            }
        )
    except OSError as e:
        return json.dumps(
            {
                "error": f"could not run {executable}: {e}",
                "interpreter": executable,
                "flavor": flavor,
            }
        )

    return json.dumps(
        {
            "interpreter": executable,
            "flavor": flavor,
            "exit_code": result.returncode,
            "stdout": clip(strip_ansi(result.stdout or ""), _MAX_OUTPUT),
            "stderr": clip(strip_ansi(result.stderr or ""), _MAX_OUTPUT),
            "seconds": round(time.monotonic() - started, 2),
        }
    )
