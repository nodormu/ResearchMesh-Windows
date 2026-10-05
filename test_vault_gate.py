"""Tests for the vault name gate, the gopass timeout and the secret scrubbing.

    python test_vault_gate.py

A fake `gopass` script on PATH stands in for the real one, so these checks need
no GPG key and no password store. They cover this module's logic. The Windows
branch of the timeout cleanup (`taskkill /T`) is checked by its command line
only; ending a real process tree on Windows needs a hands-on run there.
"""

import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import processes as mod

FAILURES: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="rm-vault-test-"))

SCRIPT = """#!/bin/sh
if [ "$1" = "ls" ]; then cat "$FAKE_ENTRIES"; exit 0; fi
if [ "$1" = "show" ]; then
  case "$2" in
    hangs) sleep 60 & echo $! > "$FAKE_CHILD_PID"; wait; exit 0 ;;
    broken) echo "Error: gpg failed" >&2; exit 2 ;;
    *) echo "secret-$2"; echo "second line"; exit 0 ;;
  esac
fi
exit 1
"""


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def install_fake(entries: list[str]) -> None:
    bindir = WORK / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "gopass"
    fake.write_text(SCRIPT)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (WORK / "entries.txt").write_text("\n".join(entries) + "\n")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
    os.environ["FAKE_ENTRIES"] = str(WORK / "entries.txt")
    os.environ["FAKE_CHILD_PID"] = str(WORK / "child.pid")


def confirmed_after(text: str) -> set[str]:
    mod._confirmed_secret_entries.clear()
    mod.note_user_message(text)
    return set(mod._confirmed_secret_entries)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def main() -> int:
    install_fake(["github", "my-github", "aws/prod", "hangs", "broken"])

    print("the name gate")
    mod._confirmed_secret_entries.clear()
    value, error = mod.resolve_secret("github")
    check("an entry the user has not named is refused with the real entry list",
          value == "" and error is not None and error.startswith("please select the cred name I need to use:") and "github" in error, str(error))
    value, error = mod.resolve_secret("github")
    check("asking again does not confirm it", value == "" and "please select" in (error or "") and "github" not in mod._confirmed_secret_entries)
    value, error = mod.resolve_secret("?")
    check("a question mark is the selection prompt", value == "" and "please select" in (error or ""))
    check("a name in the user's message confirms it", confirmed_after("push it to github") == {"github"})
    check("the match is on the whole name, not inside a longer one", confirmed_after("use my-github") == {"my-github"})
    check("a name inside a longer word does not match", confirmed_after("githubx and xgithub") == set())
    check("a nested entry name matches", confirmed_after("log in with aws/prod now") == {"aws/prod"})
    check("a prefix of a nested name does not match", confirmed_after("the aws account") == set())
    check("an empty message confirms nothing and spawns nothing", confirmed_after("   ") == set())
    mod._confirmed_secret_entries.clear()
    mod.note_user_message("push it to github")
    value, error = mod.resolve_secret("github")
    check("a confirmed entry is decrypted, first line only", (value, error) == ("secret-github", None), f"{value!r} {error!r}")
    mod._confirmed_secret_entries.add("broken")
    value, error = mod.resolve_secret("broken")
    check("a gopass failure surfaces its own text and the entry list", value == "" and "gpg failed" in (error or "") and "Entries actually in the vault" in (error or ""), str(error))

    print("the same gate through a step")
    mod._confirmed_secret_entries.clear()
    check("an unconfirmed send_secret step is an error naming the entries",
          mod._resolve_reply({"expect": "x", "send_secret": "github"})[2] is not None and "please select" in (mod._resolve_reply({"expect": "x", "send_secret": "github"})[2] or ""))
    mod.note_user_message("github")
    check("a confirmed one returns the value as a secret", mod._resolve_reply({"expect": "x", "send_secret": "github"}) == ("secret-github", True, None))

    print("a gopass that never answers")
    mod._confirmed_secret_entries.add("hangs")
    saved = mod._SEND_SECRET_TIMEOUT
    mod._SEND_SECRET_TIMEOUT = 1
    pidfile = WORK / "child.pid"
    try:
        started = time.monotonic()
        value, error = mod.resolve_secret("hangs")
        elapsed = time.monotonic() - started
        check("it gives up after the timeout and says why", value == "" and "did not return within 1s" in (error or ""), str(error))
        check("it does not wait for the child", elapsed < 10, f"{elapsed:.1f}s")
        child = int(pidfile.read_text()) if pidfile.exists() else 0
        time.sleep(0.5)
        check("the background process gopass started is gone too", child > 0 and not alive(child), f"pid {child}")

        pidfile.unlink(missing_ok=True)
        real_kill = mod._kill_process_tree
        mod._kill_process_tree = lambda proc: proc.kill()
        try:
            mod.resolve_secret("hangs")
            survivor = int(pidfile.read_text()) if pidfile.exists() else 0
            time.sleep(0.5)
            survived = survivor > 0 and alive(survivor)
            check("control: killing only the parent would leave that process running", survived, f"pid {survivor}")
            if survived:
                os.kill(survivor, 9)
        finally:
            mod._kill_process_tree = real_kill
    finally:
        mod._SEND_SECRET_TIMEOUT = saved

    print("the Windows timeout cleanup")
    with mock.patch.object(subprocess, "run") as run:
        mod._taskkill_tree(4242)
    check("the whole tree is ended by taskkill", run.call_args[0][0] == ["taskkill", "/F", "/T", "/PID", "4242"], str(run.call_args))

    print("scrubbing")
    secret = "p@ss w&rd!/é"
    forms = mod._secret_forms(secret)
    check("encoded forms are derived", {secret} <= forms and len(forms) >= 5, str(forms))
    transcript = "\n".join(sorted(forms)) + "\nplain text stays"
    out = mod._redact(transcript, [secret])
    check("the value and every encoded form are removed", all(f not in out for f in forms), out)
    check("other text is untouched", "plain text stays" in out)
    check("an empty value changes nothing", mod._redact("abc", [""]) == "abc")
    check("a longer secret is replaced whole, not left half-replaced by a shorter one inside it", mod._redact("x abcdef y", ["abc", "abcdef"]) == "x *** y", mod._redact("x abcdef y", ["abc", "abcdef"]))

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import shutil

        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(code)
