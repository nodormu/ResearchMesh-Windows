"""Fast sanity checks — no API key, no network, no per-tool backing packages needed.

    python smoke_test.py

This is not a test suite and does not pretend to be one. There is no pytest, no
fixtures, and nothing here exercises a tool's actual behaviour (that needs
LibreOffice, a browser, a real desktop and real API credits). What it does check
is the wiring that breaks silently and that nothing else catches:

  1. every module imports at all
  2. the tool registry is well-formed and free of duplicate names
  3. the tool count the docs claim still matches reality
  4. mcp_server.py completes an MCP handshake and advertises `delegate`

(3) exists because this project states its tool count in enough places, phrased
several different ways, that hand-checking them drifts silently — see
check_docs_match_code()'s own docstring for the exact phrasings this guards
against. (4) exists because the stdio server's one fatal failure mode — a
stray byte on stdout desynchronising JSON-RPC — is invisible until a client
connects.

(4) only runs on Windows and is reported as a skip elsewhere: the stdout guard
it exercises is built on `msvcrt` and `SetStdHandle`, so off Windows the server
exits at import and the check could only ever say "Connection closed". The
other three do not depend on the platform, so this script stays useful
wherever it is run.

Only pyproject.toml's five module-level dependencies are required (anthropic,
mcp, prompt_toolkit, pydantic, anyio); every per-tool backing package is
imported lazily inside the tool that needs it (they're still all installed by
requirements.txt in a real setup — this script just doesn't need them to pass),
so this runs on a bare CI box.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []
SKIPPED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def skip(name: str, reason: str) -> None:
    """A check that cannot run here, as distinct from one that passed.

    Printed rather than silently omitted, and counted in the summary, because
    the failure mode of a skip is that nobody notices the coverage is gone.
    """
    print(f"  skip  {name} — {reason}")
    SKIPPED.append(name)


def check_imports() -> None:
    print("imports")
    import importlib

    modules = [f"core.{p.stem}" for p in sorted((ROOT / "core").glob("*.py"))]
    modules = [m for m in modules if not m.endswith("__init__")]
    modules += ["main", "mcp_client", "mcp_server"]
    for name in modules:
        try:
            importlib.import_module(name)
            check(name, True)
        except Exception as e:
            check(name, False, f"{type(e).__name__}: {e}")


def check_tool_registry() -> None:
    print("tool registry")
    from core import local_tools

    tools = local_tools.TOOLS
    names = [t["name"] for t in tools]

    check("at least one tool declared", bool(tools))
    check(
        "no duplicate tool names",
        len(names) == len(set(names)),
        f"dupes: {sorted({n for n in names if names.count(n) > 1})}",
    )
    for tool in tools:
        name = tool.get("name", "<unnamed>")
        # The learned schemas (text editor, memory, computer) carry a
        # `type` instead of a description and input_schema — Claude already
        # knows their shape, so declaring one would contradict its training.
        if "type" in tool:
            check(f"{name}: learned schema has a name", bool(tool.get("name")))
            continue
        check(f"{name}: has a description", bool(tool.get("description")))
        schema = tool.get("input_schema") or {}
        check(
            f"{name}: input_schema is an object",
            schema.get("type") == "object" and "properties" in schema,
        )

    # Every module must expose the three-name contract local_tools relies on.
    for module in local_tools.MODULES:
        label = module.__name__
        check(
            f"{label}: exposes TOOLS/handles/execute",
            all(hasattr(module, a) for a in ("TOOLS", "handles", "execute")),
        )


def check_docs_match_code() -> None:
    """The count is stated in prose in several places and drifts silently.

    Three phrasings are checked, all confirmed in actual use across this
    project's own forks: "N local tools" (the canonical form), "N local +
    whatever the connected MCP servers advertise" (the Key Conventions
    tool-selection bullet), and "tool, not N" (the mcp_server.py Architecture
    bullet explaining it exposes one tool, not the whole local set). A bare
    "N tools" pattern was tried and rejected — it false-matched an unrelated
    "30-50 tools" threshold and a "2006 tool" aside in an unrelated package
    explanation, both real strings already in this file.
    """
    print("docs vs code")
    from core import local_tools

    actual = len(local_tools.TOOLS)
    pattern = re.compile(
        r"(\d+) local tools?"
        r"|(\d+) local \+"
        r"|tools?,? not (\d+)\b"
    )
    for doc in ("README.md", "CLAUDE.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        claimed = {int(g) for m in pattern.finditer(text) for g in m.groups() if g}
        if not claimed:
            check(f"{doc}: states a tool count", False, "no tool-count phrasing found")
            continue
        check(
            f"{doc}: claims {sorted(claimed)} == actual {actual}",
            claimed == {actual},
        )


def check_model_command() -> None:
    """`/model` / `/model swap` — config.toml wiring and index/name matching.

    No API call and no CliApp/prompt_toolkit involved: `load_claude_models`
    and `resolve_model_swap` (core/claude.py) are pure enough to check
    directly, the same way check_clear_and_diagnostics() below checks
    core/chat.py's diagnostics without a real conversation. core/cli.py's
    `/model` branch is a thin print/continue wrapper around these two calls,
    so covering the calls covers the actual matching logic that a bad
    index/name could otherwise silently mismatch.
    """
    print("/model command")
    from core.claude import load_claude_models, resolve_model_swap

    models = load_claude_models()
    check("claude_models is non-empty", len(models) > 0, str(models))
    check(
        "claude_models entries are all strings",
        all(isinstance(m, str) for m in models),
        str(models),
    )

    # Index matching (1-based, as shown in /model's own listing).
    check("index 1 resolves to the first entry", resolve_model_swap(models, "1") == models[0])
    last = str(len(models))
    check(
        f"index {last} resolves to the last entry",
        resolve_model_swap(models, last) == models[-1],
    )
    check("index 0 is out of range", resolve_model_swap(models, "0") is None)
    check(
        "an index past the end is out of range",
        resolve_model_swap(models, str(len(models) + 1)) is None,
    )

    # Name matching, case-insensitive, whitespace-tolerant.
    check(
        "exact name matches",
        resolve_model_swap(models, models[0]) == models[0],
    )
    check(
        "matching is case-insensitive",
        resolve_model_swap(models, models[0].upper()) == models[0],
    )
    check(
        "matching tolerates surrounding whitespace",
        resolve_model_swap(models, f"  {models[0]}  ") == models[0],
    )
    check(
        "an unrecognized name resolves to None",
        resolve_model_swap(models, "not-a-real-model") is None,
    )
    check("an empty arg resolves to None", resolve_model_swap(models, "") is None)


def check_model_refresh() -> None:
    """fetch_live_models()/refresh_claude_models() — the live-scan + TTL cache
    behind config.toml's claude_models array.

    Neither function is exercised by check_model_command() above (that one
    only covers the pre-existing load_claude_models()/resolve_model_swap()).
    Both accept fake collaborators for exactly this reason — fetch_live_models
    takes a `client`, refresh_claude_models takes `config_path`/`fetch_fn` —
    the same dependency-injection shape check_clear_and_diagnostics() below
    uses (a FakeBlock duck-typing a real content block). No network, no real
    config.toml touched, no tempfile left behind.

    refresh_claude_models's whole point is "never write on failure, only ever
    write on a successful scan" — that is asserted directly here (byte-for-
    byte file comparison before/after), not just exercised incidentally, so a
    future edit that weakens that guarantee fails loudly instead of only
    showing up as a mystery CI config.toml diff months later. tomlkit is
    imported lazily inside refresh_claude_models() only on a successful
    scan's write — if it isn't installed (true for CI's minimal dependency
    set), the success-path write is skipped in favour of a documented
    fallback (return the fresh result, persist nothing), and this check
    verifies whichever behaviour is actually correct for the environment
    it's running in, rather than assuming tomlkit is present.
    """
    print("model refresh (fetch_live_models / refresh_claude_models)")
    import importlib.util
    import tomllib
    from datetime import UTC, datetime, timedelta

    from core.claude import fetch_live_models, refresh_claude_models

    has_tomlkit = importlib.util.find_spec("tomlkit") is not None

    # --- fetch_live_models(): pure grouping/sorting logic, fake client -----

    class FakeModel:
        def __init__(self, model_id: str, created_at: datetime):
            self.id = model_id
            self.created_at = created_at

    class FakeModelsResource:
        def __init__(self, models: list):
            self._models = models

        def list(self):
            return list(self._models)

    class FakeClient:
        def __init__(self, models: list):
            self.models = FakeModelsResource(models)

    now = datetime.now(UTC)

    # Sonnet exists but is NOT the newest release overall — it must still
    # end up first, ahead of the genuinely newest family (opus here).
    mixed = FakeClient(
        [
            FakeModel("claude-opus-9", now),
            FakeModel("claude-opus-8", now - timedelta(days=30)),
            FakeModel("claude-sonnet-9", now - timedelta(days=5)),
            FakeModel("claude-sonnet-8", now - timedelta(days=40)),
            FakeModel("claude-haiku-9", now - timedelta(days=10)),
            FakeModel("not-a-claude-id-at-all", now),  # must be skipped, not crash
        ]
    )
    result = fetch_live_models(client=mixed)  # type: ignore[arg-type]
    check(
        "sonnet is forced first even when not newest",
        result[0] == "claude-sonnet-9",
        str(result),
    )
    check(
        "one entry per family, newest kept",
        set(result) == {"claude-sonnet-9", "claude-opus-9", "claude-haiku-9"},
        str(result),
    )
    check(
        "non-family-matching ids are silently skipped",
        "not-a-claude-id-at-all" not in result,
        str(result),
    )
    check(
        "remaining families stay in pure recency order",
        result[1:] == ["claude-opus-9", "claude-haiku-9"],
        str(result),
    )

    # No sonnet family at all -> pure recency order, no reordering applied.
    no_sonnet = FakeClient(
        [
            FakeModel("claude-opus-1", now - timedelta(days=1)),
            FakeModel("claude-haiku-1", now - timedelta(days=2)),
        ]
    )
    check(
        "with no sonnet family, order is pure recency",
        fetch_live_models(client=no_sonnet) == ["claude-opus-1", "claude-haiku-1"],  # type: ignore[arg-type]
    )

    # --- refresh_claude_models(): TTL cache / write-on-success-only --------

    def write_config(path: Path, *, models: list, checked_at, ttl_hours=24) -> None:
        lines = ["[claude]", f"claude_models = {models!r}".replace("'", '"')]
        if checked_at is not None:
            lines.append(f'claude_models_checked_at = "{checked_at}"')
        lines.append(f"model_scan_ttl_hours = {ttl_hours}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read_models(path: Path) -> list:
        with open(path, "rb") as f:
            return tomllib.load(f).get("claude", {}).get("claude_models")

    tmp_dir = Path("/tmp") if sys.platform != "win32" else Path(os.environ["TEMP"])
    fresh_iso = now.isoformat()
    stale_iso = (now - timedelta(hours=48)).isoformat()

    # 1) TTL fresh -> no scan attempted at all, cache returned untouched.
    cfg = tmp_dir / "smoke_model_refresh_fresh.toml"
    write_config(cfg, models=["cached-a", "cached-b"], checked_at=fresh_iso)
    before = cfg.read_text(encoding="utf-8")
    called = {"n": 0}

    def should_not_be_called() -> list:
        called["n"] += 1
        raise AssertionError("fetch_fn should not run when the TTL is fresh")

    try:
        out = refresh_claude_models(config_path=cfg, fetch_fn=should_not_be_called)
        check("fresh TTL returns the cached array", out == ["cached-a", "cached-b"], str(out))
        check("fresh TTL never calls fetch_fn", called["n"] == 0)
        check("fresh TTL leaves the file untouched", cfg.read_text(encoding="utf-8") == before)
    finally:
        cfg.unlink(missing_ok=True)

    # 2) TTL stale + scan succeeds -> array + timestamp updated (if tomlkit
    #    is installed) or the fresh result is still returned but not
    #    persisted (if it isn't) — either way is the documented contract.
    cfg = tmp_dir / "smoke_model_refresh_success.toml"
    write_config(cfg, models=["old-a"], checked_at=stale_iso)
    before = cfg.read_text(encoding="utf-8")
    try:
        out = refresh_claude_models(
            config_path=cfg, fetch_fn=lambda: ["fresh-x", "fresh-y"]
        )
        check(
            "stale + successful scan returns the fresh array",
            out == ["fresh-x", "fresh-y"],
            str(out),
        )
        if has_tomlkit:
            check(
                "successful scan persists the fresh array",
                read_models(cfg) == ["fresh-x", "fresh-y"],
                str(read_models(cfg)),
            )
            check(
                "successful scan updates claude_models_checked_at",
                "claude_models_checked_at" in cfg.read_text(encoding="utf-8"),
            )
        else:
            check(
                "without tomlkit, a successful scan still isn't persisted",
                cfg.read_text(encoding="utf-8") == before,
            )
    finally:
        cfg.unlink(missing_ok=True)

    # 3) TTL stale (well past due) + scan fails -> config untouched, old
    #    cache returned. This is the exact CI/placeholder-key scenario.
    cfg = tmp_dir / "smoke_model_refresh_failure.toml"
    write_config(cfg, models=["old-cached"], checked_at=stale_iso)
    before = cfg.read_text(encoding="utf-8")

    def failing_fetch() -> list:
        raise RuntimeError("simulated network/auth failure")

    try:
        out = refresh_claude_models(config_path=cfg, force=True, fetch_fn=failing_fetch)
        check("a failed scan returns the old cached array", out == ["old-cached"], str(out))
        check(
            "a failed scan leaves the file byte-for-byte untouched",
            cfg.read_text(encoding="utf-8") == before,
        )
    finally:
        cfg.unlink(missing_ok=True)

    # 4) force=True bypasses an otherwise-fresh TTL.
    cfg = tmp_dir / "smoke_model_refresh_forced.toml"
    write_config(cfg, models=["cached-only"], checked_at=fresh_iso)
    try:
        out = refresh_claude_models(config_path=cfg, force=True, fetch_fn=lambda: ["forced"])
        check("force=True overrides a fresh TTL", out == ["forced"], str(out))
    finally:
        cfg.unlink(missing_ok=True)

    # 5) A malformed timestamp is treated as stale, not fatal.
    cfg = tmp_dir / "smoke_model_refresh_malformed.toml"
    write_config(cfg, models=["cached-only"], checked_at="not-a-real-timestamp")
    try:
        out = refresh_claude_models(config_path=cfg, fetch_fn=lambda: ["rescanned"])
        check(
            "a malformed checked_at is treated as stale, not fatal",
            out == ["rescanned"],
            str(out),
        )
    finally:
        cfg.unlink(missing_ok=True)


def check_mcp_server() -> None:
    """Spawn the real server over stdio and complete a handshake.

    Uses a placeholder key: `_require_api_key` only checks that the variable is
    set, and listing tools never reaches the Anthropic API. A downstream MCP
    server that isn't present on this machine is reported and skipped by
    `_connect_mcp_servers`, so a CI box with no Unreal/n8n still passes.
    """
    print("mcp_server.py (stdio handshake)")

    # Windows-only, and skipped rather than failed elsewhere. mcp_server.py's
    # stdout guard imports `msvcrt` and calls `SetStdHandle` — the half of the
    # guard that stops a subprocess writing into the JSON-RPC channel, which
    # `os.dup2` alone does not cover on Windows. Neither exists on another
    # platform, so the server exits at import and this check can only report
    # "Connection closed", naming nothing. CI runs on windows-latest, which is
    # where this check actually earns its place.
    if sys.platform != "win32":
        skip(
            "handshake completes",
            f"needs Windows (running on {sys.platform}); mcp_server.py's "
            "stdout guard uses msvcrt/SetStdHandle",
        )
        return

    from mcp_client import MCPClient

    env = dict(os.environ)
    env.setdefault("ANTHROPIC_API_KEY", "placeholder-not-used-for-list-tools")

    async def go() -> tuple[list[str], list[str]]:
        async with MCPClient(
            command=sys.executable,
            args=[str(ROOT / "mcp_server.py")],
            env=env,
            transport="stdio",
        ) as client:
            names = [t.name for t in await client.list_tools()]
            # list_tools() alone never spawns a subprocess, so it can't
            # exercise the one thing the SetStdHandle half of the stdout
            # guard exists for — see the core/kernel.py and mcp_server.py
            # bullets in CLAUDE.md's Architecture section, which name the
            # IPython kernel as "the live case" for a subprocess inheriting
            # the real stdout handle on Windows. Run it for real here, no
            # Anthropic API involved either way.
            await client.call_tool("python", {"code": "1 + 1"})
            # The actual proof: a corrupted channel fails on the NEXT read,
            # not necessarily the call that caused the corruption.
            names_after = [t.name for t in await client.list_tools()]
            return names, names_after

    try:
        names, names_after = asyncio.run(asyncio.wait_for(go(), timeout=120))
    except Exception as e:
        check("handshake completes", False, f"{type(e).__name__}: {e}")
        return

    check("handshake completes", True)
    check("advertises `delegate`", "delegate" in names, f"got {names}")
    check("advertises `model`", "model" in names, f"got {names}")
    check(
        "channel survives running a subprocess-spawning tool",
        names_after == names,
        f"got {names_after}",
    )


def check_model_tool_over_mcp() -> None:
    """The `model` tool's actual list/swap/reject behavior, over a real
    stdio MCP round trip — not just that it's advertised (check_mcp_server()
    above only checks the name is in the list).

    Windows-only for the same reason check_mcp_server() above is: off
    Windows, mcp_server.py exits at import (its stdout guard needs
    msvcrt/SetStdHandle), so this reports a skip rather than a fail —
    matching this repo's own established convention for exactly this
    problem, not inventing a new one.

    Same placeholder-key posture as check_mcp_server(): `model` never calls
    the Anthropic API at all (see mcp_server.py's `_model_tool_result` — it
    only touches config.toml's claude_models array and the in-process
    `_claude.model` attribute), so this needs no real key and no network,
    same as every other check in this file. Exercises the exact same
    reject-don't-crash paths core/cli.py's local `/model` command has —
    covering the MCP-facing wrapper this worker adds specifically so a
    caller like ResearchMesh-Router can swap this worker's model remotely,
    per adding-model-command-to-swap-between-Anthropic-models.md in
    /memories (Phase W3 of that plan's Windows port).
    """
    print("model tool (over stdio MCP)")

    if sys.platform != "win32":
        skip(
            "model tool round trip completes",
            f"needs Windows (running on {sys.platform}); mcp_server.py's "
            "stdout guard uses msvcrt/SetStdHandle",
        )
        return

    from mcp_client import MCPClient

    env = dict(os.environ)
    env.setdefault("ANTHROPIC_API_KEY", "placeholder-not-used-for-model-tool")

    async def go() -> dict[str, tuple[str, bool]]:
        results: dict[str, tuple[str, bool]] = {}
        async with MCPClient(
            command=sys.executable,
            args=[str(ROOT / "mcp_server.py")],
            env=env,
            transport="stdio",
        ) as client:

            async def call(label: str, arguments: dict) -> None:
                r = await client.call_tool("model", arguments)
                text = r.content[0].text if r and r.content else ""  # type: ignore[union-attr]
                is_error = bool(r.is_error) if r else True
                results[label] = (text, is_error)

            await call("list", {"action": "list"})
            # index "2", deliberately NOT "1" — a fresh worker process starts
            # on claude_models[0] (index 1), so swapping to that same index
            # would be a no-op and the "current entry moved" check below
            # would false-fail for a reason that has nothing to do with the
            # tool actually working.
            await call("swap valid", {"action": "swap", "arg": "2"})
            await call("list after swap", {"action": "list"})
            await call("swap bogus", {"action": "swap", "arg": "not-a-real-model"})
            await call("swap no arg", {"action": "swap"})
            await call("bad action", {"action": "nonsense"})
        return results

    try:
        results = asyncio.run(asyncio.wait_for(go(), timeout=120))
    except Exception as e:
        check("model tool round trip completes", False, f"{type(e).__name__}: {e}")
        return

    check("model tool round trip completes", True)

    list_text, list_err = results["list"]
    check("list: not an error", not list_err, list_text)
    check("list: shows available models", "[model: available]" in list_text, list_text)
    check("list: marks a current entry", "(current)" in list_text, list_text)

    swap_text, swap_err = results["swap valid"]
    check("swap index 2: not an error", not swap_err, swap_text)
    check("swap index 2: confirms the swap", "swapped to" in swap_text, swap_text)

    relist_text, relist_err = results["list after swap"]
    check("list after swap: not an error", not relist_err, relist_text)
    check(
        "list after swap: current entry moved",
        relist_text != list_text,
        relist_text,
    )

    bogus_text, bogus_err = results["swap bogus"]
    check("swap bogus name: reports an error", bogus_err, bogus_text)
    check("swap bogus name: names the bad arg", "not-a-real-model" in bogus_text, bogus_text)

    noarg_text, noarg_err = results["swap no arg"]
    check("swap with no arg: reports an error", noarg_err, noarg_text)

    badaction_text, badaction_err = results["bad action"]
    check("bad action: reports an error", badaction_err, badaction_text)


def check_compiles() -> None:
    print("byte-compile")
    files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "core").glob("*.py"))
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", *(str(f) for f in files)],
        capture_output=True,
        text=True,
        check=False,
    )
    check("all files compile", result.returncode == 0, result.stderr.strip()[:300])


def check_clear_and_diagnostics() -> None:
    """`/clear`, and telling the two persistent 400s apart.

    An unanswered tool_use block and a conversation past the context window
    both leave every later turn failing identically, with no way back short of
    killing the app. The orphan detector is what separates them, so it is
    checked against a history that is deliberately poisoned — the condition
    `_resolve_pending_tool_uses` exists to prevent, constructed here on purpose
    because a healthy session never produces one.
    """
    print("/clear and diagnostics")
    from core.chat import Chat, _approx_size, _orphaned_tool_uses

    class FakeBlock:
        type = "tool_use"

        def __init__(self, block_id):
            self.id = block_id

    chat = Chat(claude_service=None, clients={})  # type: ignore[arg-type]

    healthy = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": [FakeBlock("t1")]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"}
            ],
        },
    ]
    check("a healthy history has no orphans", _orphaned_tool_uses(healthy) == [])

    poisoned = healthy + [
        {"role": "assistant", "content": [FakeBlock("t2")]},
        {"role": "user", "content": "and another thing"},
    ]
    check(
        "an unanswered tool_use is detected",
        _orphaned_tool_uses(poisoned) == ["t2"],
        str(_orphaned_tool_uses(poisoned)),
    )

    count, chars = _approx_size(poisoned)
    check("size report counts every message", count == len(poisoned), str(count))
    check("size report counts characters", chars > 0, str(chars))

    chat.messages = list(poisoned)  # type: ignore[arg-type]
    report = chat.clear()
    check("clear() empties the conversation", chat.messages == [])
    check("clear() reports the message count", "5 messages" in report, report)
    check(
        "clear() names the unanswered block as the cause",
        "unanswered tool_use" in report,
        report,
    )
    check("clear() is safe on an empty conversation", "0 messages" in chat.clear())

    # The diagnostic runs on an already-failing path; an exception here would
    # mask the real error.
    for label, err in (
        ("overflow", Exception("prompt is too long: 1200000 tokens > 1000000")),
        ("orphan", Exception("tool_use ids were found without tool_result")),
    ):
        chat.messages = list(poisoned)  # type: ignore[arg-type]
        try:
            chat._report_api_failure(err)
            ok = True
        except Exception as e:
            ok, label = False, f"{label}: {e}"
        check(f"failure report survives a {label} error", ok)


def check_run_loop_tool_use_lifecycle() -> None:
    """Drive the real, unmodified `Chat.run()` against a scripted fake API:
    the cutoff-duplicate bug, self-healing an already-poisoned history
    (reproduces the actual Linux-original production error, same
    core/chat.py lineage before this port), pause_turn replace-not-append,
    a mandatory mixed-call follow-up, grace-budget-exhausted surgical
    excision (never a turn/conversation wipe), and a normal multi-round
    regression guard.

    Ported from the Linux original's smoke_test.py (same-named function)
    alongside the core/chat.py fix itself (commit 672aae1 there) — see
    that repo's researchmesh_client_dev_log.md for the full incident
    history behind each scenario, not duplicated here since it's the same
    root cause, ported. This fork's `run()` uses the same
    `ToolManager.get_all_tools(self.clients)` call as the Linux original
    (unlike ResearchMesh-Router's fleet-indexed `ToolManager.build`), so
    this port needed no structural adaptation there — only the SYSTEM_PROMPT
    wording differs (PowerShell/Windows vs Linux/bash), which none of these
    scenarios touch. No platform-specific mocking needed either: none of
    this exercises mcp_server.py's msvcrt-dependent stdout guard, so unlike
    the mcp_server.py/model-tool checks above, this suite runs the same on
    every host, no skip needed.
    """
    print("run() loop: tool_use lifecycle (cutoff, self-heal, pause_turn, mixed calls)")
    import core.chat as chat_mod
    from core.chat import Chat, _duplicate_tool_result_ids, _orphaned_tool_uses

    class FakeBlock:
        def __init__(self, type, **kw):
            self.type = type
            for k, v in kw.items():
                setattr(self, k, v)

    class FakeResponse:
        def __init__(self, stop_reason, content):
            self.stop_reason = stop_reason
            self.content = content
            self.usage = type(
                "U", (), {"input_tokens": 1, "output_tokens": 1}
            )()

    class FakeClaudeService:
        def __init__(self, script):
            self._script = list(script)
            self.calls = 0

        def add_user_message(self, messages, message):
            messages.append(
                {
                    "role": "user",
                    "content": message.content
                    if hasattr(message, "content")
                    else message,
                }
            )

        def add_assistant_message(self, messages, message):
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content
                    if hasattr(message, "content")
                    else message,
                }
            )

        def text_from_message(self, message):
            return "\n".join(
                b.text for b in message.content if b.type == "text"
            )

        def chat(
            self, messages, system=None, stop_sequences=None, tools=None,
            thinking=False,
        ):
            self.calls += 1
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    async def fake_execute(name, input):
        return "ok"

    async def fake_get_all_tools(clients):
        return []

    orig_execute = chat_mod.local_tools.execute
    orig_get_all_tools = chat_mod.ToolManager.get_all_tools
    orig_max_iter = chat_mod.MAX_TOOL_ITERATIONS
    orig_extra_limit = chat_mod.EXTRA_CONTINUATION_LIMIT
    chat_mod.local_tools.execute = fake_execute
    chat_mod.ToolManager.get_all_tools = staticmethod(fake_get_all_tools)  # type: ignore[method-assign]

    try:
        # --- 1: cutoff right after an ordinary tool_use round trip
        chat_mod.MAX_TOOL_ITERATIONS = 1
        fake1 = FakeClaudeService([
            FakeResponse(
                "tool_use", [FakeBlock("tool_use", id="t1", name="bash", input={})]
            )
        ])
        c1 = Chat(claude_service=fake1, clients={})  # type: ignore[arg-type]
        result1 = asyncio.run(c1.run("do a thing"))
        check("cutoff: exactly one chat() call", fake1.calls == 1, str(fake1.calls))
        check(
            "cutoff: no duplicate tool_result",
            not _duplicate_tool_result_ids(c1.messages),
        )
        check(
            "cutoff: no orphaned tool_use", not _orphaned_tool_uses(c1.messages)
        )
        check(
            "cutoff: reports the iteration limit",
            "exceeded tool-iteration limit" in result1,
            result1,
        )

        # --- 2: self-heal an already-poisoned history (exact prod repro,
        # same id the Linux original's own report used — carried over
        # verbatim since it's what makes this a repro rather than a
        # synthetic case)
        chat_mod.MAX_TOOL_ITERATIONS = 75
        dup_id = "toolu_01Lz4DQdjjho9bntBh7LtYWJ"
        fake2 = FakeClaudeService([
            Exception(
                "each tool_use must have a single result. Found multiple "
                f"`tool_result` blocks with id: {dup_id}"
            ),
            FakeResponse("end_turn", [FakeBlock("text", text="all better now")]),
        ])
        c2 = Chat(claude_service=fake2, clients={})  # type: ignore[arg-type]
        c2.messages = [
            {"role": "user", "content": "earlier turn one"},
            {"role": "assistant", "content": "answer one"},
            {
                "role": "assistant",
                "content": [FakeBlock("tool_use", id=dup_id, name="bash", input={})],  # type: ignore[list-item]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": dup_id, "content": "the REAL result"},
                    {
                        "type": "tool_result",
                        "tool_use_id": dup_id,
                        "content": "[stopped: exceeded tool-iteration limit]",
                        "is_error": True,
                    },
                ],
            },
        ]
        earlier_turns_before = [dict(m) for m in c2.messages[:2]]
        result2 = asyncio.run(c2.run("please continue"))
        check(
            "self-heal: one failed call + one successful retry",
            fake2.calls == 2, str(fake2.calls),
        )
        check(
            "self-heal: duplicate removed",
            not _duplicate_tool_result_ids(c2.messages),
        )
        check("self-heal: no orphan left behind", not _orphaned_tool_uses(c2.messages))
        check(
            "self-heal: earlier turns preserved exactly",
            c2.messages[:2] == earlier_turns_before,
        )
        check(
            "self-heal: retried request's real answer returned",
            "all better now" in result2, result2,
        )

        # --- 2b: self-heal a ZERO-result orphan (a distinct production
        # error shape -- a tool_use with NO result at all, sitting as the
        # very last message, vs. scenario 2's duplicate-result shape)
        chat_mod.MAX_TOOL_ITERATIONS = 75
        orphan_id = "toolu_01GegL1vVQgSDzMC2d6WfAsJ"
        fake2b = FakeClaudeService([
            Exception(
                "messages.188: `tool_use` ids were found without "
                f"`tool_result` blocks immediately after: {orphan_id}."
            ),
            FakeResponse("end_turn", [FakeBlock("text", text="picking up again")]),
        ])
        c2b = Chat(claude_service=fake2b, clients={})  # type: ignore[arg-type]
        c2b.messages = [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier answer"},
            {
                "role": "assistant",
                "content": [FakeBlock("tool_use", id=orphan_id, name="bash", input={})],  # type: ignore[list-item]
            },
        ]
        before_2b = [dict(m) for m in c2b.messages[:2]]
        result2b = asyncio.run(c2b.run("continue please"))
        check(
            "zero-orphan: one failed call + one successful retry",
            fake2b.calls == 2, str(fake2b.calls),
        )
        check("zero-orphan: no orphan left behind", not _orphaned_tool_uses(c2b.messages))
        check(
            "zero-orphan: earlier turns preserved exactly",
            c2b.messages[:2] == before_2b,
        )
        check(
            "zero-orphan: retried request's real answer returned",
            "picking up again" in result2b, result2b,
        )

        # --- 3: pause_turn continuations REPLACE, never append
        chat_mod.MAX_TOOL_ITERATIONS = 75
        server_block = FakeBlock("server_tool_use", id="s1", name="web_search", input={})
        result_block = FakeBlock("web_search_tool_result", tool_use_id="s1", content=[])
        fake3 = FakeClaudeService([
            FakeResponse("pause_turn", [server_block]),
            FakeResponse("pause_turn", [server_block, FakeBlock("text", text="still going")]),
            FakeResponse(
                "end_turn",
                [server_block, result_block, FakeBlock("text", text="search done")],
            ),
        ])
        c3 = Chat(claude_service=fake3, clients={})  # type: ignore[arg-type]
        result3 = asyncio.run(c3.run("search for something"))
        assistant_msgs3 = [m for m in c3.messages if m["role"] == "assistant"]
        check(
            "pause_turn: exactly one assistant message for the whole turn",
            len(assistant_msgs3) == 1, str(len(assistant_msgs3)),
        )
        roles3 = [m["role"] for m in c3.messages]
        no_adjacent_assistant = all(
            not (roles3[i] == "assistant" == roles3[i + 1])
            for i in range(len(roles3) - 1)
        )
        check(
            "pause_turn: no two consecutive assistant messages",
            no_adjacent_assistant, str(roles3),
        )
        check("pause_turn: real final answer returned", "search done" in result3, result3)

        # --- 4: mixed client+server tool_use forces the mandatory follow-up
        chat_mod.MAX_TOOL_ITERATIONS = 1
        server_block4 = FakeBlock("server_tool_use", id="s1", name="web_search", input={})
        client_block4 = FakeBlock("tool_use", id="c1", name="bash", input={})
        fake4 = FakeClaudeService([
            FakeResponse("tool_use", [server_block4, client_block4]),
            FakeResponse(
                "end_turn",
                [
                    server_block4,
                    FakeBlock("web_search_tool_result", tool_use_id="s1", content=[]),
                    FakeBlock("text", text="all resolved"),
                ],
            ),
        ])
        c4 = Chat(claude_service=fake4, clients={})  # type: ignore[arg-type]
        result4 = asyncio.run(c4.run("mixed call"))
        check(
            "mixed call: mandatory follow-up made despite budget=1",
            fake4.calls == 2, str(fake4.calls),
        )
        check("mixed call: no orphan left behind", not _orphaned_tool_uses(c4.messages))
        check("mixed call: real final answer returned", "all resolved" in result4, result4)

        # --- 5: grace budget also exhausted -> surgical excise only
        chat_mod.MAX_TOOL_ITERATIONS = 1
        chat_mod.EXTRA_CONTINUATION_LIMIT = 1
        server_block5 = FakeBlock("server_tool_use", id="s1", name="web_search", input={})
        fake5 = FakeClaudeService([
            FakeResponse("pause_turn", [FakeBlock("text", text="searching"), server_block5]),
            FakeResponse("pause_turn", [FakeBlock("text", text="still searching"), server_block5]),
        ])
        c5 = Chat(claude_service=fake5, clients={})  # type: ignore[arg-type]
        c5.messages = [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        before5 = [dict(m) for m in c5.messages]
        result5 = asyncio.run(c5.run("search for something"))
        check("excise: earlier turn message 0 untouched", c5.messages[0] == before5[0])
        check("excise: earlier turn message 1 untouched", c5.messages[1] == before5[1])
        check(
            "excise: current turn's own query preserved",
            c5.messages[2] == {"role": "user", "content": "search for something"},
        )
        this_turn_assistant5 = [m for m in c5.messages[2:] if m["role"] == "assistant"]
        check(
            "excise: exactly one assistant message for this turn",
            len(this_turn_assistant5) == 1, str(len(this_turn_assistant5)),
        )
        if this_turn_assistant5:
            kinds5 = [getattr(b, "type", None) for b in this_turn_assistant5[0]["content"]]
            check(
                "excise: dangling server_tool_use removed",
                "server_tool_use" not in kinds5, str(kinds5),
            )
            check(
                "excise: unrelated text block in the same message survives",
                "still searching" in [getattr(b, "text", None) for b in this_turn_assistant5[0]["content"]],
                str(kinds5),
            )
        check(
            "excise: never mentions /clear or a whole-turn wipe",
            "/clear" not in result5 and "undone" not in result5, result5,
        )

        # --- 6: normal multi-round conversation, regression guard
        chat_mod.MAX_TOOL_ITERATIONS = 75
        fake6 = FakeClaudeService([
            FakeResponse("tool_use", [FakeBlock("tool_use", id="a", name="bash", input={})]),
            FakeResponse("tool_use", [FakeBlock("tool_use", id="b", name="bash", input={})]),
            FakeResponse("end_turn", [FakeBlock("text", text="done for real")]),
        ])
        c6 = Chat(claude_service=fake6, clients={})  # type: ignore[arg-type]
        result6 = asyncio.run(c6.run("multi round task"))
        check(
            "normal multi-round: all three calls made", fake6.calls == 3, str(fake6.calls)
        )
        check(
            "normal multi-round: no dupes/orphans",
            not _duplicate_tool_result_ids(c6.messages)
            and not _orphaned_tool_uses(c6.messages),
        )
        check(
            "normal multi-round: real final answer returned",
            result6 == "done for real", result6,
        )
    finally:
        chat_mod.local_tools.execute = orig_execute
        chat_mod.ToolManager.get_all_tools = orig_get_all_tools  # type: ignore[method-assign]
        chat_mod.MAX_TOOL_ITERATIONS = orig_max_iter
        chat_mod.EXTRA_CONTINUATION_LIMIT = orig_extra_limit


def main() -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    for step in (
        check_compiles,
        check_imports,
        check_tool_registry,
        check_docs_match_code,
        check_model_command,
        check_model_refresh,
        check_mcp_server,
        check_model_tool_over_mcp,
        check_clear_and_diagnostics,
        check_run_loop_tool_use_lifecycle,
    ):
        step()
        print()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    if SKIPPED:
        # Still exit 0 — a skip is "not checkable here", not a failure. Said
        # out loud so a green run off Windows is not mistaken for full cover.
        print(
            f"all checks passed ({len(SKIPPED)} skipped: {', '.join(SKIPPED)})"
        )
        return 0
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
