"""Behavioural regression tests for core/processes.py.

    python test_processes.py

Covers `interactive_run`'s `send_env`/`send_secret` step fields — added so a
password/token prompt can be answered without the real value ever having to
be written into the tool call itself (see the module's own docstring for the
full rationale).

TWO THINGS ARE FAKED HERE, both for the same underlying reason: this suite
has to run in CI on `ubuntu-latest` as well as by hand on whatever box a
contributor is using, and neither `pywinpty` (wraps native Windows ConPTY
APIs — genuinely cannot exist on Linux/macOS, not merely "not installed")
nor a real `gopass` vault is available/deterministic there:

  * `winpty` itself is faked — a tiny `PtyProcess` stand-in, backed by a
    REAL `pexpect`-spawned `/bin/bash` child instead of real ConPTY/cmd.exe,
    installed into `sys.modules['winpty']` for the duration of each call.
    This means every check below runs the REAL, unmodified `_run()` — the
    resolve-up-front loop, `secrets_to_scrub` seeding, the `is_secret`-driven
    transcript branch, `_Reader`'s polling loop, all of it — for real, not a
    re-implementation. What it does NOT verify is anything ConPTY/cmd.exe
    -specific (the `\\r`-not-`\\r\\n` write, `["cmd.exe", "/c", ...]`
    wrapping, real Windows echo behavior) — that half is untestable off a
    real Windows box, same limitation this fork's own CI already accepts
    (see `.github/workflows/ci.yml`'s own reasoning for keeping smoke_test.py
    platform-agnostic rather than exercising real winpty on either OS leg).
    Commands below are therefore written in bash syntax, run by the fake's
    stand-in shell, not actual cmd.exe/PowerShell syntax.

  * `send_secret` shells out to a REAL `gopass` binary, but this suite does
    not depend on a real GPG key/password-store being set up — a tiny fake
    `gopass` script (plain shell, no gpg at all) is placed on `PATH` ahead of
    any real one for the duration of these specific checks, giving fully
    deterministic, fast, CI-safe coverage of `_resolve_reply`'s own logic
    (found entry, missing entry, `gopass` altogether absent, a hung/
    unanswerable prompt) without ever touching real encryption or timing on
    an actual passphrase cache. Same technique the Linux/Mac originals use
    for `pass`.
"""

import asyncio
import json
import os
import shlex
import stat
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


class _FakeWinpty:
    """Installed as `sys.modules['winpty']` for the duration of a `with`
    block, so `core.processes._run()`'s own `import winpty` succeeds on a
    non-Windows box and its REAL body runs unmodified. `PtyProcess.spawn`
    receives exactly what production code sends — `["cmd.exe", "/c",
    command]` — and translates just that one shape to a real `pexpect`-
    spawned `/bin/bash -c <command>` child instead; `read`/`write`/
    `isalive`/`terminate`/`close`/`exitstatus` are thin pass-throughs to
    that real child, close enough to pywinpty's own documented shape
    (blocking-only `read`, no timeout param) for `_Reader`'s polling loop
    to drive without caring which transport is really underneath.
    """

    class _FakePtyProcess:
        def __init__(self, child):
            self._child = child

        @classmethod
        def spawn(cls, argv):
            import pexpect

            if len(argv) >= 3 and argv[0].lower() in ("cmd.exe", "cmd") and argv[1] == "/c":
                command = argv[2]
            else:
                command = " ".join(argv)
            child = pexpect.spawn(
                "/bin/bash",
                ["-c", command],
                encoding="utf-8",
                codec_errors="replace",
                echo=False,
                timeout=None,
            )
            return cls(child)

        def read(self, n=4096):
            import pexpect

            try:
                return self._child.read_nonblocking(size=n, timeout=None)
            except pexpect.EOF as e:
                raise EOFError() from e

        def write(self, data):
            self._child.send(data)

        def isalive(self):
            return self._child.isalive()

        def terminate(self, force=False):
            try:
                self._child.terminate(force=force)
            except Exception as e:
                print(f"[test_processes fake winpty] terminate failed (ignored): {e}")

        def close(self, force=False):
            try:
                self._child.close(force=force)
            except Exception as e:
                print(f"[test_processes fake winpty] close failed (ignored): {e}")

        @property
        def exitstatus(self):
            return self._child.exitstatus

    def __enter__(self):
        fake_module = types.ModuleType("winpty")
        fake_module.PtyProcess = self._FakePtyProcess
        self._old = sys.modules.get("winpty")
        sys.modules["winpty"] = fake_module
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            sys.modules["winpty"] = self._old
        else:
            sys.modules.pop("winpty", None)


def call(mod, tool_input: dict) -> dict:
    with _FakeWinpty():
        result = asyncio.run(mod.execute("interactive_run", tool_input))
    return json.loads(result)


# Bash syntax — the fake winpty's stand-in child is a real /bin/bash, not
# cmd.exe. See module docstring: real cmd.exe/ConPTY-specific behavior is
# untestable here regardless of what these commands say.
PROMPT_CMD = 'read -s -p "Enter: " val; echo "GOT:[$val]"'


def match_cmd(expected: str) -> str:
    """A prompt whose own script compares the received value against
    `expected` INSIDE the shell, printing only MATCH/MISMATCH — never the
    real value itself. Used for "did the child receive the correct value"
    checks now that redaction correctly scrubs every occurrence of a secret
    (see `check_secret_redacted_even_when_echoed_back_later`): a test that
    verified correctness by echoing the raw value back and inspecting the
    transcript would be checking for something redaction is now supposed to
    remove, which is backwards. `PROMPT_CMD`'s echo-back style is kept
    on purpose for the one test that specifically needs a secret to leak
    into unrelated output, to prove redaction now catches it anyway.
    """
    return (
        f'read -s -p "Enter: " val; '
        f'if [ "$val" = {shlex.quote(expected)} ]; then echo "GOT:MATCH"; '
        f'else echo "GOT:MISMATCH"; fi'
    )


def check_existing_literal_send_unaffected(mod) -> None:
    print("existing behavior: literal `send` + `secret` unaffected")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send": "plaintext123", "secret": False}],
    })
    check("no error", "error" not in r, str(r))
    check("child received the literal value", "GOT:[plaintext123]" in r.get("transcript", ""), str(r))
    check("non-secret send appears in transcript", "plaintext123" in r.get("transcript", "").split("GOT:")[0], str(r))

    r = call(mod, {
        "command": match_cmd("shouldberedacted"),
        "steps": [{"expect": "Enter: ", "send": "shouldberedacted", "secret": True}],
    })
    check("secret:true still redacts the echoed send", "shouldberedacted" not in r.get("transcript", ""), str(r))
    check("child still received the real value despite redaction", "GOT:MATCH" in r.get("transcript", ""), str(r))


def check_send_env_happy_path(mod) -> None:
    print("send_env: real value reaches the child, never appears unredacted in transcript")
    os.environ["_TEST_INTERACTIVE_RUN_SECRET"] = "s3cr3t-from-env-9f8a"
    try:
        r = call(mod, {
            "command": match_cmd("s3cr3t-from-env-9f8a"),
            "steps": [{"expect": "Enter: ", "send_env": "_TEST_INTERACTIVE_RUN_SECRET"}],
        })
        check("no error", "error" not in r, str(r))
        check("child received the real env value", "GOT:MATCH" in r.get("transcript", ""), str(r))
        check("real value NOT anywhere in the transcript", "s3cr3t-from-env-9f8a" not in r.get("transcript", ""), str(r))
        check("redaction marker present instead", "***" in r.get("transcript", ""), str(r))
    finally:
        del os.environ["_TEST_INTERACTIVE_RUN_SECRET"]


def check_send_env_missing_var(mod) -> None:
    print("send_env: missing variable fails clearly, before spawning anything")
    assert "_TEST_INTERACTIVE_RUN_DEFINITELY_UNSET" not in os.environ
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_env": "_TEST_INTERACTIVE_RUN_DEFINITELY_UNSET"}],
    })
    check("returns an error", "error" in r, str(r))
    check("error names the missing variable", "_TEST_INTERACTIVE_RUN_DEFINITELY_UNSET" in r.get("error", ""), str(r))
    check("no transcript/exit_status leaked through (never spawned)", "transcript" not in r, str(r))


class _FakeGopassOnPath:
    """Puts a tiny fake `gopass` script on `PATH`, ahead of any real one,
    for the duration of a `with` block. Plain shell, zero gpg dependency —
    `show existing-entry` prints a known value, `show sleeps-forever`
    blocks forever (simulating an unanswerable Gpg4win pinentry dialog),
    `ls --flat` prints a plain one-per-line list (no tree-drawing, matching
    real `gopass`'s own `--flat` output shape), anything else fails with
    the same shape of stderr message a real `gopass` would give.
    """

    SCRIPT = """#!/bin/sh
if [ "$1" = "show" ]; then
    case "$2" in
        existing-entry) echo "fake-secret-value-9k2m"; exit 0 ;;
        fresh-unconfirmed-entry) echo "fake-secret-value-9k2m"; exit 0 ;;
        sleeps-forever) sleep 999; exit 0 ;;
        *) echo "Error: entry $2 is not in the password store." >&2; exit 1 ;;
    esac
fi
if [ "$1" = "ls" ]; then
    echo "existing-entry"
    exit 0
fi
exit 1
"""

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp()
        path = os.path.join(self._tmpdir, "gopass")
        with open(path, "w") as f:
            f.write(self.SCRIPT)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = self._tmpdir + os.pathsep + self._old_path
        return self

    def __exit__(self, *exc):
        os.environ["PATH"] = self._old_path


def confirm(mod, *names: str) -> None:
    """Mark entry name(s) as already-confirmed, bypassing the real two-call
    present-then-use flow for tests that are checking something OTHER than
    that flow itself (exit-code fidelity, error surfacing, etc.) — see
    `check_first_reference_always_forces_selection` for the dedicated test
    of the confirmation gate itself. Without this, every other send_secret
    test would need two throwaway calls just to get past a gate unrelated
    to what it's actually testing.
    """
    mod._confirmed_secret_entries.update(names)


def check_send_secret_happy_path(mod) -> None:
    print("send_secret: real value reaches the child, never appears unredacted in transcript")
    confirm(mod, "existing-entry")
    with _FakeGopassOnPath():
        r = call(mod, {
            "command": match_cmd("fake-secret-value-9k2m"),
            "steps": [{"expect": "Enter: ", "send_secret": "existing-entry"}],
        })
    check("no error", "error" not in r, str(r))
    check("child received the value gopass show printed", "GOT:MATCH" in r.get("transcript", ""), str(r))
    check("real value NOT anywhere in the transcript", "fake-secret-value-9k2m" not in r.get("transcript", ""), str(r))
    check("redaction marker present instead", "***" in r.get("transcript", ""), str(r))


def check_send_secret_missing_entry(mod) -> None:
    print("send_secret: entry not in the store fails clearly, before spawning anything")
    confirm(mod, "no-such-entry")
    with _FakeGopassOnPath():
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "no-such-entry"}],
        })
    check("returns an error", "error" in r, str(r))
    check("error surfaces gopass's own stderr text", "not in the password store" in r.get("error", ""), str(r))
    check("no transcript/exit_status leaked through (never spawned)", "transcript" not in r, str(r))


def check_send_secret_missing_entry_shows_real_available_entries(mod) -> None:
    print("send_secret: a wrong/guessed entry name's error includes the "
          "REAL list of what's actually in the vault (gopass ls --flat), "
          "so a hallucinated or mistyped name doesn't just fail blind")
    confirm(mod, "totally-made-up-name")
    with _FakeGopassOnPath():
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "totally-made-up-name"}],
        })
    check("returns an error", "error" in r, str(r))
    check("error includes the real available entry name", "existing-entry" in r.get("error", ""), str(r))
    check("error is clearly labeled as the real vault contents", "Entries actually in the vault" in r.get("error", ""), str(r))


def check_send_secret_gopass_not_installed(mod) -> None:
    print("send_secret: gopass genuinely absent from PATH fails clearly")
    confirm(mod, "anything")
    old_path = os.environ["PATH"]
    try:
        os.environ["PATH"] = "/nonexistent-empty-dir"
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "anything"}],
        })
    finally:
        os.environ["PATH"] = old_path
    check("returns an error", "error" in r, str(r))
    check("error says gopass isn't installed", "not installed" in r.get("error", ""), str(r))


def check_send_secret_timeout(mod) -> None:
    print("send_secret: an unanswerable prompt times out with a clear "
          "message instead of hanging for the full interactive_run timeout")
    confirm(mod, "sleeps-forever")
    old_timeout = mod._SEND_SECRET_TIMEOUT
    mod._SEND_SECRET_TIMEOUT = 1
    try:
        with _FakeGopassOnPath():
            r = call(mod, {
                "command": PROMPT_CMD,
                "steps": [{"expect": "Enter: ", "send_secret": "sleeps-forever"}],
            })
    finally:
        mod._SEND_SECRET_TIMEOUT = old_timeout
    check("returns an error", "error" in r, str(r))
    check("error explains the likely cause (unanswerable passphrase prompt)", "passphrase" in r.get("error", ""), str(r))
    check("no transcript leaked through (never spawned)", "transcript" not in r, str(r))


def check_secret_redacted_even_when_echoed_back_later(mod) -> None:
    print("regression: a secret value is scrubbed EVERYWHERE in the "
          "transcript, not just on the line where it was sent -- this is "
          "a real bug the Linux/Mac originals caught live, checked here too")
    os.environ["_TEST_ECHO_BACK_SECRET"] = "Jum@nji23Suck$2#"
    try:
        r = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_env": "_TEST_ECHO_BACK_SECRET"}],
        })
    finally:
        del os.environ["_TEST_ECHO_BACK_SECRET"]
    check("no error", "error" not in r, str(r))
    transcript = r.get("transcript", "")
    check(
        "real value does not appear ANYWHERE, including the child's own later echo",
        "Jum@nji23Suck$2#" not in transcript,
        transcript,
    )
    check("both occurrences (send line AND echoed-back line) show the redaction marker",
          transcript.count("***") == 2, transcript)


def check_first_reference_always_forces_selection(mod) -> None:
    print("send_secret: a DIRECT, CORRECT, real entry name is still refused "
          "on its first-ever reference -- same incident/rationale as the "
          "Linux/Mac originals: nothing should skip straight to using the "
          "only entry that exists, with no '?' involved at all")
    with _FakeGopassOnPath():
        r1 = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "fresh-unconfirmed-entry"}],
        })
    check("first reference is refused, not used, even though it's a real correct name",
          "error" in r1, str(r1))
    check("refusal is the exact same selection prompt \"?\" produces",
          "please select the cred name I need to use:" in r1.get("error", ""), str(r1))
    check("no transcript leaked through on the refused first attempt",
          "transcript" not in r1, str(r1))

    with _FakeGopassOnPath():
        r2 = call(mod, {
            "command": match_cmd("fake-secret-value-9k2m"),
            "steps": [{"expect": "Enter: ", "send_secret": "fresh-unconfirmed-entry"}],
        })
    check("SAME name, second reference, now proceeds for real",
          "error" not in r2, str(r2))
    check("and actually works correctly once confirmed",
          "GOT:MATCH" in r2.get("transcript", ""), str(r2))


def check_send_secret_select_sentinel(mod) -> None:
    print("send_secret: \"?\" returns the exact hardcoded selection prompt, "
          "built from REAL vault entries via `gopass ls --flat` -- not "
          "something composed on the fly")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_secret": "?"}],
    })
    err = r.get("error", "")
    # No _FakeGopassOnPath here deliberately -- "?" is meant to be checked
    # BEFORE any real send_secret is confirmed, and a bare "?" with no
    # gopass on PATH at all should still fail cleanly rather than crash.
    check("returns an error (step never proceeds)", "error" in r, str(r))
    check("exact opening line present, or a clean 'not installed' fallback",
          ("please select the cred name I need to use:" in err) or ("not installed" in err), err)
    check("no transcript leaked through (never spawned)", "transcript" not in r, str(r))

    with _FakeGopassOnPath():
        r2 = call(mod, {
            "command": PROMPT_CMD,
            "steps": [{"expect": "Enter: ", "send_secret": "?"}],
        })
    err2 = r2.get("error", "")
    check("with a real (fake) vault present: exact opening line", "please select the cred name I need to use:" in err2, err2)
    check("with a real (fake) vault present: real entry listed", "existing-entry" in err2, err2)


def check_send_file_not_available(mod) -> None:
    print("send_file does not exist as an option -- treated as an unknown "
          "field, resolved as if only send/send_env were considered")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_file": "/tmp/whatever"}],
    })
    check("no send/send_env present -> error, send_file is simply ignored", "error" in r, str(r))
    check("error does not treat send_file as a valid source", "send_file" not in r.get("error", "") or "must include" in r.get("error", ""), str(r))


def check_conflicting_and_missing_sources(mod) -> None:
    print("validation: exactly one of send/send_env/send_secret required")
    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send": "a", "send_env": "PATH"}],
    })
    check("both send + send_env is an error", "error" in r, str(r))

    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: ", "send_env": "PATH", "send_secret": "x"}],
    })
    check("both send_env + send_secret is an error", "error" in r, str(r))

    r = call(mod, {
        "command": PROMPT_CMD,
        "steps": [{"expect": "Enter: "}],
    })
    check("neither send nor send_env/send_secret is an error", "error" in r, str(r))


def main() -> int:
    import core.processes as mod

    check_existing_literal_send_unaffected(mod)
    print()
    check_send_env_happy_path(mod)
    print()
    check_send_env_missing_var(mod)
    print()
    check_first_reference_always_forces_selection(mod)
    print()
    check_send_secret_happy_path(mod)
    print()
    check_send_secret_missing_entry(mod)
    print()
    check_send_secret_missing_entry_shows_real_available_entries(mod)
    print()
    check_send_secret_select_sentinel(mod)
    print()
    check_send_secret_gopass_not_installed(mod)
    print()
    check_send_secret_timeout(mod)
    print()
    check_secret_redacted_even_when_echoed_back_later(mod)
    print()
    check_send_file_not_available(mod)
    print()
    check_conflicting_and_missing_sources(mod)

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
