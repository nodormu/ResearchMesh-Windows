import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from typing import Any, Literal, Optional

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)

Transport = Literal["stdio", "sse", "http"]


class MCPClient:
    """MCP client supporting stdio, SSE, and Streamable HTTP transports.

    - stdio: spawns a local server process (`command` + `args`).
    - sse:   connects to a remote server's SSE endpoint (`url`).
    - http:  connects to a remote server's Streamable HTTP endpoint (`url`).

    For the remote transports (sse / http) pass optional `headers` for auth,
    e.g. {"Authorization": "Bearer <token>"} for a server using Bearer auth.
    """

    def __init__(
        self,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict] = None,
        *,
        url: Optional[str] = None,
        transport: Transport = "stdio",
        headers: Optional[dict[str, str]] = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env
        self._url = url
        self._transport = transport
        self._headers = headers
        self._session: Optional[ClientSession] = None
        self._exit_stack: AsyncExitStack = AsyncExitStack()

    async def connect(self):
        if self._transport == "stdio":
            read, write = await self._connect_stdio()
        elif self._transport == "sse":
            read, write = await self._connect_sse()
        elif self._transport == "http":
            read, write = await self._connect_http()
        else:
            raise ValueError(f"Unknown transport: {self._transport!r}")

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()

    async def _connect_stdio(self):
        if not self._command:
            raise ValueError("stdio transport requires a `command`")
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        return read, write

    async def _connect_sse(self):
        if not self._url:
            raise ValueError("sse transport requires a `url`")
        read, write = await self._exit_stack.enter_async_context(
            sse_client(self._url, headers=self._headers)
        )
        return read, write

    async def _connect_http(self):
        if not self._url:
            raise ValueError("http transport requires a `url`")
        # Streamable HTTP is what most remote MCP servers expose today (n8n's
        # MCP Server Trigger, and anything built on the high-level server). It
        # was `streamablehttp_client` in mcp 1.x, and it also dropped this
        # transport's `headers=` argument in 2.0: HTTP settings now come from an
        # httpx2 client you build yourself. `create_mcp_http_client`
        # is the SDK's own factory, so the recommended MCP timeouts still apply —
        # a bare `httpx2.AsyncClient(headers=...)` would silently drop them.
        # Passing a client also transfers its lifecycle to us (the transport only
        # closes one it created itself), hence entering it on the exit stack.
        http_client = None
        if self._headers:
            http_client = await self._exit_stack.enter_async_context(
                create_mcp_http_client(headers=self._headers)
            )
        read, write = await self._exit_stack.enter_async_context(
            streamable_http_client(self._url, http_client=http_client)
        )
        return read, write

    def session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(
                "Client session not initialized. Call connect() first."
            )
        return self._session

    async def list_tools(self) -> list[types.Tool]:
        result = await self.session().list_tools()
        return result.tools

    async def call_tool(
        self, tool_name: str, tool_input
    ) -> types.CallToolResult | None:
        return await self.session().call_tool(tool_name, tool_input)

    async def list_prompts(self) -> list[types.Prompt]:
        result = await self.session().list_prompts()
        return result.prompts

    async def list_resources(self) -> list[types.Resource]:
        result = await self.session().list_resources()
        return result.resources

    async def get_prompt(self, prompt_name, args: dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

    async def read_resource(self, uri: str) -> Any:
        # 2.0 takes a plain `str` here; 1.x wanted a pydantic `AnyUrl`.
        result = await self.session().read_resource(uri)
        resource = result.contents[0]  # only the first content is used

        if isinstance(resource, types.TextResourceContents):
            if resource.mime_type == "application/json":
                return json.loads(resource.text)

            return resource.text  # fallback: return as plain text

    async def cleanup(self):
        await self._exit_stack.aclose()
        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


# --- command line ----------------------------------------------------------
# This is the inspector for this project. It exists because the obvious
# alternative, `npx @modelcontextprotocol/inspector`, needs a separately
# installed Node runtime to do things `MCPClient` above already implements —
# and worse, it makes you re-enter each server's address and token by hand in a
# browser, so what it tests is not what the app is configured to do.
#
# This builds every client through `main.build_client`, from the same
# config.toml the app reads. That is the whole point: an inspector that
# connects differently from the app can disagree with it, and this one already
# did once — before it reused `build_client` it forced transport="http" on
# every entry, so a stdio entry failed on a missing URL and reported `unreal`
# unreachable while `python main.py` was talking to it perfectly well.
_EXAMPLES = """\
examples:
  python mcp_client.py                          list tools on every configured server
  python mcp_client.py -s unreal --schema       one server, with each tool's input schema
  python mcp_client.py --prompts --resources    also list prompts and resources
  python mcp_client.py -s n8n --call list_flows --args '{\"limit\": 5}'
  python mcp_client.py --url http://host:8000/mcp --token-env N8N_MCP_TOKEN
"""


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the MCP servers this app is configured to use: connect, "
            "list what they expose, and call a tool."
        ),
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--server", metavar="NAME",
        help="only this [mcp].servers entry, by its `name`.",
    )
    parser.add_argument(
        "--url", metavar="URL",
        help="ignore config.toml and inspect this Streamable HTTP endpoint.",
    )
    parser.add_argument(
        "--token-env", metavar="VAR",
        help="environment variable holding the bearer token for --url. The "
             "token itself is never a command-line argument, so it stays out "
             "of your shell history.",
    )
    parser.add_argument(
        "--schema", action="store_true",
        help="print each tool's full input schema, not just its description.",
    )
    parser.add_argument(
        "--prompts", action="store_true", help="also list prompts."
    )
    parser.add_argument(
        "--resources", action="store_true", help="also list resources."
    )
    parser.add_argument(
        "--call", metavar="TOOL", help="call this tool and print its result."
    )
    parser.add_argument(
        "--args", metavar="JSON", default="{}",
        help="JSON object of arguments for --call (default: {}).",
    )
    return parser.parse_args(argv)


def _render(result) -> str:
    """A CallToolResult as readable text.

    Content blocks are rendered rather than repr'd because the point of calling
    a tool from here is to read what it said. `is_error` is snake_case: mcp 2.0
    renamed these fields and kept the camelCase spellings as serialization
    aliases only, so `isError` would silently read as missing.
    """
    lines = []
    if getattr(result, "is_error", False):
        lines.append("[tool reported an error]")
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            lines.append(block.text)
        elif kind == "image":
            data = getattr(block, "data", "") or ""
            lines.append(
                f"[image: {getattr(block, 'mime_type', '?')}, "
                f"{len(data)} base64 chars — not shown]"
            )
        else:
            lines.append(f"[{kind or type(block).__name__}] {block!r}")
    return "\n".join(lines) or "(no content)"


async def _inspect(client: MCPClient, name: str, target: str, args) -> None:
    async with client:
        if args.call:
            try:
                call_args = json.loads(args.args)
            except json.JSONDecodeError as e:
                print(f"\n{name}: --args is not valid JSON ({e})")
                return
            print(f"\n{name}: {target}\n  calling {args.call}({call_args})")
            result = await client.call_tool(args.call, call_args)
            print(_render(result))
            return

        tools = await client.list_tools()
        print(f"\n{name}: {target} — {len(tools)} tool(s)")
        for tool in tools:
            print(f"  - {tool.name}: {tool.description}")
            if args.schema:
                schema = json.dumps(tool.input_schema, indent=2)
                print("      " + schema.replace("\n", "\n      "))

        # Optional because plenty of servers implement neither, and a server
        # that does not is entitled to answer the request with an error rather
        # than an empty list — which should not read as the server being broken.
        for label, fetch in (
            ("prompt", client.list_prompts if args.prompts else None),
            ("resource", client.list_resources if args.resources else None),
        ):
            if fetch is None:
                continue
            try:
                items = await fetch()
            except Exception as e:
                print(f"  ({label}s unavailable: {type(e).__name__})")
                continue
            print(f"  {len(items)} {label}(s)")
            for item in items:
                detail = getattr(item, "description", None) or ""
                print(f"  - {getattr(item, 'name', item)}: {detail}")


async def main(argv=None):
    args = _parse_args(argv)

    # Imported inside the function, not at module scope, because main.py imports
    # *this* module — at module scope that is a circular import. Reusing its
    # `build_client` / `_expand_paths` is the entire point: a standalone check is
    # only worth running if it builds each client exactly the way the app does.
    import main as app

    if args.url:
        token = os.getenv(args.token_env) if args.token_env else None
        if args.token_env and not token:
            print(f"[mcp] {args.token_env} is not set — connecting without auth")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        client = MCPClient(transport="http", url=args.url, headers=headers)
        try:
            await _inspect(client, "--url", args.url, args)
        except BaseException as e:
            print(f"\n--url: {args.url} — FAILED ({type(e).__name__}: {e})")
        return

    servers = app.MCP_SERVERS
    if args.server:
        servers = [s for s in servers if s.get("name") == args.server]
        if not servers:
            names = [s.get("name") for s in app.MCP_SERVERS]
            print(f"No [mcp].servers entry named {args.server!r}. Configured: {names}")
            return

    if not servers:
        print("No servers configured under [mcp] in config.toml.")
        return

    if not app.MCP_ENABLED:
        # Checking them anyway: this command exists to tell you whether a server
        # *would* work, and `enabled = false` is usually why the app isn't
        # using one you expected it to.
        print("[mcp] enabled = false in config.toml — the app skips all of these.")

    if args.call and len(servers) > 1:
        print("--call needs one server; narrow it with --server NAME.")
        return

    for index, server in enumerate(servers):
        name = server.get("name") or f"server_{index}"

        if not server.get("enabled", True):
            print(f"\n{name}: disabled in config.toml — skipped")
            continue

        server = app._expand_paths(server)
        target = server.get("url") or " ".join(server.get("command") or [])
        try:
            await _inspect(app.build_client(server, name), name, target, args)
        except BaseException as e:
            print(f"\n{name}: {target} — FAILED ({type(e).__name__}: {e})")


if __name__ == "__main__":
    # See the note in main.py: no policy is set. Proactor is already the
    # Windows default, and the policy API is deprecated as of 3.14.
    asyncio.run(main())
