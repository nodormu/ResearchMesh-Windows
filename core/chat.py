import os

from anthropic.types import MessageParam

from core import local_tools
from core.claude import Claude
from core.tools import ToolManager
from mcp_client import MCPClient

MAX_TOOL_ITERATIONS = 75

# Separate, small grace budget for continuations the API contract makes
# mandatory (an open pause_turn; a server_tool_use left dangling by a mixed
# tool_use response) — MAX_TOOL_ITERATIONS alone must never block these.
# 5 matches Anthropic's own reference example's `max_continuations` default.
# See Chat._finalize_turn for what happens if even this runs out. Ported
# from the Linux original's core/chat.py (commit 672aae1) — same bug class
# is reachable here too, since this fork also declares web_search/web_fetch
# as real Anthropic server tools.
EXTRA_CONTINUATION_LIMIT = 5

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
You are the assistant in a command-line research client running on the user's own Windows
machine. What follows describes your actual environment.

These 24 tools are the ones built into this client: powershell, powershell_session,
str_replace_based_edit_tool, web_search, web_fetch, memory, computer, browser_navigate,
browser_extract, browser_click, browser_fill, browser_links, browser_back,
document_convert, python, interactive_run, config_edit, sql_query, trash,
text_embeddings, vision_query, speak, listen, midi1. Any other tool in your list comes
from a connected MCP server and runs on that server — those are real; use them. But if
you are about to name a tool that is in neither group, you are mistaken.

This is a Windows machine and `powershell` is the only shell. Write real PowerShell
cmdlets: `Get-ChildItem`, `Get-Content`, `Select-String`, `Test-Path`,
`Remove-Item`, `Get-Process`, and the object pipeline (`Where-Object`, `ForEach-Object`,
`Select-Object`). There is no `grep`, `sed`, `awk`, `which`, `touch`, or `$(...)` command
substitution. The compatibility aliases PowerShell ships with — `ls`, `cat`, `rm`, `cp`,
`mv`, `ps`, `kill`, `diff`, `tee`, `pwd`, `curl`, `wget` — are removed before your command
runs, deliberately, so reaching for them fails outright instead of half-working. Paths are
native Windows paths (C:\\Users\\...), which is what every tool here both returns and
expects; write them with `\\` or `/`, both work.

Of the built-in 24, only `web_search` and `web_fetch` run on Anthropic's servers.
Everything else runs locally, in this user's own account — including the browser, which is
a headless Chromium process on this machine, so pages are fetched from the user's own
network.

There is no sandbox and no code-execution container, and there are no `code_execution`,
`bash_code_execution`, or `text_editor_code_execution` definitions in your tool list. The
2026 `web_search`/`web_fetch` variants do filter their results using server-side code
execution internally, which is likely why those names feel available — but that is
machinery inside those two tools, not something you can call. `powershell` and `python`
run as the user, with their permissions, their filesystem, and their network. Nothing you
run is isolated or automatically reversible, so treat destructive actions as real. You are
not elevated: anything needing Administrator will fail with an access error rather than
prompting, since there is no way to answer a UAC dialog from here.

State between calls:
- `python` is a persistent IPython kernel: variables, imports, and loaded data survive
  across calls. Load data once and keep working with it.
- `powershell` is a fresh process every call. `cd`, `$env:` changes, and activated
  virtualenvs do not carry over; chain with `;` in a single call instead.
- `powershell_session` is the stateful alternative to `powershell`: one real pwsh
  process that survives across calls, so `cd`/Set-Location, `$env:` changes,
  variables, functions, and imported modules all persist. Use it instead of
  `powershell` for anything that needs that; use plain `powershell` for one-off
  commands. A foreground program that blocks on its own input (a credential
  prompt, `Read-Host`) still hangs there for the call's timeout — `restart: true`
  gives a clean session if one ever gets stuck.
- The browser holds one live page, and `sql_query` one DuckDB connection, for the session.
- `memory` is the only state that outlives this process. Everything above is gone when the
  session ends; files under `/memories` are still there next time.

Choosing between overlapping tools:
- Deleting: use `trash`, which goes to the Recycle Bin and is recoverable, rather than
  `Remove-Item`.
- YAML/TOML/JSON config files: use `config_edit`. It preserves comments and key order;
  the file editor and text substitution silently destroy them.
- Commands that prompt for input: use `interactive_run`. `powershell` has no stdin and
  hangs.
- Reading the web: `browser_navigate` is the primary way, since it renders JavaScript and
  `browser_links`/`browser_back` let you follow links. Use `web_fetch` for a single known
  document you don't need to interact with.
- Querying a CSV, Parquet, or JSON file: `sql_query` reads it in place, no import step.
- Vector embeddings: there is no Anthropic-hosted embeddings endpoint, so use
  `text_embeddings` — it calls the user's own private embedding server, configured under
  `[embeddings]` in config.toml. It errors with a clear message (rather than silently doing
  nothing) if no `url` is set there yet.
- Asking a question about an image without spending an Anthropic API vision call: use
  `vision_query` — it calls the user's own private vision-capable chat server, configured
  under `[vision]` in config.toml. If that server is unset or unreachable it returns a
  `local_unavailable` status and stops rather than silently falling back — tell the user
  and get explicit confirmation before using your own vision on the image instead, since
  that means sending it to Anthropic's API rather than keeping it on their local/private
  compute.
- Producing a document: write markdown with the file editor, then `document_convert` it.
  From markdown the targets are pdf, docx, odt, html, epub, rtf, and txt — xlsx and pptx
  are reachable only from another office format, not from markdown.
- The file editor is text-only (UTF-8) apart from .png/.jpg/.jpeg, which it returns as an
  image. It cannot view PDFs or other binary files; it will return a decoding error. Use
  `powershell` or `python` to inspect those.
- Anything scriptable: prefer `powershell`, `python`, or the browser over `computer`. `computer`
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
    """tool_use ids that never got a result block — the poisoned-session check.

    The API requires every tool_use block to be answered in the *immediately
    following* message. One that isn't doesn't just break the turn it happened
    in: the block stays in the history for the life of the process, so every
    later request fails the same way, however many turns later. That failure
    reads as "it started 400ing and won't stop", which is very hard to tell
    from a context overflow without looking.

    Covers both flavors the API can leave dangling, not just the client-tool
    one:
      - a plain client `tool_use` block, answered by a `tool_result` block.
      - a `server_tool_use` (or an MCP-connector `mcp_tool_use`) block,
        answered by a tool-specific result block instead — e.g.
        `web_search_tool_result`, `web_fetch_tool_result`. This app declares
        both as real Anthropic server tools (core/claude_learned_schemas.py),
        so this is a real, reachable case, not a theoretical one. A dangling
        one is just as poisonous: the assistant turn never closed, so the
        next request 400s the same way a missing client tool_result does.
        Matched generically by suffix (`_tool_use` / `_tool_result`) rather
        than a hardcoded list of current tool names, so a future server tool
        is covered without editing this function again.

    Both flavors pair up by the same id field regardless of which specific
    block type is involved — confirmed against Anthropic's own docs: "A
    server_tool_use block and its result block pair up by tool_use_id, not
    by position."

    `Chat._finalize_turn` exists to make this list empty by the time any
    turn ends; this is how you find out it didn't. Ported from the Linux
    original's core/chat.py (commit 672aae1) — see that repo's
    researchmesh_client_dev_log.md for the full production-error history
    this closes out.
    """
    answered: set[str] = set()
    issued: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if not kind:
                continue
            if kind == "tool_use" or kind.endswith("_tool_use"):
                block_id = _block_field(block, "id")
                if block_id:
                    issued.append(block_id)
            elif kind == "tool_result" or kind.endswith("_tool_result"):
                used = _block_field(block, "tool_use_id")
                if used:
                    answered.add(used)
    return [i for i in issued if i not in answered]


def _classify_orphans(messages) -> tuple[list[str], list[str]]:
    """Split `_orphaned_tool_uses`'s output by whether each id is mechanically
    fixable or not.

    A plain client `tool_use` block can always be closed out with a synthetic
    error `tool_result` — that's what makes it "client-flavored" here. A
    `server_tool_use`/`mcp_tool_use` block (web_search, web_fetch, an MCP
    connector tool) cannot: the API expects a type-specific result block
    (`web_search_tool_result`, etc.) that this app never had the real data
    for, since the server ran it, not us. Returns (client_ids, server_ids).
    """
    ids = _orphaned_tool_uses(messages)
    if not ids:
        return [], []
    orphan_set = set(ids)
    client_ids: list[str] = []
    server_ids: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            block_id = _block_field(block, "id")
            if block_id not in orphan_set:
                continue
            kind = _block_field(block, "type")
            if kind == "tool_use":
                client_ids.append(block_id)
            elif kind and kind.endswith("_tool_use"):
                server_ids.append(block_id)
    return client_ids, server_ids


def _duplicate_tool_result_ids(messages) -> dict[str, int]:
    """tool_use_ids answered by MORE than one tool_result-family block.

    The literal API error is `invalid_request_error: ... each tool_use must
    have a single result. Found multiple tool_result blocks with id: <id>`
    — confirmed hit for real in production on the Linux original (this
    fork's sibling client, same core/chat.py lineage before this port).
    Returns {id: count}.
    """
    counts: dict[str, int] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if not kind:
                continue
            if kind == "tool_result" or kind.endswith("_tool_result"):
                used = _block_field(block, "tool_use_id")
                if used:
                    counts[used] = counts.get(used, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


def _dedupe_duplicate_tool_results(messages) -> int:
    """Mutates `messages` in place: for any tool_use_id with more than one
    tool_result-family block answering it, keep only the FIRST one seen (in
    message order — the real one from genuine tool execution) and drop the
    rest (synthetic duplicates from the now-fixed iteration-cutoff bug, or
    any other stray duplicate).

    Removes just the offending blocks from whichever message's content list
    holds them, not whole messages — never a bulk deletion. Returns how many
    blocks were removed.
    """
    dup_counts = _duplicate_tool_result_ids(messages)
    if not dup_counts:
        return 0
    seen: set[str] = set()
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            kind = _block_field(block, "type")
            is_result = bool(kind) and (kind == "tool_result" or kind.endswith("_tool_result"))
            used = _block_field(block, "tool_use_id") if is_result else None
            if is_result and used in dup_counts:
                if used in seen:
                    removed += 1
                    continue
                seen.add(used)
            kept.append(block)
        message["content"] = kept
    return removed


def _excise_dangling_blocks(messages, ids: set[str]) -> int:
    """Mutates `messages` in place: removes any block whose `id` is in `ids`
    — used only for a `server_tool_use`/`mcp_tool_use` orphan, where no
    synthetic result block satisfies the API's schema for that tool type.

    Removes only the specific dangling block(s), never the message that
    holds them (any other content in that message — text, other blocks — is
    kept) and never any other message. This is the minimal possible repair:
    contrast with wiping a turn or a conversation, neither of which this file
    does anywhere. Returns how many blocks were removed.
    """
    if not ids:
        return 0
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            block_id = _block_field(block, "id")
            if block_id in ids:
                removed += 1
                continue
            kept.append(block)
        message["content"] = kept
    return removed


def _answer_orphaned_client_tool_uses(
    messages, client_ids: list[str], content: str
) -> None:
    """Mutates `messages` in place: answers each id in `client_ids` with a
    synthetic error `tool_result`, placed so it is genuinely part of the
    message immediately following the specific message that holds that
    tool_use — never simply appended to the tail of `messages`.

    Appending to the tail (the previous behavior here, via
    `claude_service.add_user_message`) is only correct if the orphan happens
    to already be the very last thing in the conversation. It silently stops
    being correct the moment anything else has already been appended after
    the orphaning message — most commonly a plain new user query, added by
    `run()`'s own next call before this repair ever runs. Confirmed live in
    production (on the original Linux client, ported here unchanged): the
    repair ran, reported success ("answered 1 orphaned tool_use block"), and
    the retried request 400'd on the exact same id it had supposedly just
    answered — because the synthetic result landed one message too late,
    still leaving the tool_use followed by a plain user-text message instead
    of its own answer. Reproduced exactly outside production too
    (`_orphaned_tool_uses` came back empty after that "successful" repair —
    it only checks "answered somewhere later," not the API's actual
    stricter "immediately after" rule, which is why the old code believed
    it had fixed something it hadn't).

    If the message right after the orphaning one is already a `user`
    message, the synthetic result(s) are merged into the FRONT of its
    existing content (mixing tool_result blocks with other content in one
    user turn is a normal, documented shape) — this also avoids ever
    creating two consecutive `user`-role messages. Only if there is no
    following message at all is a new one inserted, matching the original
    behavior for the case it was actually correct for.

    Groups ids by which message actually holds them (usually one, but not
    guaranteed) and processes messages back-to-front so an earlier
    insertion never shifts the index of a later one out from under it.
    """
    if not client_ids:
        return
    orphan_set = set(client_ids)
    by_index: dict[int, list[str]] = {}
    for idx, message in enumerate(messages):
        content_list = message.get("content")
        if not isinstance(content_list, list):
            continue
        for block in content_list:
            if _block_field(block, "type") != "tool_use":
                continue
            block_id = _block_field(block, "id")
            if block_id in orphan_set:
                by_index.setdefault(idx, []).append(block_id)

    for idx in sorted(by_index, reverse=True):
        results = [
            {
                "type": "tool_result",
                "tool_use_id": i,
                "content": content,
                "is_error": True,
            }
            for i in by_index[idx]
        ]
        next_idx = idx + 1
        if next_idx < len(messages) and messages[next_idx].get("role") == "user":
            existing = messages[next_idx].get("content")
            if isinstance(existing, str):
                existing = [{"type": "text", "text": existing}]
            elif not isinstance(existing, list):
                existing = []
            messages[next_idx]["content"] = results + existing
        else:
            messages.insert(next_idx, {"role": "user", "content": results})


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

    def _report_api_failure(
        self, error: Exception, repair_attempted: str | None = None
    ) -> None:
        """Say which failure this is, rather than leaving it to guesswork.

        By the time this runs, `_call_chat_with_auto_repair` has already
        tried the one fully-mechanical repair this app knows how to do
        (dedupe/answer/excise dangling tool blocks — see
        `_auto_repair_poisoned_history`) and retried once.
        `repair_attempted` carries what it found and fixed, if anything.

        This never recommends `/clear`, or any other action that discards
        conversation content, anywhere — deliberately (ported from the
        Linux original's core/chat.py commit 672aae1, whose whole point was
        removing exactly that from every automated path — `/clear` still
        exists as the manual command above, untouched). If nothing here
        could be mechanically repaired, the honest thing to do is report the
        facts (the error, the size, any orphans still present after the
        repair attempt) and leave the decision to the user, not prescribe a
        destructive default.
        """
        text = str(error)
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)

        print(f"[api error] {text}")
        print(f"[api error] conversation: {count} messages, ~{chars:,} chars")

        if repair_attempted:
            print(
                f"[api error] an automatic repair ran first ({repair_attempted}), "
                "but the retried request still failed — this is a different, "
                "unrelated problem."
            )

        if orphans:
            print(
                f"[api error] {len(orphans)} unanswered tool_use block(s) "
                f"still present after the repair attempt: "
                f"{', '.join(orphans[:3])}"
                f"{' …' if len(orphans) > 3 else ''}"
            )
        elif "too long" in text.lower() or "context" in text.lower():
            print(
                "[api error] the conversation has outgrown the context window."
            )

    def _auto_repair_poisoned_history(self) -> str | None:
        """Mechanical, unconditionally-safe repair of `self.messages`,
        tried whenever a real `chat()` call raises: dedupe any duplicate
        tool_result, answer any orphaned client tool_use, excise any
        orphaned server-flavored block. Touches only the offending blocks,
        never a whole message or the conversation.

        Ported from the Linux original's core/chat.py (commit 672aae1).

        Returns a short description of what was repaired, or None if there
        was nothing here to fix (a real network/auth error, a genuine
        context-window overflow, or some other cause).
        """
        repairs: list[str] = []

        removed_dupes = _dedupe_duplicate_tool_results(self.messages)
        if removed_dupes:
            repairs.append(
                f"removed {removed_dupes} duplicate tool_result block"
                f"{'s' if removed_dupes != 1 else ''}"
            )

        client_ids, server_ids = _classify_orphans(self.messages)
        if client_ids:
            _answer_orphaned_client_tool_uses(
                self.messages,
                client_ids,
                "[repaired: this tool_use was never answered]",
            )
            repairs.append(
                f"answered {len(client_ids)} orphaned tool_use block"
                f"{'s' if len(client_ids) != 1 else ''}"
            )
        if server_ids:
            _excise_dangling_blocks(self.messages, set(server_ids))
            repairs.append(
                f"removed {len(server_ids)} dangling background tool block"
                f"{'s' if len(server_ids) != 1 else ''} (no synthetic fix "
                f"exists for these)"
            )

        return "; ".join(repairs) if repairs else None

    def _call_chat_with_auto_repair(self, tool_defs, thinking):
        """The one real `chat()` call site: on failure, try
        `_auto_repair_poisoned_history` and retry once. Returns the
        response on success (either attempt), or None if both raised
        (`_report_api_failure` already called in that case).
        """
        try:
            return self.claude_service.chat(
                messages=self.messages,
                system=SYSTEM_PROMPT,
                tools=tool_defs,
                thinking=thinking,
            )
        except Exception as e:
            repair = self._auto_repair_poisoned_history()
            if repair is None:
                self._report_api_failure(e)
                return None
            print(f"[api error] {e}")
            print(f"[api error] auto-repaired the conversation history: {repair}")
            print("[api error] retrying this request once...")
            try:
                return self.claude_service.chat(
                    messages=self.messages,
                    system=SYSTEM_PROMPT,
                    tools=tool_defs,
                    thinking=thinking,
                )
            except Exception as e2:
                self._report_api_failure(e2, repair_attempted=repair)
                return None

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

    def _finalize_turn(self, reason: str) -> str:
        """Close out a turn that's ending abnormally. Always re-scans the
        live `self.messages` for what's actually still dangling — never
        trusts a cached `response` object (that was the source of a real
        duplicate-tool_result bug on the Linux original, this fork's sibling
        client — same core/chat.py lineage before this port; see that
        repo's researchmesh_client_dev_log.md for the full production-error
        history).

        Never deletes a turn, a message, or the conversation — only the
        specific dangling block(s), each in the minimal way its flavor
        allows: client `tool_use` gets a synthetic error `tool_result`;
        server-flavored (`server_tool_use`/`mcp_tool_use`) has no valid
        synthetic result, so the block itself is excised in place.

        Replaces the old `_resolve_pending_tool_uses` (only ever handled
        the client-tool_use case, and only when `stop_reason == "tool_use"`
        — a dangling server_tool_use at the iteration cutoff during an
        open pause_turn was never resolved at all).

        Returns the text to show the user.
        """
        client_ids, server_ids = _classify_orphans(self.messages)
        if client_ids:
            _answer_orphaned_client_tool_uses(
                self.messages, client_ids, f"[{reason}]"
            )
        base = f"[{reason}]"
        if server_ids:
            _excise_dangling_blocks(self.messages, set(server_ids))
            base += (
                f" {len(server_ids)} background tool call"
                f"{'s' if len(server_ids) != 1 else ''} that never finished "
                f"{'were' if len(server_ids) != 1 else 'was'} removed so the "
                "conversation can continue — nothing else was touched."
            )
        return base

    async def run(self, query: str, thinking: bool=False) -> str:
        final_text_response = ""
        self.claude_service.add_user_message(self.messages, query)

        # The MCP tool list is fetched once per user turn, not once per
        # tool-use iteration — it can't change mid-turn, and re-listing was a
        # round trip per client per loop pass (up to MAX_TOOL_ITERATIONS).
        mcp_tools = await ToolManager.get_all_tools(self.clients)
        tool_defs = local_tools.TOOLS + mcp_tools

        # Index of this turn's own assistant message while a pause_turn
        # continuation is open. Anthropic's own reference implementation
        # REPLACES this slot on each continuation rather than appending a
        # sibling message — an unconditional append here (the pre-port
        # behavior) stacks multiple consecutive assistant-role messages
        # with zero user messages between them across a multi-continuation
        # pause_turn sequence, a real contract violation independent of
        # the duplicate-tool_result bug below. Ported from the Linux
        # original's core/chat.py (commit 672aae1) — see that repo's
        # researchmesh_client_dev_log.md for the full production-error
        # history this closes out.
        pending_pause_turn_idx: int | None = None

        iterations = 0
        extra_continuations = 0
        # Set only for the two cases where the next chat() call is
        # API-mandated, not optional: an open pause_turn, or a
        # server_tool_use left dangling by a mixed tool_use response.
        # Reset every pass so the grace budget below is never spent on an
        # ordinary continuation once the main budget runs out.
        mandatory_continuation = False
        while True:
            if iterations >= MAX_TOOL_ITERATIONS:
                if not mandatory_continuation:
                    final_text_response = "[stopped: exceeded tool-iteration limit]"
                    break
                if extra_continuations >= EXTRA_CONTINUATION_LIMIT:
                    final_text_response = self._finalize_turn(
                        "stopped: exceeded tool-iteration limit"
                    )
                    break
                extra_continuations += 1
            else:
                iterations += 1
            mandatory_continuation = False

            response = self._call_chat_with_auto_repair(tool_defs, thinking)
            if response is None:
                return "[api error: chat request failed]"
            if SHOW_USAGE:
                _report_usage(response)
            if thinking:
                thought = [b for b in response.content if b.type == "thinking"]
                print(f"[thinking blocks: {len(thought)}]")

            if pending_pause_turn_idx is not None:
                self.messages[pending_pause_turn_idx]["content"] = response.content
            else:
                self.claude_service.add_assistant_message(self.messages, response)

            if response.stop_reason == "pause_turn":
                # A server-side tool (web_search / web_fetch) paused mid-run;
                # resend the conversation so the server resumes it.
                if pending_pause_turn_idx is None:
                    pending_pause_turn_idx = len(self.messages) - 1
                mandatory_continuation = True
                continue

            pending_pause_turn_idx = None

            if response.stop_reason == "tool_use":
                print(self.claude_service.text_from_message(response))
                try:
                    tool_result_parts = await self._run_tool_uses(response)
                except Exception as e:
                    # Genuinely unanswered — first time seeing these blocks,
                    # not a re-resolution.
                    print(f"[tool routing error: {e}]")
                    final_text_response = self._finalize_turn(
                        f"tool execution failed: {e}"
                    )
                    break
                self.claude_service.add_user_message(
                    self.messages, tool_result_parts
                )

                # A dangling server_tool_use here means the API owes us one
                # more mandatory round trip to resolve it (Server tools doc).
                _, server_ids = _classify_orphans(self.messages)
                if server_ids:
                    mandatory_continuation = True
                    continue

                if iterations >= MAX_TOOL_ITERATIONS:
                    final_text_response = "[stopped: exceeded tool-iteration limit]"
                    break
                continue

            # Anything else (end_turn, stop_sequence, and critically
            # max_tokens) falls through here. A max_tokens cutoff that hit
            # mid-tool_use — e.g. a single large `create` call whose file
            # content ran past the token budget — still gets its content
            # appended above like any other assistant turn, tool_use block
            # included, but stop_reason is "max_tokens", not "tool_use", so
            # nothing above ever routed/answered it. Left alone, that
            # tool_use sits unresolved past the end of this run() call and
            # poisons every later turn (confirmed live in production on the
            # original Linux client, and reproduced in isolation — see that
            # repo's researchmesh_client_dev_log.md). Re-check the live
            # message list rather than trusting stop_reason alone, and
            # finalize instead of returning as if this were an ordinary
            # finished turn.
            if _orphaned_tool_uses(self.messages):
                final_text_response = self._finalize_turn(
                    f"stopped: response ended early (stop_reason="
                    f"{response.stop_reason!r}) with an unresolved tool_use"
                )
                break

            final_text_response = self.claude_service.text_from_message(
                response
            )
            break

        return final_text_response
