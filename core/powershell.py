"""PowerShell — the shell tool for this client.

This replaces the `bash` tool the POSIX build declared (Anthropic's learned
`bash_20250124` schema), rather than sitting alongside it. Claude is trained to
emit POSIX shell for that tool name, so there is no honest way to keep the name
and run a different interpreter underneath — `ls | grep foo` would simply
arrive at something with neither. Keeping real bash was the alternative, and
would have meant depending on Git for Windows and living with its MinGW
filesystem view (`/c/Users/...`) disagreeing with the native paths every other
tool here emits and accepts. See core/claude_learned_schemas.py, where the
reasoning is recorded next to the gap bash left.

The cost of the swap is that this is a custom schema: Claude learns it from
the description below rather than from training, which is why that description
is more explicit about the contract than a learned tool's would need to be.

Interpreter resolution, cheapest-and-most-portable first:
  1. `pwsh` on PATH — PowerShell 7+ (Core). Genuinely cross-platform: this is
     the same binary whether it's Linux, macOS, or Windows, so this is checked
     everywhere, not just under a win32 guard.
  2. Windows only: legacy Windows PowerShell 5.1 (`powershell.exe`), first via
     PATH, then its fixed install location. Every real Windows box ships this
     even when PS7 was never installed separately, so it is a reasonable
     degrade rather than an error — the two dialects are close enough that
     most scripts run unmodified, and the response reports which one actually
     ran (`flavor`) so a script relying on a PS7-only cmdlet fails legibly
     instead of silently.
  Neither existing means no PowerShell is installed; the response says so and
  names the two install paths rather than guessing further.

Process invocation uses an explicit argv list
(`[executable, "-NoProfile", "-NonInteractive", "-Command", command]`), never
`shell=True`. This matters more than it looks — on Windows, `subprocess` with
`shell=True` hard-codes `<executable> /c "<args>"` (cmd.exe's flag) onto
whatever `executable=` is set to, so it would hand PowerShell a `/c` it
doesn't understand instead of the `-Command` it actually wants. Passing the
argv list ourselves sidesteps that entirely, on every OS.

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
interpreters disagree about aliases in a way that changes behaviour silently.
Windows PowerShell 5.1 aliases `curl` and `wget` to `Invoke-WebRequest`, whose
arguments are nothing like the real tools', so `curl -s <url>` is a parameter
error there. PowerShell 7 removed both aliases, and Windows 10+ ships a real
`curl.exe`, so the same command works. `ls`, `cat`, `rm`, `cp`, `mv` and `ps`
are aliases in both. A command that behaves differently between two machines is
usually this.

Behaviour confirmed by running the tool against a real pwsh rather than assumed:
a cmdlet error and a failing native program both give exit code 1 (a non-zero
exit is not swallowed by the host), `-NonInteractive` makes `Read-Host` fail
rather than block, and an unrecognised command produces an explicit "The term
'x' is not recognized as a name of a cmdlet..." — which is what lets a
POSIX-ism be self-correcting rather than silently wrong.

Requires:  pwsh (https://github.com/PowerShell/PowerShell#get-powershell) on
any OS, or Windows PowerShell 5.1 (ships with Windows) as a fallback there.
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
            "Run a command in PowerShell. This is THE shell on this machine — "
            "there is no bash tool and no POSIX shell here, so write PowerShell "
            "(Get-ChildItem, Get-Content, Select-String, Test-Path, the object "
            "pipeline), not sh/bash syntax. Commands like `ls | grep foo`, "
            "`cat`, `rm -rf`, `which`, and `$(...)` substitution will fail or, "
            "worse, hit a PowerShell alias that behaves differently than the "
            "Unix tool of that name. Paths are native Windows paths "
            "(C:\\Users\\...), which is also what every other tool here returns "
            "and accepts. Prefers PowerShell 7+ (pwsh) and falls back to the "
            "Windows PowerShell 5.1 that ships with Windows; the response says "
            "which one ran, so a script using a 7-only cmdlet fails legibly. "
            "Returns JSON with the interpreter, exit code, and stdout/stderr. "
            "Each call is a fresh process: nothing persists between calls (no "
            "variables, no cwd) — chain with ';' or a pipeline within one call, "
            "and use the python tool when state must survive."
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
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
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
