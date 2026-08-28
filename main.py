import asyncio
import os
import sys
import tomllib
from contextlib import AsyncExitStack

from anthropic import Anthropic

from core import local_tools
from core.chat import Chat
from core.claude import Claude
from core.cli import CliApp
from mcp_client import MCPClient

# Anthropic Config
api_key = os.getenv("ANTHROPIC_API_KEY") # api key is in .bashrc file, which is why this is here
client = Anthropic(api_key=api_key)
# Configuration file (config.toml) — non-secret settings.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.toml")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


_config = _load_config()

# Claude model: config.toml [claude] model, overridable by the CLAUDE_MODEL env var.
claude_model = os.getenv("CLAUDE_MODEL") or _config.get("claude", {}).get(
    "model", "claude-sonnet-5"
)

# MCP servers (Streamable HTTP), declared as a list in config.toml so adding one
# is a config edit rather than a code change. Bearer tokens stay in the
# environment: each entry's `token_env` names the variable holding its token.
_mcp_config = _config.get("mcp", {})
MCP_ENABLED = _mcp_config.get("enabled", True)  # default on
MCP_SERVERS = _mcp_config.get("servers", [])


def _expand(value):
    """Expand `~` and `$VAR`/`${VAR}` in a config value, recursing into lists
    and dicts.

    config.toml is plain TOML and `tomllib` does no substitution of its own, so
    without this a path like `/home/$USER/...` would be handed to the
    subprocess literally. An undefined variable is left as-is (that is
    `expandvars`' behaviour, not an accident) so a typo shows up verbatim in
    the "could not reach/launch" message instead of silently collapsing to a
    path that starts with `/home//`.
    """
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _expand_paths(server: dict) -> dict:
    """A copy of a [mcp].servers entry with `~`/`$VAR` expanded in the fields
    that hold paths or URLs, so config.toml can be checked in without anyone's
    home directory or mount point baked into it.

    Only `command`, `url` and `env` are touched. `env`'s keys are variable
    *names* and are left alone; only its values are expanded. `token_env` is
    likewise a name, and the token itself never appears in this file.
    """
    expanded = dict(server)
    for key in ("command", "url", "env"):
        if key in expanded:
            expanded[key] = _expand(expanded[key])
    return expanded


def build_client(server: dict, name: str) -> MCPClient:
    """One MCPClient from a [mcp].servers entry.

    Two kinds of entry are recognized:

    - Streamable HTTP (remote server):
        { name = "...", url = "http://host:port/...", token_env = "..." }

    - stdio (local subprocess the client launches itself):
        { name = "...", command = ["node", "/path/to/bin.js"], env = { ... } }
      `command` is the full argv — command[0] is the executable, the rest are
      its arguments. `env` is optional: extra environment variables to hand
      the subprocess (merged with a safe default set — PATH, HOME, etc. — by
      the MCP SDK itself, so you don't need to repeat those).

    Paths are expected to arrive already expanded (`_connect_mcp_servers` runs
    `_expand_paths` first); calling this directly with a raw config entry will
    pass `$USER` through to the subprocess unsubstituted.
    """
    if "command" in server:
        command_list = server.get("command")
        if not isinstance(command_list, list) or not command_list:
            raise ValueError(
                "'command' must be a non-empty list, e.g. "
                '["node", "/path/to/bin.js"]'
            )
        command, *args = command_list
        env = server.get("env")
        return MCPClient(command=command, args=args, env=env, transport="stdio")

    url = server.get("url")
    if not url:
        raise ValueError("entry needs either 'url' (http) or 'command' (stdio)")

    token_env = server.get("token_env")
    token = os.getenv(token_env) if token_env else None
    if token_env and not token:
        print(
            f"[mcp] {name}: {token_env} is not set — connecting without auth",
            file=sys.stderr,
        )
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return MCPClient(transport="http", url=url, headers=headers)


async def _connect_mcp_servers(stack: AsyncExitStack, clients: dict) -> None:
    """Connect every configured server. A server that fails is reported and
    skipped, so one unreachable endpoint doesn't take the whole app down."""
    for index, server in enumerate(MCP_SERVERS):
        name = server.get("name") or f"server_{index}"

        if not server.get("enabled", True):
            print(f"[mcp] {name}: disabled in config.toml")
            continue

        # Do this before build_client so the failure message below also shows
        # the real path rather than the `$USER` the file was written with.
        server = _expand_paths(server)

        try:
            client = build_client(server, name)
        except ValueError as e:
            print(f"[mcp] {name}: skipped — {e}", file=sys.stderr)
            continue

        try:
            await client.connect()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # A failed connect raises CancelledError from connect() and surfaces
            # the real cause (e.g. ConnectError) from cleanup(); swallow both and
            # report the endpoint instead of dumping a traceback.
            try:
                await client.cleanup()
            except BaseException as cleanup_error:
                # Same rule as core/local_tools.shutdown(): cleanup must not be
                # able to fail, but it must not fail *silently* either — this is
                # already an error path, so a swallowed second failure here is
                # the least visible place in the app.
                print(
                    f"[mcp] {name}: cleanup after failed connect also failed "
                    f"(ignored): {cleanup_error}",
                    file=sys.stderr,
                )
            target = server.get("url") or " ".join(server.get("command", []))
            print(
                f"[mcp] {name}: could not reach/launch {target} — skipped",
                file=sys.stderr,
            )
            continue

        stack.push_async_callback(client.cleanup)
        clients[name] = client
        print(f"[mcp] {name}: connected")


async def main():
    claude_service = Claude(model=claude_model)

    server_scripts = sys.argv[1:]
    clients = {}

    async with AsyncExitStack() as stack:
        if MCP_ENABLED and MCP_SERVERS:
            await _connect_mcp_servers(stack, clients)
            if not clients:
                print(
                    "[mcp] no server connected — running with local tools only",
                    file=sys.stderr,
                )
        elif not MCP_ENABLED:
            print("[mcp] disabled in config.toml — running with local tools only")
        else:
            print("[mcp] no servers configured — running with local tools only")

        for i, server_script in enumerate(server_scripts):
            client_id = f"client_{i}_{server_script}"
            client = await stack.enter_async_context(
                MCPClient(command="python", args=[server_script])
            )

            clients[client_id] = client

        # Close anything a local tool started (browser, IPython kernel, DuckDB).
        stack.push_async_callback(local_tools.shutdown)

        chat = Chat(
            clients=clients,
            claude_service=claude_service,
        )

        cli = CliApp(chat)
        await cli.run()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
