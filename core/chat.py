import os

from anthropic.types import MessageParam

from core import local_tools
from core.claude import Claude
from core.tools import ToolManager
from mcp_client import MCPClient

MAX_TOOL_ITERATIONS = 75

# Set CLAUDE_SHOW_USAGE=1 to print token and cache counters per request. Prompt
# caching fails *silently* (a too-short prefix or a changed byte early in the
# prefix just means no hit, with no error), so this is the only way to confirm
# the cache_control breakpoint in core/claude.py is actually paying off.
SHOW_USAGE = os.getenv("CLAUDE_SHOW_USAGE") == "1"

# Sent as the `system` parameter on every request. Without it, Claude has nothing
# but the tool schemas to reason from and will describe capabilities it doesn't
# have (e.g. inventing a sandboxed code-execution container, which this app has
# no such thing as). Everything here is either a fact about this environment that
# Claude cannot infer, or a choice between genuinely overlapping tools.
SYSTEM_PROMPT = """\
You are the assistant in a command-line research client running on the user's own Linux
machine. What follows describes your actual environment.

These 18 tools are the ones built into this client: bash, str_replace_based_edit_tool,
web_search, web_fetch, memory, computer, browser_navigate, browser_extract, browser_click,
browser_fill, browser_links, browser_back, document_convert, python, interactive_run,
config_edit, sql_query, trash. Any other tool in your list comes from a connected MCP
server and runs on that server — those are real; use them. But if you are about to name a
tool that is in neither group, you are mistaken.

Of the built-in 18, only `web_search` and `web_fetch` run on Anthropic's servers.
Everything else runs locally, in this user's own account — including the browser, which is
a headless Chromium process on this machine, so pages are fetched from the user's own
network.

There is no sandbox and no code-execution container, and there are no `code_execution`,
`bash_code_execution`, or `text_editor_code_execution` definitions in your tool list. The
2026 `web_search`/`web_fetch` variants do filter their results using server-side code
execution internally, which is likely why those names feel available — but that is
machinery inside those two tools, not something you can call. `bash` and `python` run as
the user, with their permissions, their filesystem, and their network. Nothing you run is
isolated or automatically reversible, so treat destructive actions as real.

State between calls:
- `python` is a persistent IPython kernel: variables, imports, and loaded data survive
  across calls. Load data once and keep working with it.
- `bash` is a fresh subprocess every call. `cd`, exported variables, and activated
  virtualenvs do not carry over; chain with `&&` in a single call instead.
- The browser holds one live page, and `sql_query` one DuckDB connection, for the session.
- `memory` is the only state that outlives this process. Everything above is gone when the
  session ends; files under `/memories` are still there next time.

Choosing between overlapping tools:
- Deleting: use `trash`, which is recoverable, rather than `rm`.
- YAML/TOML/JSON config files: use `config_edit`. It preserves comments and key order;
  the file editor and `sed` silently destroy them.
- Commands that prompt for input: use `interactive_run`. `bash` has no stdin and hangs.
- Reading the web: `browser_navigate` is the primary way, since it renders JavaScript and
  `browser_links`/`browser_back` let you follow links. Use `web_fetch` for a single known
  document you don't need to interact with.
- Querying a CSV, Parquet, or JSON file: `sql_query` reads it in place, no import step.
- Producing a document: write markdown with the file editor, then `document_convert` it.
  From markdown the targets are pdf, docx, odt, html, epub, rtf, and txt — xlsx and pptx
  are reachable only from another office format, not from markdown.
- The file editor is text-only (UTF-8) apart from .png/.jpg/.jpeg, which it returns as an
  image. It cannot view PDFs or other binary files; it will return a decoding error. Use
  `bash` to inspect those.
- Anything scriptable: prefer `bash`, `python`, or the browser over `computer`. `computer`
  drives the real desktop by moving the pointer and synthesising keystrokes, so it is slow,
  it returns a screenshot per action, and it competes with the user for their own mouse and
  keyboard. Reach for it only when there is no other way in — a GUI-only application, or
  something you must see rendered on their actual screen.
- `memory` writes to a private `/memories` store, not to the user's project files. Notes
  meant for you later go there; files the user asked for go on the real filesystem.

Report what actually happened. If a command failed, say so and include its output. If you
haven't verified something, say that rather than implying you have.
"""


def _block_field(block, name: str):
    """Read a field off a content block that may be an SDK object or a dict.

    Assistant turns hold the SDK's own block objects (straight off
    `response.content`); the tool_result turns we build ourselves are plain
    dicts. Anything walking the whole conversation has to cope with both.
    """
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _orphaned_tool_uses(messages) -> list[str]:
    """tool_use ids that never got a tool_result — the poisoned-session check.

    The API requires every tool_use block to be answered in the *immediately
    following* message. One that isn't doesn't just break the turn it happened
    in: the block stays in the history for the life of the process, so every
    later request fails the same way, however many turns later. That failure
    reads as "it started 400ing and won't stop", which is very hard to tell
    from a context overflow without looking.

    `_resolve_pending_tool_uses` exists to make this impossible. This is how you
    find out it didn't.
    """
    answered: set[str] = set()
    issued: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if kind == "tool_use":
                block_id = _block_field(block, "id")
                if block_id:
                    issued.append(block_id)
            elif kind == "tool_result":
                used = _block_field(block, "tool_use_id")
                if used:
                    answered.add(used)
    return [i for i in issued if i not in answered]


def _approx_size(messages) -> tuple[int, int]:
    """(message count, character count) for the conversation.

    Deliberately a character count rather than a real token count:
    `count_tokens` cannot measure this conversation at all, because
    `web_search`/`web_fetch` are server tools and that endpoint rejects them
    outright. Roughly 3-4 characters per token is close enough to tell "nowhere
    near the window" from "at it", which is the only question being asked here.
    """
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    chars += len(str(block.get("content") or block.get("text") or ""))
                else:
                    chars += len(str(getattr(block, "text", "") or ""))
    return len(messages), chars


def _report_usage(response) -> None:
    """One line of token accounting. From the second request onward, cache read
    should be large and cache write near zero — that means the prefix is being
    reused. Cache read staying at 0 means the breakpoint isn't landing."""
    usage = response.usage
    print(
        "[usage: input {} | cache write {} | cache read {} | output {}]".format(
            usage.input_tokens,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            usage.output_tokens,
        )
    )


def _local_result_to_content(local):
    """Local tool executors normally return a plain string. They can also return
    the image marker built by core.output.image_result ({"__kind__": "image",
    ...}) — the file editor's and memory's `view` on an image file, and every
    computer-use screenshot — which we translate into a real tool_result content
    list carrying an `image` block, so the model actually receives pixels
    instead of a UTF-8 decode error."""
    if isinstance(local, dict) and local.get("__kind__") == "image":
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": local["media_type"],
                    "data": local["data"],
                },
            },
            {"type": "text", "text": local["text"]},
        ]
    return local


class Chat:
    def __init__(self, claude_service: Claude, clients: dict[str, MCPClient]):
        self.claude_service: Claude = claude_service
        self.clients: dict[str, MCPClient] = clients
        self.messages: list[MessageParam] = []

    def clear(self) -> str:
        """`/clear` — drop the conversation, keep the process and its servers.

        The only recovery path from a poisoned history. `self.messages` lives
        for the life of the process, so both of the failures that persist —
        an unanswered tool_use block, and a conversation that has outgrown the
        context window — leave every subsequent turn failing identically.
        Before this existed the only way out was killing the app, which also
        drops the browser page, the IPython kernel and every MCP connection.

        Deliberately does not touch `self.clients`, the kernel or the browser:
        none of them is the reason the history is unusable, and re-establishing
        them would be the expensive half of a restart for none of the benefit.
        `/memories` is untouched too — it is meant to outlive the session.
        """
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)
        self.messages = []
        detail = f"cleared {count} messages (~{chars:,} chars)"
        if orphans:
            detail += (
                f" — including {len(orphans)} unanswered tool_use block"
                f"{'s' if len(orphans) != 1 else ''}, which is what was "
                f"breaking every turn"
            )
        return f"[{detail}]"

    def _report_api_failure(self, error: Exception) -> None:
        """Say which failure this is, rather than leaving it to guesswork.

        The two that persist look identical from the outside — the app starts
        400ing and does not stop — but they have different causes and different
        fixes, and the error text plus these two numbers separate them every
        time.
        """
        text = str(error)
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)

        print(f"[api error] {text}")
        print(f"[api error] conversation: {count} messages, ~{chars:,} chars")

        if orphans:
            print(
                f"[api error] {len(orphans)} unanswered tool_use block(s): "
                f"{', '.join(orphans[:3])}"
                f"{' …' if len(orphans) > 3 else ''}"
            )
            print(
                "[api error] this poisons every later request in the session. "
                "Run /clear."
            )
        elif "too long" in text.lower() or "context" in text.lower():
            print(
                "[api error] the conversation has outgrown the context window. "
                "Run /clear."
            )

    async def _run_tool_uses(self, message) -> list:
        """Route each tool_use block: local executor, or the MCP ToolManager.

        Every tool_use block here owes the API a matching tool_result in the
        very next message, no exceptions — so a local executor that raises
        must not abort the batch and orphan its block (or the blocks after
        it). Turn the raise into an error tool_result instead, the same way
        ToolManager.execute_blocks already does for MCP-side tools below.
        """
        blocks = [b for b in message.content if b.type == "tool_use"]
        results: list = []
        mcp_blocks: list = []

        for block in blocks:
            try:
                local = await local_tools.execute(block.name, block.input)
            except Exception as e:
                print(f"[local tool '{block.name}' raised: {e}]")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error executing tool '{block.name}': {e}",
                        "is_error": True,
                    }
                )
                continue

            if local is not None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _local_result_to_content(local),
                    }
                )
            else:
                mcp_blocks.append(block)

        if mcp_blocks:
            results.extend(
                await ToolManager.execute_blocks(self.clients, mcp_blocks)
            )
        return results

    def _resolve_pending_tool_uses(self, response, reason: str) -> None:
        """Guarantee every tool_use block in `response` has a tool_result.

        self.messages persists for the life of the process (one Chat per
        run), so any tool_use left unresolved here doesn't just affect this
        turn — it poisons *every* request for the rest of the session with a
        400 (`tool_use ids were found without tool_result blocks immediately
        after`), because that block is still sitting there with nothing after
        it. This is the fallback of last resort for the two ways that used to
        happen: the MAX_TOOL_ITERATIONS cutoff firing right after a
        stop_reason == "tool_use" response (the loop broke before ever
        calling _run_tool_uses for it), and an exception escaping tool
        routing entirely. Safe to call even when there's nothing to resolve.
        """
        if response is None or response.stop_reason != "tool_use":
            return
        blocks = [b for b in response.content if b.type == "tool_use"]
        if not blocks:
            return
        results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"[{reason}]",
                "is_error": True,
            }
            for block in blocks
        ]
        self.claude_service.add_user_message(self.messages, results)

    async def run(self, query: str, thinking: bool=False) -> str:
        final_text_response = ""
        self.claude_service.add_user_message(self.messages, query)

        # The MCP tool list is fetched once per user turn, not once per
        # tool-use iteration — it can't change mid-turn, and re-listing was a
        # round trip per client per loop pass (up to MAX_TOOL_ITERATIONS).
        mcp_tools = await ToolManager.get_all_tools(self.clients)
        tool_defs = local_tools.TOOLS + mcp_tools

        response = None
        iterations = 0
        while True:
            iterations += 1
            if iterations > MAX_TOOL_ITERATIONS:
                if response is None:
                    # Only reachable with MAX_TOOL_ITERATIONS < 1, i.e. the
                    # limit was hit before anything was ever sent: there is no
                    # turn to resolve and no text to report.
                    break
                # `response` is still the last one we received (this iteration
                # never calls chat() again). If it ended on stop_reason ==
                # "tool_use", its tool_use blocks are already sitting in
                # self.messages with nothing after them — resolve them before
                # breaking, or the *next* user turn's first chat() call fails
                # immediately with a 400, however many messages later.
                self._resolve_pending_tool_uses(
                    response, "stopped: exceeded tool-iteration limit"
                )
                final_text_response = (
                    self.claude_service.text_from_message(response)
                    or "[stopped: exceeded tool-iteration limit]"
                )
                break

            try:
                response = self.claude_service.chat(
                    messages=self.messages,
                    system=SYSTEM_PROMPT,
                    tools=tool_defs,
                    thinking=thinking
                )
            except Exception as e:
                # Diagnose before returning. Both persistent failures leave the
                # history in a state where every later turn fails the same way,
                # so the useful information is *why*, and it is gone as soon as
                # this returns a bare error string.
                self._report_api_failure(e)
                return f"[api error: {e}]"
            if SHOW_USAGE:
                _report_usage(response)
            if thinking:
                thought = [b for b in response.content if b.type == "thinking"]
                print(f"[thinking blocks: {len(thought)}]")
            self.claude_service.add_assistant_message(self.messages, response)

            if response.stop_reason == "tool_use":
                print(self.claude_service.text_from_message(response))
                try:
                    tool_result_parts = await self._run_tool_uses(response)
                except Exception as e:
                    # _run_tool_uses already turns a per-block failure (local
                    # or MCP) into an error tool_result rather than raising, so
                    # reaching here means something broke outside any single
                    # block's execution (tool routing itself). Resolve the
                    # pending blocks with a synthetic error result and stop
                    # for this turn instead of crashing the process and
                    # leaving self.messages permanently broken.
                    print(f"[tool routing error: {e}]")
                    self._resolve_pending_tool_uses(
                        response, f"tool execution failed: {e}"
                    )
                    final_text_response = (
                        f"[error running tools: {e}]"
                    )
                    break
                self.claude_service.add_user_message(
                    self.messages, tool_result_parts
                )
            elif response.stop_reason == "pause_turn":
                # A server-side tool (web_search / web_fetch) paused mid-run;
                # resend the conversation so the server resumes it.
                continue
            else:
                final_text_response = self.claude_service.text_from_message(
                    response
                )
                break

        return final_text_response
