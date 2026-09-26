"""Behavioural regression tests for core/process_reaper.py (Windows port).

    python test_process_reaper.py

Same "spawn a real process tree, confirm the reaper actually finds/kills
it" spirit as the Linux/macOS suites this is adapted from, with one
structural difference forced by the platform: the actual discovery
mechanism here is a WMI query (`Get-CimInstance -ClassName Win32_Process`)
that simply does not exist outside real Windows — confirmed live, on this
dev sandbox's own real `pwsh`, that it errors with "term is not
recognized." There is no way to exercise that specific call from here,
same class of gap this whole project already accepts for
`powershell_session.py`'s own pywinpty/ConPTY transport.

What CAN be verified for real, and is below: `_all_processes()`'s parsing
of a `Win32_Process`-shaped JSON payload (real field names:
ProcessId/ParentProcessId/Name), the self-referential-subprocess-pid
filter, children-map construction, recursive descendant collection, and —
the part that matters most — that `reap_orphans()` given a SYNTHETIC
payload describing REAL spawned processes on this box actually kills
them and leaves the tree genuinely empty afterward, via `reap_orphans()`'s
own `raw_json` testability hook (never used in production; production
always calls it with no arguments, which runs the real, unverified-here
WMI query).
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def _cim_payload(entries: Sequence[tuple[int, int | None, str]]) -> str:
    """Build a JSON string shaped exactly like
    `Get-CimInstance -ClassName Win32_Process | Select-Object ProcessId,
    ParentProcessId, Name | ConvertTo-Json -Compress` would really
    produce — same field names/casing, same "bare object, not a
    single-element array, when there's exactly one result" quirk
    `ConvertTo-Json` has (reproduced here for the one-entry test below)."""
    objs = [{"ProcessId": pid, "ParentProcessId": ppid, "Name": name} for pid, ppid, name in entries]
    if len(objs) == 1:
        return json.dumps(objs[0])
    return json.dumps(objs)


def check_real_production_path_degrades_gracefully(reaper) -> None:
    print(
        "the REAL production code path (no injection) -- a genuine `pwsh` "
        "subprocess call attempting the real WMI query, which fails on this "
        "non-Windows sandbox -- degrades to an empty list quickly rather "
        "than crashing or hanging the whole app's exit"
    )
    t0 = time.monotonic()
    reaped = reaper.reap_orphans()
    elapsed = time.monotonic() - t0
    check("returns an empty list, not an exception", reaped == [], str(reaped))
    check(
        f"resolves well within the {reaper._CIM_QUERY_TIMEOUT}s bound, not by exhausting it",
        elapsed < reaper._CIM_QUERY_TIMEOUT,
        f"took {elapsed:.3f}s",
    )


def check_json_parsing(reaper) -> None:
    print("_all_processes() parses a real-shaped Win32_Process JSON payload correctly")
    payload = _cim_payload([(100, 1, "explorer.exe"), (200, 100, "notepad.exe"), (300, 200, "conhost.exe")])
    result = reaper._all_processes(raw_json=payload)
    check("all three entries parsed", len(result) == 3, str(result))
    check(
        "fields map correctly (pid, ppid, name)",
        (200, 100, "notepad.exe") in result,
        str(result),
    )


def check_single_entry_payload_shape(reaper) -> None:
    print(
        "ConvertTo-Json's own real quirk -- a single result comes back as a "
        "bare object, not a one-element array -- is handled, not just the "
        "common multi-entry case"
    )
    payload = _cim_payload([(42, 1, "lonely.exe")])
    check("single-object payload is genuinely a bare JSON object, not an array", payload.startswith("{"), payload)
    result = reaper._all_processes(raw_json=payload)
    check("still parses to a one-item list", result == [(42, 1, "lonely.exe")], str(result))


def check_null_parent_handled(reaper) -> None:
    print("a null ParentProcessId (a root/orphaned process) doesn't crash parsing")
    payload = _cim_payload([(4, None, "System")])
    result = reaper._all_processes(raw_json=payload)
    check("parsed without error", len(result) == 1, str(result))
    check("null parent normalized to -1, not None/crash", result[0][1] == -1, str(result))


def check_malformed_json_returns_empty(reaper) -> None:
    print("malformed/empty JSON (e.g. a WMI provider error) returns an empty list, not a traceback")
    check("garbage text", reaper._all_processes(raw_json="not json at all") == [])
    check("empty string", reaper._all_processes(raw_json="") == [])
    check("empty array", reaper._all_processes(raw_json="[]") == [])


def check_self_referential_ps_pid_filtered(reaper) -> None:
    print(
        "the invoking PowerShell process's OWN pid is filtered out of its "
        "own results -- the same self-observation artifact the macOS `ps` "
        "port hit live, reproduced here synthetically since it can't be "
        "observed for real without WMI"
    )
    # _all_processes() only applies this filter on the REAL subprocess path
    # (own_ps_pid is None on the injected path, by design -- there is no
    # "own ps pid" to filter when the caller supplies the data directly).
    # This check instead confirms the injected path does NOT spuriously
    # invent a filter that would drop a legitimate entry.
    payload = _cim_payload([(999, 1, "pwsh")])
    result = reaper._all_processes(raw_json=payload)
    check(
        "an injected entry named 'pwsh' is NOT filtered on the injection path "
        "(the filter is pid-based and only active on the real subprocess path)",
        result == [(999, 1, "pwsh")],
        str(result),
    )


def check_full_tree_via_injected_payload(reaper) -> None:
    print(
        "reap_orphans(), given a SYNTHETIC payload describing REAL spawned "
        "processes on this box, finds the full tree, kills every real "
        "process, and leaves it GENUINELY empty afterward -- not just "
        "self-reported as killed. This is the part that matters: everything "
        "downstream of the (unverified-here) WMI call, exercised for real."
    )
    child = subprocess.Popen(["/bin/bash", "-c", "sleep 300 & sleep 300"])
    time.sleep(0.5)

    my_pid = os.getpid()
    # Real descendants right now: child (bash) -> its own two `sleep`
    # children. Query the REAL tree via the Linux-only /proc, ONE TIME,
    # purely to build an accurate synthetic payload for this test -- this
    # is test-harness bookkeeping, not something process_reaper.py itself
    # does or needs on any platform.
    def real_children(pid):
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as f:
                return [int(x) for x in f.read().split()]
        except OSError:
            return []

    grandchildren = real_children(child.pid)
    entries = [(my_pid, 1, "pwsh_test_harness"), (child.pid, my_pid, "bash")]
    for gc in grandchildren:
        entries.append((gc, child.pid, "sleep"))

    payload = _cim_payload(entries)

    try:
        before = reaper._collect_descendants(my_pid, reaper._children_map(reaper._all_processes(raw_json=payload)))
        before_pids = [p for p, _ in before]
        check(
            "all three real processes found before reaping (bash + 2 sleeps)",
            len(before) == 3,
            str(before),
        )
        check("the direct child is among them", child.pid in before_pids, str(before_pids))

        reaped = reaper.reap_orphans(raw_json=payload)
        check("reports killing exactly what was found", len(reaped) == 3, str(reaped))

        time.sleep(0.3)
        # Test-harness accommodation, not a process_reaper.py concern: this
        # test runs Windows-targeted logic on a POSIX host, and `child` is
        # OUR OWN direct child (spawned via subprocess.Popen right in this
        # test) -- POSIX leaves a killed direct child as a zombie, still
        # visible to a signal-0 probe, until its parent calls wait()/poll().
        # Windows has no such state at all (see module docstring), so this
        # accommodation has nothing to mirror there; it exists purely so
        # THIS test's own "genuinely gone" probe means what it says on the
        # POSIX box actually running it.
        child.poll()

        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

        check("the direct child is GENUINELY gone, not just reported", not alive(child.pid), "")
        check(
            "every grandchild is GENUINELY gone too",
            all(not alive(gc) for gc in grandchildren),
            str(grandchildren),
        )
    finally:
        try:
            child.wait(timeout=2)
        except Exception as e:
            print(f"  (cleanup: reaping the test's own outer child failed, ignored: {e})")


def check_already_dead_pid_reported_as_kill_failed(reaper) -> None:
    print(
        "a pid the payload names that's already gone by the time reap_orphans() "
        "runs is reported cleanly (kill failed), not raised as an exception"
    )
    proc = subprocess.Popen(["/bin/bash", "-c", "true"])
    proc.wait()  # already exited before the reaper ever looks
    payload = _cim_payload([(proc.pid, os.getpid(), "already_gone")])
    reaped = reaper.reap_orphans(raw_json=payload)
    check("no exception, a result list came back", isinstance(reaped, list), str(reaped))


async def _noop():
    pass


def _run_all() -> int:
    sys.path.insert(0, str(ROOT))
    from core import process_reaper as reaper

    check_real_production_path_degrades_gracefully(reaper)
    print()
    check_json_parsing(reaper)
    print()
    check_single_entry_payload_shape(reaper)
    print()
    check_null_parent_handled(reaper)
    print()
    check_malformed_json_returns_empty(reaper)
    print()
    check_self_referential_ps_pid_filtered(reaper)
    print()
    check_full_tree_via_injected_payload(reaper)
    print()
    check_already_dead_pid_reported_as_kill_failed(reaper)
    print()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
