"""Fast sanity checks — no API key, no network, no optional packages needed.

    python smoke_test.py

This is not a test suite and does not pretend to be one. There is no pytest, no
fixtures, and nothing here exercises a tool's actual behaviour (that needs
LibreOffice, a browser, an X11 display and real API credits). What it does check
is the wiring that breaks silently and that nothing else catches:

  1. every module imports at all
  2. the tool registry is well-formed and free of duplicate names
  3. the tool count the docs claim still matches reality
  4. mcp_server.py completes an MCP handshake and advertises `delegate`

(3) exists because this project states its tool count in five places across two
files, and (4) because the stdio server's one fatal failure mode — a stray byte
on stdout desynchronising JSON-RPC — is invisible until a client connects.

Only module-level dependencies are required (anthropic, mcp, prompt_toolkit,
pydantic, anyio); every optional backing is imported lazily inside the tool that
needs it, so this runs on a bare CI box.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


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
        # The learned schemas (bash, text editor, memory, computer) carry a
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
    """The count is stated in prose in several places and drifts silently."""
    print("docs vs code")
    from core import local_tools

    actual = len(local_tools.TOOLS)
    pattern = re.compile(r"(\d+) local tools")
    for doc in ("README.md", "CLAUDE.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        claimed = {int(m) for m in pattern.findall(text)}
        if not claimed:
            check(f"{doc}: states a tool count", False, "no '<n> local tools' found")
            continue
        check(
            f"{doc}: claims {sorted(claimed)} == actual {actual}",
            claimed == {actual},
        )


def check_mcp_server() -> None:
    """Spawn the real server over stdio and complete a handshake.

    Uses a placeholder key: `_require_api_key` only checks that the variable is
    set, and listing tools never reaches the Anthropic API. A downstream MCP
    server that isn't present on this machine is reported and skipped by
    `_connect_mcp_servers`, so a CI box with no Unreal/n8n still passes.
    """
    print("mcp_server.py (stdio handshake)")
    from mcp_client import MCPClient

    env = dict(os.environ)
    env.setdefault("ANTHROPIC_API_KEY", "placeholder-not-used-for-list-tools")

    async def go() -> list[str]:
        async with MCPClient(
            command=sys.executable,
            args=[str(ROOT / "mcp_server.py")],
            env=env,
            transport="stdio",
        ) as client:
            return [t.name for t in await client.list_tools()]

    try:
        names = asyncio.run(asyncio.wait_for(go(), timeout=120))
    except Exception as e:
        check("handshake completes", False, f"{type(e).__name__}: {e}")
        return

    check("handshake completes", True)
    check("advertises `delegate`", "delegate" in names, f"got {names}")


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


def main() -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    for step in (
        check_compiles,
        check_imports,
        check_tool_registry,
        check_docs_match_code,
        check_mcp_server,
        check_clear_and_diagnostics,
    ):
        step()
        print()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
