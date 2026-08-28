"""ResearchMesh as an MCP server — the whole agent behind one `delegate` tool.

Point Claude Code (or any MCP client) at this file and it gains a single tool
that hands a task to ResearchMesh, which then runs its own full agentic loop:
all 18 local tools plus whatever `[mcp].servers` in config.toml connects to.
Claude Code gets the finished result, not the intermediate tool traffic.

**Why one tool instead of re-exporting all 18.** `bash_20250124`,
`memory_20250818` and `computer_20251124` are *learned* schemas — Claude is
trained on their exact shape, and `computer` additionally needs the
`computer-use-2025-11-24` beta header on the request that declares it. Neither
survives a round trip through MCP's generic tool schema: the header belongs to
ResearchMesh's own API call, not to the client's. Re-exporting them would hand
Claude Code a lookalike of a tool it already knows, with the trained schema
discarded. Wrapping the loop keeps every one of them running against the API
exactly as designed, and keeps `SYSTEM_PROMPT` (which explains the tools to the
model actually calling them) in force.

So the delegate is a self-contained agent, not an extension of the caller's
toolset — the `bash`/editor overlap with Claude Code's own built-ins is the
point, not redundancy, and the ~30-50 tool ceiling that governs `local_tools`
doesn't apply here because the client only ever sees one tool.

Two transports:

    python mcp_server.py                          # stdio (default) — the client
                                                  # spawns this as a subprocess
    python mcp_server.py --transport streamable-http --port 8765

stdio is right for Claude Code and anything else that launches its server itself.
Streamable HTTP is for clients that connect to an already-running endpoint, or to
share one agent between several clients. `--host` defaults to 127.0.0.1, so
nothing is reachable off this machine unless you ask for it.
"""

import argparse
import os
import sys
from io import TextIOWrapper

# Mirrors how this app authenticates *to* its own MCP servers (config.toml's
# `token_env`): the config names an environment variable, the variable holds the
# token, and the token is never written into a file that gets committed. This is
# the same contract pointed the other way, so a client adding ResearchMesh uses
# the arrangement it already uses for every other server.
DEFAULT_TOKEN_ENV = "RESEARCHMESH_MCP_TOKEN"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve ResearchMesh to an MCP client as one `delegate` tool."
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio (default): the client spawns this process and talks over "
        "its stdin/stdout. streamable-http: listen on a port instead.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="streamable-http only. Default 127.0.0.1 (this machine only); "
        "0.0.0.0 exposes the agent to your whole network.",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="streamable-http only."
    )
    parser.add_argument(
        "--path", default="/mcp", help="streamable-http only. Endpoint path."
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="streamable-http only. Reply with a single JSON response instead "
        "of an SSE stream.",
    )
    parser.add_argument(
        "--ssl-certfile",
        metavar="PATH",
        help="streamable-http only. PEM certificate to serve TLS with, turning "
        "the endpoint into https://. Give the full chain (leaf first, then any "
        "intermediates) if the issuer is a company CA or a public one; clients "
        "verify against their own OS trust store, so nothing is configured on "
        "this side for that. Requires --ssl-keyfile.",
    )
    parser.add_argument(
        "--ssl-keyfile",
        metavar="PATH",
        help="streamable-http only. Private key for --ssl-certfile. Keep it "
        "readable only by the user this runs as; it is a path, never the key "
        "itself, so it stays out of `ps` like the bearer token does.",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        metavar="VAR",
        help="streamable-http only. Name of the environment variable holding "
        f"the bearer token clients must present (default {DEFAULT_TOKEN_ENV}). "
        "If that variable is unset the endpoint is unauthenticated. The token "
        "itself is never a command-line argument, so it stays out of `ps` and "
        "your shell history.",
    )
    return parser.parse_args()


# Parsed at import so the stdout guard below can depend on the transport, but
# only when actually run as a script — importing this module (for a test, say)
# must not consume sys.argv or steal fd 1.
ARGS = _parse_args() if __name__ == "__main__" else None

# --- stdout guard (stdio only) ----------------------------------------------
# MUST come before importing anything from core/. On stdio, fd 1 *is* the
# JSON-RPC channel: one stray byte and the client's parser desyncs and the
# session dies. This app prints to stdout in ~22 places (the chat loop echoes
# the model's text, tools report cleanup failures, `_report_usage` prints
# counters), and more will be added over time, so patching call sites is a
# losing game. Instead take fd 1 away from the process entirely: dup it for our
# own use, then point fd 1 at stderr. Doing it at the file-descriptor level
# rather than by reassigning `sys.stdout` also covers subprocesses that inherit
# fd 1 without capturing it — which is what stops a downstream stdio MCP
# server's own startup banner from corrupting the stream.
#
# Under streamable-http none of this applies: fd 1 isn't the wire, so the app's
# prints are just ordinary server logs and are left alone.
_JSONRPC_FD: int | None = None
if ARGS is not None and ARGS.transport == "stdio":
    _JSONRPC_FD = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
elif ARGS is not None:
    # Under HTTP the app's prints are the only operator-facing log, and this is
    # normally run as a background service with stdout redirected to a file.
    # Python block-buffers stdout when it isn't a terminal, so without this the
    # log stays empty until ~8KB accumulates, and a `kill` loses the lot —
    # including the "listening on ..." line you started it to see.
    # `reconfigure` belongs to TextIOWrapper, not the TextIO interface, and a
    # replaced sys.stdout need not have it — hence the narrowing check.
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

# The app must run from the repo root: config.toml is resolved relative to
# main.py, but the `memory` tool's CLAUDE_MEMORY_DIR default ("memories") is
# relative to the *working directory*, and an MCP client spawns us with its own
# project as cwd. Without this, every project Claude Code is used in would grow
# its own stray memories/ directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import asyncio
import hmac
from contextlib import AsyncExitStack

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server

import main as app
from core import local_tools
from core.chat import Chat
from core.claude import Claude

SERVER_NAME = "researchmesh"

_DELEGATE_DESCRIPTION = """\
Hand a task to ResearchMesh, a full agent running on this machine, and get back \
its final answer. It runs its own multi-step loop — it will use as many tools as \
the task needs before replying, so give it an outcome to achieve rather than a \
single command to run.

Reach for this when the task needs something you cannot do yourself:

- Controlling the desktop GUI: clicking, typing into, and reading windows of \
already-running applications via screenshots (X11 only).
- Answering interactive prompts — sudo/ssh passwords, `[y/N]` confirmations, \
installers, REPLs — that a plain non-interactive shell just hangs on.
- Stateful Python: a persistent IPython kernel where variables, imports and \
loaded dataframes survive across steps of the same session.
- Browsing the real DOM: rendering JavaScript, following links, filling and \
submitting forms in a headless Chromium.
- Converting documents between markdown/docx/odt/pdf/xlsx/pptx via LibreOffice \
and pandoc.
- SQL directly over CSV/Parquet/JSON files, with no import step.
- Any tool exposed by the MCP servers ResearchMesh itself connects to (for \
example a game engine editor), which are not visible to you.

Do NOT delegate ordinary file reads, edits, greps or shell commands you can \
already run — that is slower and gives you less control, not more.

Calls are serialised: there is one mouse, one browser page and one kernel, so \
two delegations cannot run at once. A GUI task can take minutes.\
"""

_SESSION_DESCRIPTION = """\
Conversation to continue. Re-using a session id keeps the full history, the \
Python kernel namespace and the browser page from the previous call, so you can \
say "now click the button below it" and it knows what "it" is. Use a fresh id \
to start clean. Defaults to "default".\
"""

TOOLS = [
    types.Tool(
        name="delegate",
        description=_DELEGATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "What you want done, in plain language. State the goal "
                        "and any constraints; the agent decides the steps. "
                        "Include absolute paths — its working directory is the "
                        "ResearchMesh repo, not yours."
                    ),
                },
                "session": {"type": "string", "description": _SESSION_DESCRIPTION},
                "thinking": {
                    "type": "boolean",
                    "description": (
                        "Give the agent extended reasoning for a hard task. "
                        "Slower; leave off for routine work."
                    ),
                },
            },
            "required": ["task"],
        },
    )
]

# One Chat per session id. Chat.messages is the entire conversation, so this is
# what makes a follow-up call able to refer back to the previous one.
_sessions: dict[str, Chat] = {}
_clients: dict = {}
_claude: Claude | None = None

# ResearchMesh has exactly one mouse, one browser page and one IPython kernel.
# Two delegations interleaving would have them fighting over all three, so the
# tool is serialised rather than made concurrent.
_lock = asyncio.Lock()


def _session(session_id: str) -> Chat:
    if _claude is None:
        # run() builds the service before either transport starts serving, so
        # a delegation can't arrive first. Stated rather than assumed.
        raise RuntimeError("Claude service was not initialised before serving")
    if session_id not in _sessions:
        _sessions[session_id] = Chat(clients=_clients, claude_service=_claude)
    return _sessions[session_id]


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


# Handlers are plain functions registered on the Server below, not decorated
# ones: mcp 2.0 removed `@server.list_tools()` / `@server.call_tool()` in favour
# of constructor `on_*` arguments (and `add_request_handler()` for methods
# outside the spec set). The handler signature gained a leading request context
# and now returns a whole Result model rather than a bare content list, so the
# `is_error` flag below is ours to set instead of the framework's to infer.
async def list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    if params.name != "delegate":
        return _text_result(f"Unknown tool: {params.name}", is_error=True)

    arguments = params.arguments or {}
    task = arguments.get("task", "")
    if not task.strip():
        return _text_result("Error: no task provided", is_error=True)

    session_id = arguments.get("session") or "default"
    thinking = bool(arguments.get("thinking"))

    async with _lock:
        chat = _session(session_id)
        try:
            answer = await chat.run(task, thinking=thinking)
        except Exception as e:
            # Chat.run already converts per-tool failures into tool_results, so
            # reaching here is the loop itself failing. Report it as tool
            # output rather than raising: an exception would surface to the
            # client as a transport-level error with no detail, and this server
            # stays up for the next call either way.
            print(f"[delegate] session {session_id!r} failed: {e}")
            return _text_result(f"Delegation failed: {e}", is_error=True)

    return _text_result(answer or "[the agent returned no text]")


server = Server(SERVER_NAME, on_list_tools=list_tools, on_call_tool=call_tool)


def _require_api_key() -> None:
    """Fail loudly at startup if the key is missing, rather than per delegation.

    An MCP client does *not* hand the server its own environment wholesale — the
    stdio transport passes a deliberately small safe subset (PATH, HOME, and
    friends), so `ANTHROPIC_API_KEY` being exported in the shell you launched the
    client from is not enough. It has to be named in the server's own `env`
    block in the client's config. Without this check, the key's absence surfaces
    only when a delegation is attempted, as an SDK error about resolving an
    authentication method, which does not point at the config that caused it.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return
    sys.exit(
        f"[{SERVER_NAME}] ANTHROPIC_API_KEY is not set in this server's "
        "environment.\n"
        "An MCP client passes stdio servers only a safe subset of environment "
        "variables, so exporting it in your shell does not reach here — add it "
        'to the "env" block of this server\'s entry in the client config '
        "(see README.md).",
    )


async def _serve_stdio() -> None:
    """JSON-RPC over this process's stdin/stdout."""
    if _JSONRPC_FD is None:
        # Set by the stdout guard, which runs for exactly this transport.
        raise RuntimeError("stdio transport started without the duplicated fd")
    # Hand the *real* stdout (dup'd before fd 1 was pointed at stderr) to the
    # transport, so JSON-RPC still reaches the client.
    jsonrpc_out = anyio.wrap_file(
        TextIOWrapper(os.fdopen(_JSONRPC_FD, "wb", buffering=0), encoding="utf-8")
    )
    async with stdio_server(stdout=jsonrpc_out) as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def _bearer_auth(asgi_app, token: str):
    """Require `Authorization: Bearer <token>` on every request.

    The mirror image of what `main.py` does when connecting *to* an MCP server
    with a `token_env`: same header, same shape, so a client already configured
    for that pattern needs no new concepts. Compared with `hmac.compare_digest`
    rather than `==` so that a wrong token can't be recovered a byte at a time
    from response timing.
    """
    expected = f"Bearer {token}"

    async def guarded(scope, receive, send) -> None:
        if scope["type"] != "http":
            await asgi_app(scope, receive, send)
            return
        offered = ""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                offered = value.decode("latin-1")
                break
        if not hmac.compare_digest(offered, expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="researchmesh"'),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"unauthorized"}',
                }
            )
            return
        await asgi_app(scope, receive, send)

    return guarded


async def _serve_http(args: argparse.Namespace) -> None:
    """JSON-RPC over Streamable HTTP, for clients that connect to a running
    endpoint rather than spawning one.

    The same `server` object drives both transports — the low-level MCP
    `Server` only ever deals in read/write streams, so a transport is a
    different way of supplying those, not a different server. (This is also why
    switching to the high-level server — `FastMCP` in mcp 1.x, renamed
    `MCPServer` in 2.0 — would be a downgrade here: its stdio path calls
    `stdio_server()` with no arguments, i.e. it insists on the real `sys.stdout`,
    which is exactly the file descriptor the stdout guard has to take away.)
    """
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(
        app=server, json_response=args.json_response
    )

    async def handle(scope, receive, send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # The manager runs one task group for every session; it is single-use,
        # so it is started here rather than per request.
        async with manager.run():
            yield

    token = os.getenv(args.token_env)
    endpoint = _bearer_auth(handle, token) if token else handle

    http_app = Starlette(
        routes=[Mount(args.path, app=endpoint)], lifespan=lifespan
    )

    # Both or neither: uvicorn ignores a lone --ssl-certfile and would serve
    # plain HTTP anyway, which is the one failure worth refusing to make — the
    # operator asked for TLS and every client would still connect happily.
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        raise SystemExit(
            f"[{SERVER_NAME}] --ssl-certfile and --ssl-keyfile must be given "
            "together (uvicorn silently serves plain HTTP with only one)"
        )
    tls = bool(args.ssl_certfile)
    for label, path in (("--ssl-certfile", args.ssl_certfile), ("--ssl-keyfile", args.ssl_keyfile)):
        if path and not os.path.isfile(path):
            raise SystemExit(f"[{SERVER_NAME}] {label}: no such file: {path}")

    local_only = args.host in ("127.0.0.1", "localhost", "::1")
    scheme = "https" if tls else "http"
    print(
        f"[{SERVER_NAME}] listening on {scheme}://{args.host}:{args.port}{args.path}"
        + ("" if local_only else "  (reachable from your network)")
    )
    if not tls and not local_only:
        # A worker on a company network is the case this matters for: the
        # bearer token and every delegated task and result cross the wire in
        # the clear. Said once here rather than refusing, matching the
        # unauthenticated-bind posture below.
        print(
            f"[{SERVER_NAME}] no TLS — traffic is plaintext. Pass "
            "--ssl-certfile/--ssl-keyfile to serve https instead."
        )
    if token:
        print(
            f"[{SERVER_NAME}] auth: bearer token required "
            f"(from ${args.token_env})"
        )
    else:
        # Same posture as main.py's "connecting without auth" notice: say it
        # plainly and carry on rather than refusing. Only the combination of
        # no token *and* a non-loopback bind is actually exposed, so that is
        # the one that gets shouted about.
        print(
            f"[{SERVER_NAME}] auth: none — ${args.token_env} is not set"
            + (
                ""
                if local_only
                else "\n"
                f"[{SERVER_NAME}] WARNING: unauthenticated and bound to "
                f"{args.host}. Anyone who can reach this port has unrestricted "
                "shell and desktop control of this machine."
            )
        )
    config = uvicorn.Config(
        http_app,
        host=args.host,
        port=args.port,
        log_level="warning",
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )
    await uvicorn.Server(config).serve()


async def run(args: argparse.Namespace) -> None:
    global _claude

    _require_api_key()
    _claude = Claude(model=app.claude_model)

    async with AsyncExitStack() as stack:
        # ResearchMesh keeps its own downstream MCP servers: this server is a
        # client of them at the same time as it is a server to Claude Code.
        if app.MCP_ENABLED and app.MCP_SERVERS:
            await app._connect_mcp_servers(stack, _clients)
        stack.push_async_callback(local_tools.shutdown)

        print(
            f"[{SERVER_NAME}] ready — {len(local_tools.TOOLS)} local tools, "
            f"{len(_clients)} downstream MCP server(s), "
            f"transport={args.transport}"
        )

        if args.transport == "stdio":
            await _serve_stdio()
        else:
            await _serve_http(args)


if __name__ == "__main__":
    # ARGS is parsed at import under exactly this condition, so it is never
    # None here; the fallback keeps that provable rather than assumed.
    try:
        asyncio.run(run(ARGS if ARGS is not None else _parse_args()))
    except (KeyboardInterrupt, EOFError):
        pass
