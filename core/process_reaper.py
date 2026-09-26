"""Last-line safety net for process exit: verify the real OS child-process
tree is actually empty, independent of what any individual tool's own
`shutdown()`/`close()` claims to have cleaned up.

Windows port of the Linux `core/process_reaper.py` (which itself has a
separate macOS port, using `ps`, since Darwin has no `/proc` either).
Windows has neither `/proc` nor a `ps` binary, so discovery goes through
WMI via `Get-CimInstance -ClassName Win32_Process` — the standard,
long-established mechanism for exactly this (`ParentProcessId` has been a
plain, documented property of that class since PowerShell 3.0/2012,
unlike the newer cross-platform `System.Diagnostics.Process.Parent`
property, which was evaluated first and REJECTED: confirmed live, on
this dev sandbox's own real PowerShell 7, that bulk `Get-Process` leaves
`.Parent` null for nearly every entry, including the calling process's
own — reliable only for a single freshly-`-Id`-filtered lookup, not for
walking a whole tree. `Get-CimInstance`'s `ParentProcessId` has none of
that flakiness; it is the field every "map a Windows process tree" script
already reaches for.)

Every local tool that spawns a subprocess (powershell_session's
persistent PowerShell, the IPython kernel, Playwright's browser, an MCP
server launched via stdio) is responsible for closing its own resource,
and does. This module doesn't replace that — it runs LAST, after all of
it, and checks the one thing none of those individually can: whether
anything is *still* alive regardless. That catches what none of them can
see on their own — a background job a `powershell_session` command
started that outlives the tool call by design, or a future tool that
spawns a subprocess without wiring up its own cleanup at all.

**Genuinely unverified on this Linux dev sandbox, same honesty standard
as `powershell_session.py`'s own pywinpty/ConPTY transport**: WMI does
not exist outside Windows at all (`Get-CimInstance` errors with "term is
not recognized" here, confirmed live), so the actual production
`_all_processes()` call path — the real CIM query — has never run for
real. What HAS been verified here, against real spawned process trees on
this sandbox: every step AFTER that call — JSON parsing (using a
synthetic payload shaped exactly like a real `Win32_Process` selection,
same field names), the self-referential-subprocess-pid filter (see
below), children-map construction, recursive descendant collection, and
the actual `os.kill()`-based termination + verification that real
processes are genuinely gone afterward. Same class of gap this whole
multi-fork project already documents elsewhere (real hardware needed for
full confidence) — not hidden, not assumed away.

Also genuinely unverified: `os.kill(pid, signal.SIGTERM)`'s actual
Windows behavior. Per CPython's own documented behavior, passing any
signal value OTHER than `signal.CTRL_C_EVENT`/`signal.CTRL_BREAK_EVENT`
to `os.kill()` on Windows calls the Win32 `TerminateProcess` API
unconditionally — an immediate, non-negotiable kill with no equivalent
to a POSIX process installing its own SIGTERM handler to catch or ignore
it. That is exactly the "no negotiation" semantic this module wants
(matching the Linux/macOS originals' own use of SIGKILL specifically,
never SIGTERM, for the same reason) — but it runs through a completely
different OS code path than either of those, and cannot be exercised for
real from here.

No zombie-reaping step, unlike the Linux/macOS originals — deliberately,
not an oversight. POSIX zombies exist because a terminated process stays
in the process table until its parent calls `wait()`/`waitpid()` to
collect its exit status; Windows has no equivalent concept for a process
this one didn't itself spawn via `subprocess.Popen`; a terminated
process's resources are reclaimed once every open handle to it is
closed, which is unrelated to any parent-child relationship recorded in
the CIM process table. `os.waitpid()` does not even exist in CPython's
`os` module on Windows (it is conditionally exposed only on POSIX)
confirming there is nothing analogous to call here.

Requires:  the same PowerShell already required by
           `core/powershell.py`/`core/powershell_session.py` — no new
           dependency. Uses whichever of `pwsh`/legacy `powershell.exe`
           `core/powershell.py`'s own `_powershell_executable()` would
           find, duplicated here rather than imported (this fork's own
           established convention — see that module and
           `core/powershell_session.py`'s matching duplicate, both with
           the identical "each local tool module stands alone" note).
"""

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

# Duplicated from core/powershell.py's own `_powershell_executable()` --
# same convention that module and core/powershell_session.py already
# follow, not an oversight. See either of those for the fuller rationale.
_WIN_LEGACY_PATH = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# One WMI query over every process on the box can genuinely be slower
# than Linux's `/proc` walk or macOS's `ps` call -- confirmed live on this
# sandbox's own hundred-ish real processes that a comparable `pwsh`
# round-trip already costs a meaningful fraction of a second; a real
# Windows box under load could reasonably take longer. Bounded, not
# unbounded, so a stuck WMI provider can't hang the whole app's exit.
_CIM_QUERY_TIMEOUT = 10

_CIM_COMMAND = (
    "Get-CimInstance -ClassName Win32_Process | "
    "Select-Object ProcessId, ParentProcessId, Name | "
    "ConvertTo-Json -Compress"
)


def _powershell_executable() -> str | None:
    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh
    legacy = shutil.which("powershell")
    if legacy:
        return legacy
    if Path(_WIN_LEGACY_PATH).is_file():
        return _WIN_LEGACY_PATH
    return None


def _all_processes(raw_json: str | None = None) -> list[tuple[int, int, str]]:
    """Every process currently running, as (pid, ppid, name) triples.

    `raw_json` lets a caller (namely this module's own tests) inject an
    already-captured payload instead of running the real WMI query --
    the only way to exercise the parsing/filtering logic on a platform
    that has no WMI at all. Production code never passes it; it is
    fetched for real, every call, exactly like the Linux/macOS versions'
    own `/proc`/`ps` reads.

    Deliberately excludes the invoking PowerShell process itself from the
    returned list, for the identical reason the macOS port excludes its
    own `ps` subprocess: querying this process's own descendants
    necessarily spawns one MORE descendant (the PowerShell process
    running the query itself), which is still alive, as a real child of
    this process, at the exact moment it captures the snapshot --
    confirmed live (via the macOS port's own equivalent bug, since this
    exact artifact cannot be observed here without real WMI) that an
    unfiltered version would self-report as a spurious "leftover."
    Filtered by that subprocess's own pid specifically, not by name, so a
    real, unrelated PowerShell process some other tool legitimately
    spawned still counts.
    """
    own_ps_pid = None
    if raw_json is None:
        executable = _powershell_executable()
        if executable is None:
            return []
        try:
            proc = subprocess.Popen(
                [executable, "-NoLogo", "-NoProfile", "-Command", _CIM_COMMAND],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            own_ps_pid = proc.pid
            raw_json, _ = proc.communicate(timeout=_CIM_QUERY_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return []

    try:
        data = json.loads(raw_json) if raw_json and raw_json.strip() else []
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):  # ConvertTo-Json drops the array wrapper for one object
        data = [data]

    processes = []
    for entry in data:
        try:
            pid = int(entry["ProcessId"])
            ppid_raw = entry.get("ParentProcessId")
            ppid = int(ppid_raw) if ppid_raw is not None else -1
        except (KeyError, TypeError, ValueError):
            continue
        if pid == own_ps_pid:
            continue
        name = entry.get("Name") or "?"
        processes.append((pid, ppid, name))
    return processes


def _children_map(processes: list[tuple[int, int, str]]) -> dict[int, list[tuple[int, str]]]:
    """ppid -> [(pid, name), ...], built once from one snapshot rather than
    re-querying per pid."""
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, ppid, name in processes:
        children.setdefault(ppid, []).append((pid, name))
    return children


def _collect_descendants(
    pid: int, children: dict[int, list[tuple[int, str]]] | None = None
) -> list[tuple[int, str]]:
    """Every process still alive under `pid`, at every depth. Collected
    fully before anything is killed, so kill order can't cause a deeper
    descendant to be missed.

    `children` is not required — pass the map from an already-taken
    `_all_processes()`/`_children_map()` snapshot to reuse it across
    several calls (what `reap_orphans()` does, one snapshot for the whole
    operation); omit it for a one-off check (tests calling this directly)
    and a fresh snapshot is taken internally.
    """
    if children is None:
        children = _children_map(_all_processes())
    descendants: list[tuple[int, str]] = []
    for child_pid, name in children.get(pid, []):
        descendants.append((child_pid, name))
        descendants.extend(_collect_descendants(child_pid, children))
    return descendants


def reap_orphans(raw_json: str | None = None) -> list[str]:
    """Force-kill every real descendant process still alive, regardless of
    what any tool's own cleanup believes it already handled. Returns a
    short description of each thing actually found and killed — empty if
    the tree was already clean, which is the expected common case.

    `os.kill(pid, signal.SIGTERM)` on Windows calls `TerminateProcess`
    unconditionally (see module docstring) — no zombie-reaping step
    follows, unlike the Linux/macOS originals, because Windows has no
    equivalent concept for a process this one didn't itself spawn.

    `raw_json` is the same testability hook `_all_processes()` takes —
    forwarded straight through, never used in production. Production
    callers (`main.py`) always call this with no arguments.
    """
    pid = os.getpid()
    children = _children_map(_all_processes(raw_json))
    reaped = []
    for descendant_pid, name in _collect_descendants(pid, children):
        try:
            os.kill(descendant_pid, signal.SIGTERM)
            reaped.append(f"{name}(pid {descendant_pid})")
        except ProcessLookupError:
            continue  # died on its own between the snapshot and the kill -- fine
        except OSError as e:
            reaped.append(f"{name}(pid {descendant_pid}, kill failed: {e})")
    return reaped
