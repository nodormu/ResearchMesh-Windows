# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResearchMesh-Windows is a command-line chat client for the Anthropic API, built on the Model Context Protocol (MCP). The CLI talks to Claude and to one or more MCP servers, and additionally gives Claude **18 local tools** — Anthropic's built-in "learned" schemas (text editor, web search/fetch, cross-session memory, computer use) plus custom ones for PowerShell, DOM browsing, document conversion, stateful Python, interactive commands, config editing, SQL, and recoverable deletes. It began as a learning/tutorial project (a Skilljar submodule) but has since been rewired to connect to any number of external MCP servers over Streamable HTTP, declared as a list under `[mcp]` in `config.toml`, instead of the original bundled stdio document server.

**This is a Windows-only fork** of [ResearchMesh](https://github.com/nodormu/ResearchMesh), taken at commit `9e6959b`. Windows is assumed outright: there is not a single `sys.platform` check left in the app, and the only one anywhere is in `smoke_test.py`, which has to know where it is running in order to skip. Do not add cross-platform branches, and do not reintroduce shell idioms from other platforms — if something looks like it was written for a different operating system, it is a bug, not compatibility.

Two things follow from the fork that are easy to trip over:

- **`core/powershell.py` is the shell tool, and it is a custom schema on purpose.** Anthropic's learned `bash_20250124` was not kept. A learned schema is normally free, but that is what ruled this one out: the name trains the model to emit shell for it, and no interpreter behind it changes what the name asks for. A custom schema is what lets the description say "write PowerShell cmdlets" and be believed. It goes further and **deletes PowerShell's compatibility aliases** (`ls`, `cat`, `rm`, `cp`, `mv`, `ps`, `kill`, `diff`, `tee`, `pwd`, `curl`, `wget`, …) before every command, so reaching for one fails outright instead of failing confusingly on a parameter.
- **Nothing here has been run by hand on Windows.** `ruff`, `mypy` and `smoke_test.py` pass, and CI runs on `windows-latest` — which makes GitHub Actions the only place this code has executed on its target platform at all. Treat a green run as "it imports, wires up and handshakes", which is well short of "the tools work". `document_convert`, `interactive_run` and `computer` are the three most likely to still need a real session.

## Commands

Run the app (**from the root project folder**, not from `core/`):

```powershell
python main.py
```

Requires environment variables, read from the process environment — the app does **not** load a `.env` file. `setx` writes them to the user environment but only affects shells started *afterwards*, so set both for the current session too:

```powershell
setx ANTHROPIC_API_KEY "..."      # persists; new shells only
$env:ANTHROPIC_API_KEY = "..."    # this session
$env:N8N_MCP_TOKEN     = "..."    # one per server, named by its token_env in config.toml
```

Check every configured MCP server standalone (connects to each, lists tools, reports failures, exits):

```powershell
python mcp_client.py
```

Connect additional stdio MCP servers by passing their scripts as argv: `python main.py path\to\other_server.py`.

One-time setup for the browser tool (headless Chromium via Playwright — `pip` installs the package but not the browser binary), plus the two binaries `document_convert` shells out to:

```powershell
pip install -r requirements.txt
playwright install chromium
winget install TheDocumentFoundation.LibreOffice   # document_convert
winget install JohnMacFarlane.Pandoc              # document_convert: the markdown path
```

There is no `playwright install-deps` step: that installs shared libraries for other operating systems and does not apply here.

Two things about those two binaries, both of which fail in ways that do not look like the cause:

- **LibreOffice does not put itself on PATH**, so `shutil.which("soffice")` finds nothing on a perfectly good install. `core/documents.py` also checks `C:\Program Files\LibreOffice\program` and its 32-bit sibling.
- **It invokes `soffice.com`, never `soffice.exe`.** The `.exe` is linked as a GUI binary: run from a console it spawns the real process, detaches, and returns exit code 0 *before converting anything*, so a success check passes on a file that was never written. The `.com` sibling in the same directory is the console entry point that actually blocks and returns a real exit code.

Pandoc's installer *does* extend PATH, but an already-running shell keeps its old copy — restart the client after installing it.

The `computer` tool needs no system packages: `pyautogui` and `pillow` are the whole dependency and screen capture works out of the box. The one thing to know about it is **UIPI**: a process at medium integrity cannot send input to a window owned by an elevated one, so if anything running as Administrator has focus, clicks and keystrokes are discarded and the screenshot afterwards looks exactly like a click that missed. It is not refused up front, because it depends on which window has focus at that instant rather than on the machine — see the `core/computer.py` bullet under Architecture.

Optional tool dependencies are imported **lazily, inside the tool that needs them**, so a missing package only breaks that one tool — it still gets declared to Claude and returns an install hint if used. To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py`.

If a tool's install hint names a package that `requirements.txt` already lists (e.g.
`sql_query`'s `duckdb`, or `config_edit`'s `ruamel.yaml`/`jsonpath-ng`), the docs aren't
incomplete — the active venv predates that line. There is no lockfile here: every entry in
`requirements.txt` is a `>=` floor rather than a pin, so a venv can still satisfy the file as
it stood when it was built and lack a package added to it since. Re-running
`pip install -r requirements.txt` fixes it **without restarting the app** — every optional
backing is imported inside the function that needs it, and a failed import leaves no cached
sentinel behind (`core/data.py` assigns `_connection` only on success), so the next tool call
simply retries the import.

See **`README.md`** for the full environment setup — the quick start is at the top, and the collapsed "Full setup detail" section covers the browser download, the optional per-tool packages, and environment variables. (`SETUP.md` was merged into it; the two duplicated ~60% of their content and drifted apart.)

**One linter is configured: `ruff`.** `pyproject.toml` has a `[tool.ruff.lint]` section, so
**`ruff check .` should come back clean** — treat that as the bar for an edit. It adds no
rules; it only lists exemptions, each with its reason beside it. A run that is *not* clean
means the finding is new: either something you just wrote, or a rule a newer ruff added
(`select` is deliberately left at ruff's defaults, which do shift between versions —
`BLE001`/`S110`/`PLW1510` only began appearing around 0.16). Triage it rather than assuming
it's more of the same. Ruff itself is not a project dependency and nothing runs it for you.

**`python smoke_test.py` is the other gate**, and CI (`.github/workflows/ci.yml`) runs it plus
`ruff` and `mypy` on every push and PR to `main`. It is not a test suite: it never exercises a
tool's behaviour, because that would need LibreOffice, a browser, a real desktop and real API
credits. It checks the four things that break silently — everything imports, the tool registry
is well-formed with no duplicate names, **the tool count claimed in the docs still equals
`len(local_tools.TOOLS)`**, and `mcp_server.py` completes an MCP handshake advertising
`delegate`. That third check exists because this repo states its tool count in five places
across two files; the fourth because a stray byte on stdout desynchronising JSON-RPC is
invisible until a client connects. It needs no API key (a placeholder satisfies
`_require_api_key`, and listing tools never reaches the API) and no optional packages, since
every optional backing is imported lazily — which is why CI installs only the five
module-level dependencies and finishes in seconds.

**The handshake check is Windows-only and reports a `skip` elsewhere.** `mcp_server.py`'s
stdout guard imports `msvcrt` and calls `SetStdHandle` unconditionally, so off Windows the
server exits at import and the check could only ever report `MCPError: Connection closed` —
naming nothing, because the server dies before the transport is up. A check that fails for an
unactionable reason on the machine you develop on is a check you learn to ignore, so it skips
with the reason stated, still exits 0, and the summary names what was skipped
(`all checks passed (1 skipped: handshake completes)`). **A green run off Windows is therefore
not full coverage** — the other three checks are platform-independent and do run everywhere.

**Both CI jobs run on `windows-latest`**, including the committed-credential scan — `git grep`
is implemented inside git rather than shelling out, so its regexes carry over and only the
control flow around them is PowerShell. Note the default shell on a Windows runner is
PowerShell, where `\` is not a line continuation (it is a backtick) and `${VAR}` expands
inside a double-quoted string; both have already broken a `run:` block here.

**`mypy .` is the third gate**, configured in `pyproject.toml`'s `[tool.mypy]` and run by CI
alongside `ruff`. It should come back clean. Only one option is set —
`ignore_missing_imports`, because the optional tool backings are lazily imported and
legitimately absent from a bare environment, and **`platform = "win32"`**, so it checks the
platform that ships rather than the one it happens to run on. That second option is what lets
`core/computer.py`'s `ctypes.windll` calls type-check wherever the gate is run, and code that
is wrong *on Windows* is reported even where it would otherwise pass.
Strictness is left at mypy's defaults, so
bodies of unannotated functions go unchecked and annotating a function is what opts it in.
It earns its slot for a specific reason: **mypy checks against the packages actually
installed**, which makes it the one gate that notices a dependency changing shape. Every
mcp 1.x → 2.x breakage in this repo was caught in a single run, each renamed attribute named
with its new spelling — including the three in `core/tools.py` that `smoke_test.py`
structurally cannot reach, since it never calls an MCP tool. Note the flip side: run against
an *old* installed version it stays quiet, so it warns at upgrade time, not before.

There are still **no unit tests**. `pylint`/`black` (in whatever venv you run the project
from) and `shellcheck` (system) are unconfigured but safe to run by hand. For a quick manual
check, `python -m py_compile` and an import smoke test (`PYTHONPATH=.. python -c "import
core.chat"`) are what `smoke_test.py` automates.

Two rules about this codebase that a linter will fight you on, both learned the hard way:

- **Blanket `except` is the architecture, not an oversight** (`BLE001`, ~32 sites). Every
  local tool must catch anything and return an error string rather than crash the chat loop
  (see `core/chat.py`'s `_run_tool_uses` / `_resolve_pending_tool_uses`); the ones in
  `core/cli.py`, `core/tools.py`, `core/chat.py` and `main.py` are the equivalent guards for
  the REPL, the MCP execute path and the connect fallback. Don't narrow them.
- **Cleanup paths must not be able to fail, and must not fail silently either.** Narrowing
  an exception type in a `shutdown`/`close` path has already caused one real bug: `zmq.ZMQError`
  derives from `Exception`, *not* `OSError`, so an `except (RuntimeError, OSError)` on
  `core/kernel.py`'s `stop_channels()` — which ends in pyzmq's `context.destroy()` — let it
  escape through `local_tools.shutdown()` and the `AsyncExitStack` into a traceback on an
  ordinary Ctrl-C. Use a blanket catch **plus a `print()`**: when `S110` (`except: pass`)
  fires, the defect it names is the silence, not the breadth.

**Shutdown is isolated per tool.** `local_tools.shutdown()` runs each tool's cleanup in its
own `try`, because it is an `AsyncExitStack` callback: an exception escaping any one of them
skips every *later* one (leaking whatever that tool owns) as well as producing that
traceback. `browser.shutdown()` runs first and is itself unguarded, so before this it could
take the kernel and the DuckDB connection down with it. `main.py`'s post-failed-connect
cleanup follows the same rule.

**`subprocess.run` here is deliberately `check=False`** (`core/powershell.py`,
`core/documents.py` — the only two modules left that shell out at all). Both functions exist
to turn a non-zero exit into a normal return value — a JSON payload carrying `exit_code` for
`powershell`, an `(ok, output)` tuple for `document_convert` — so `check=True` is the wrong
fix for `PLW1510`: the resulting `CalledProcessError` would either be reported as a generic
error with stdout lost, or in `documents.py` not be caught at all (`_run` handles only
`FileNotFoundError` and `TimeoutExpired`). The explicit `check=False` is the existing default
written out, satisfying the rule while changing no behaviour.

**Neither of them passes `shell=True`, and that matters here.**
`subprocess` with `shell=True` hard-codes `<executable> /c "<args>"` — cmd.exe's flag — onto
whatever `executable=` names, so a PowerShell interpreter would be handed a `/c` it does not
understand instead of the `-Command` it wants. Both modules build the argv list themselves.

The blow-by-blow of how these were triaged lives in `git log`, not here.

## Runtime configuration

- `ANTHROPIC_API_KEY` — read from the environment (`setx` for the user environment, `$env:` for the current session; the app does not load `.env`). `main.py` keeps an explicit `os.getenv("ANTHROPIC_API_KEY")` reference on purpose; **do not remove it** even though `core/claude.py` also constructs its own `Anthropic()` that reads the same env var.
- **MCP bearer tokens** — each `[mcp].servers` entry may set `token_env` naming the environment variable that holds its token; it is sent as `Authorization: Bearer <token>`. A server with no `token_env` connects unauthenticated. Tokens are never stored in `config.toml`.
- `RESEARCHMESH_MCP_TOKEN` — the *serving* side of that same contract: the bearer token `mcp_server.py --transport streamable-http` requires from its clients (rename with `--token-env`). Unset means the endpoint is unauthenticated, which is allowed by design; only stdio needs no token at all, since there is no port. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` — there is deliberately no generator script, as a file wrapping one line of stdlib fails the same "must beat `powershell` at something structural" test that governs the tools. Three placement rules that are easy to get wrong: a Windows **service** (or a Scheduled Task) does not inherit the interactive shell's environment, so a `$env:` assignment or even a fresh `setx` will not reach it — set it machine-wide or in the service's own environment; an MCP client passes a stdio server only a small safe env subset, so the variable must be named in that server's `env` block in the client config; and the literal token must never land in `.mcp.json` or `config.toml`, both of which are committed — use `${VAR}` and `token_env`. It is one token and each end reads the variable from its own environment, so serving and consuming machines normally share the *same* name — a second name is only needed if one machine both serves an endpoint and consumes another, where one variable would otherwise have to mean two different secrets at once.
- **`%VAR%` / `$VAR` in `[mcp].servers`** — `tomllib` does no substitution, so `main.py`'s `_expand_paths()` expands `~` and `%VAR%`/`$VAR`/`${VAR}` in `command`, `url`, and the *values* of `env` before the entry reaches `build_client()`. That is what lets the committed config say `C:/Users/%USERNAME%/...` instead of one developer's home directory. `env`'s keys are variable names and are deliberately not expanded, and an undefined variable is left verbatim (`expandvars`' behaviour) so it surfaces in the "could not reach/launch" warning rather than collapsing to a path with an empty segment. Use the Windows names — `%USERNAME%`, `%USERPROFILE%`, `%APPDATA%`, `%LOCALAPPDATA%`. Note also that TOML treats `\` as an escape inside a `"basic string"`, so `"C:\Users\me"` is invalid TOML: use forward slashes (every Windows API accepts them) or a `'literal string'`.
- `CLAUDE_SHOW_USAGE` — set to `1` to print per-request token and prompt-cache counters (see `core/chat.py`). Prompt caching fails silently, so this is how you confirm the `cache_control` breakpoint is landing.
- The Claude model comes from `config.toml` (`[claude] model`), overridable by the `CLAUDE_MODEL` env var (default `claude-sonnet-5`). The app does **not** load a `.env` file — `ANTHROPIC_API_KEY` and any MCP tokens come from the process environment.
- The 2026 web-tool schemas (`web_search_20260318` / `web_fetch_20260318`) need a current `anthropic` SDK to parse the server-tool result blocks (`pip install -U anthropic`). These tools are versioned by capability rather than superseded — each dated variant is a superset of the last — so check the [tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference) for a newer date before assuming the pinned one is current.
- **`mcp>=2,<3` is a hard floor, not a preference** (`requirements.txt` carries the full note). mcp 2.0 is a breaking major and this code uses the new API on both sides: `Server(on_list_tools=…, on_call_tool=…)` in `mcp_server.py` (1.x's `@server.list_tools()` decorators no longer exist, and handlers now take a leading request context and return a whole `ListToolsResult`/`CallToolResult` instead of a bare content list), `streamable_http_client` in `mcp_client.py` (renamed, and headers now ride an httpx2 client from `create_mcp_http_client()` because the transport dropped `headers=`), and snake_case model fields in `core/tools.py` (`input_schema`, `is_error`, `mime_type`). The camelCase spellings survive as *serialization aliases*, so constructing a model still works either way while attribute reads break — which is why the 1.x→2.x failure in `core/tools.py` is a runtime `AttributeError` that no import check catches. The upper bound matches what `claude-agent-sdk` declares (`mcp<3.0.0`), so both can share a venv; it pinned `mcp<2.0.0` until 0.2.140.
- `CLAUDE_MEMORY_DIR` — where the `memory` tool's virtual `/memories` tree actually lives (default `./memories`, relative to the repo root the app must run from).
- `CLAUDE_KERNEL_ENCRYPTION` — `auto` (default), `required`, or `off`, controlling the first tier of `core/kernel.py`'s transport ladder. `auto` tries CurveZMQ and falls back with a printed reason; `required` turns a failure into a tool error instead of an unencrypted kernel; `off` skips straight to plaintext TCP. An unrecognised value is reported and treated as `auto`. **Worth setting to `required` here**, more than it was upstream: the ladder is only two tiers deep on Windows (the middle IPC tier is gone — see the `core/kernel.py` bullet), so CurveZMQ is the only thing between the kernel link and four plaintext loopback ports, and only a printed line distinguishes the two outcomes.
- `CLAUDE_DISPLAY_SIZE` — the logical screen size `computer` declares and downscales to, e.g. `1280x800` (default). Below ~1280x720 accuracy drops; it must never be set to something the module doesn't also resize screenshots to.
- Python 3.11+ (`pyproject.toml`) — the floor is `tomllib`, used by `main.py`.

## Architecture

Request flow: **CLI input → Chat.run() agentic loop → Claude API + (local tools | MCP server tools)**.

- **`main.py`** — entrypoint (in the repo root). Reads the API key, builds a `Claude` service, connects every enabled `[mcp].servers` entry over Streamable HTTP via `build_client()` / `_connect_mcp_servers()` (which runs `_expand_paths()` on each entry first, so `%USERNAME%`/`~` in paths resolve) — a server that fails is reported and skipped rather than aborting startup — plus any stdio servers passed as argv, registers `local_tools.shutdown` on the `AsyncExitStack`, wires everything into a `Chat`, and runs the `CliApp` loop. An argv-passed stdio server is spawned with **`sys.executable`, never a bare `"python"`**: on Windows PATH usually resolves that either to the App Execution Alias that opens the Microsoft Store (a stub that exits without running anything) or to a system interpreter that is not this venv. **No event-loop policy is set** — the upstream `WindowsProactorEventLoopPolicy` call was already a no-op, since Proactor has been the Windows default since 3.8, and `set_event_loop_policy` is deprecated as of 3.14 and slated for removal in 3.16. Proactor is what is wanted (Selector has no subprocess support, which the stdio MCP transport needs) and `asyncio.run` already provides it.

- **`core/chat.py`** (`Chat`) — the agentic loop, plus `SYSTEM_PROMPT`, sent as `system` on every request. That prompt exists because with tool schemas alone Claude describes capabilities it doesn't have (it invented a sandboxed `code_execution` container in testing); it states the two execution locations, that nothing is sandboxed, which tools are stateful, and how to choose between the overlapping ones. Keep it factual — if you add or remove a tool, update it. Each turn it calls Claude with the merged tool set (`local_tools.TOOLS + ToolManager.get_all_tools(...)`, the MCP half fetched once per turn). While `stop_reason == "tool_use"` it routes each `tool_use` block — `local_tools.execute()` first, then `ToolManager.execute_blocks()` (MCP) if no local module owns the name — feeds the results back, and loops. `stop_reason == "pause_turn"` (server-side web tools mid-run) is handled by resending. Capped at `MAX_TOOL_ITERATIONS`.

- **`core/local_tools.py`** — the registry of client-executed tools. Every module in `MODULES` exposes the same three names (`TOOLS`, `handles(name)`, `await execute(name, input)`), so a new tool is one new module plus one line here rather than edits to the chat loop's declaration list *and* its routing chain. Raises at import on duplicate tool names, and `shutdown()` releases everything the tools may have started (browser, kernel, DuckDB) — each in its own `try`, so one tool failing to clean up neither skips the others nor turns Ctrl-C into a traceback (see "Shutdown is isolated per tool" above).

- **`core/claude_learned_schemas.py`** — Anthropic's built-in ("learned") tools. The text editor (`text_editor_20250728` / `str_replace_based_edit_tool`) is **client-executed** here; `web_search` (`web_search_20260318`) and `web_fetch` (`web_fetch_20260318`) are **server-executed** by Anthropic (declaration only, no local handler). `handles()` reports the one client-side name; `execute()` runs it off the event loop via `asyncio.to_thread`. **The editor preserves line endings, which takes deliberate effort here:** `Path.write_text` opens in text mode and Windows text mode rewrites every `\n` as `\r\n`, so a one-line `str_replace` on an LF file would silently convert the whole file. `_read`/`_write` pass `newline=""` to disable translation in both directions. Matching then has to happen on an LF-normalised copy, because `_view` shows Claude the file through `splitlines()` and the `old_str` it sends back never contains `\r` — against raw CRLF bytes an exact match would find nothing and every edit to a CRLF file would fail with "old_str not found". A file with *mixed* endings is normalised to its dominant one; that is the only case not preserved.

- **`core/powershell.py`** — `powershell`: the shell tool. A custom schema, so its description carries the weight a learned schema would not need: it names the canonical cmdlets and states that the compatibility aliases have been removed. That removal is a prelude prepended to every command (`_ALIAS_PRELUDE`), and the reason is that an alias fails *worse* than a missing command — `ls -la` reaches a real cmdlet and errors on `-la`, saying nothing about the actual problem, where a deleted alias gives "The term 'ls' is not recognized". `sort` is deliberately left alone: Windows ships a real `sort.exe`, so removing it would silently swap object sorting for text sorting rather than erroring. Output has ANSI escapes stripped and runs with `NO_COLOR=1`, since PowerShell 7 colourises errors even into a pipe. Prefers `pwsh` (PowerShell 7+) and falls back to the Windows PowerShell 5.1 that ships with the OS, checking its fixed System32 location as well as PATH; the response reports which one ran, so a script using a 7-only cmdlet fails legibly. `-NoProfile -NonInteractive`, and an explicit argv list rather than `shell=True` (see the `subprocess` note above). Stateless between calls.

- **`core/memory.py`** — `memory` (`memory_20250818`), Anthropic's client-executed memory tool. A learned schema, so no description. `/memories` is a **virtual prefix**, not a real path: `_resolve()` maps it onto one real directory (`CLAUDE_MEMORY_DIR`, default `./memories`) and canonicalises *before* testing containment, so `..` segments and escaping symlinks are both caught — that confinement is the one hard requirement Anthropic places on the client, since `/memories/../../.ssh/id_rsa` is otherwise a key read. The return strings deliberately match the reference wording in Anthropic's docs; Claude was trained against it, so rewording them makes it misread ordinary outcomes as failures. Two deliberate deviations: `create` overwrites rather than erroring (Claude's own description says "creates or overwrites"), and `view` on a `.png`/`.jpg` returns an `image_result` marker. **This is the only local state that survives process exit** — the kernel, browser page, and DuckDB connection are all per-session. Containment already fails closed on Windows (a drive-relative `D:foo` re-anchors the join onto another drive and a backslash traversal is a real separator, so both resolve outside the root and are rejected), and comparison is `normcase`d because NTFS is case-insensitive while `is_relative_to` compares parts exactly. Two Windows-only rejections were added for things that pass containment and are still wrong: **reserved device names** (`NUL`, `CON`, `COM1`…, at any directory level, with or without an extension — writing to `memories/NUL` reports success and discards the content), and a **colon in a leaf name**, which addresses an NTFS alternate data stream rather than a file.

- **`core/computer.py`** — `computer` (`computer_20251124`), Anthropic's client-executed computer use tool: screen capture plus mouse/keyboard via `pyautogui` (both imported lazily). Two things dominate the design. **Coordinates:** Claude answers in the coordinate space of the image it was sent, so a declared `display_width_px`/`display_height_px` that disagrees with the screenshot offsets every click. The module therefore declares one fixed logical size (`CLAUDE_DISPLAY_SIZE`, default 1280x800), always resizes captures to exactly that, and scales coordinates back to native in `_to_native()` — declared size and sent image cannot drift. **Beta gating:** `computer_20251124` needs the `computer-use-2025-11-24` header, exported here as `BETA_FLAG` and consumed by `core/claude.py`, which is why the whole app posts to the beta endpoint. Actions other than `wait` return a screenshot, matching the reference implementation Claude was trained against.

  Three Windows specifics, all of which are about not silently doing the wrong thing:

  - **DPI awareness** (`_set_dpi_aware`, tried per-monitor-v2 → per-monitor → system-wide) is the Windows form of the coordinate problem above. A process that has not declared it is lied to by the OS: at 150% scaling `pyautogui.size()` reports the virtualised size while `ImageGrab` captures the real framebuffer, so `_to_native()` divides by one and multiplies by the other and every click drifts toward the bottom-right.
  - **Capture is the primary monitor only, deliberately.** Pillow's `all_screens=True` would grab the whole virtual desktop and break the coordinate contract in the other direction, since `_to_native` scales by `pyautogui.size()`. Real multi-monitor support means teaching it the virtual-desktop bounds *and* origin, which can be negative — a feature, not a flag flip.
  - **`type` does not use `pyautogui.write`.** PyAutoGUI's Windows `keyboardMapping` covers only `chr(32)`–`chr(127)` and `_keyDown` returns early for anything else, so every non-ASCII character was dropped while the tool reported the full length as typed; off a US layout `VkKeyScanA` returns `-1`, which passes the `is None` guard and yields a garbage virtual-key code, so the *wrong* character is typed rather than none. It now uses `SendInput` with `KEYEVENTF_UNICODE`, which injects UTF-16 code units directly and ignores the layout (non-BMP characters go as surrogate pairs). The `INPUT` structs use fixed-width types rather than `c_long`, which is 4 bytes on Windows but follows the host's C `long` elsewhere and so made the layout unverifiable off-platform. `sizeof(_INPUT) == 40` is a claim worth keeping testable: a wrong `cbSize` makes `SendInput` send nothing *and* report success.

  **UIPI is the standing limitation.** A medium-integrity process cannot send input to an elevated window, so with anything running as Administrator focused, clicks and keys are discarded and the screenshot looks like a click that missed. It is not refused up front, because it is a property of whichever window has focus at that instant rather than of the machine. `_type` catches it — `SendInput` returns how many events it delivered — but **the mouse actions have no equivalent signal and fail silently.** First thing to suspect when a sequence has no visible effect.

- **`core/browser.py`** — a custom **Playwright** browser tool (`browser_navigate` / `_extract` / `_click` / `_fill` / `_links` / `_back`). Fully custom schemas (Claude learns them from descriptions). A single headless page is kept alive across calls (lazy-launched — `playwright` is imported only on first use, so the module imports fine without it), and each tool trims its output to avoid context bloat. `shutdown()` closes the browser on exit. The point of this tool is **DOM-based surfing**, so `browser_navigate` is described as the primary way to read the web and every page-changing call reports the current URL — there is deliberately no separate "current URL" tool. `_trim()` flattens newlines and is for prose only; element lists use `clip()` so their line structure survives.

- **`core/documents.py`** — `document_convert`: headless LibreOffice (`soffice --convert-to`) with a **throwaway `-env:UserInstallation` profile per call**, because LibreOffice locks its user profile and a second concurrent call otherwise fails silently. Markdown sources route through **pandoc** (soffice has no dependable markdown import); `md → pdf` goes md → odt (pandoc) → pdf (soffice), since pandoc's own PDF writer needs a LaTeX engine. Two Windows specifics, both of which produce a *silent wrong answer* rather than an error: it resolves **`soffice.com`, not `soffice.exe`** (the `.exe` detaches and returns 0 before converting anything, so the success check passes on a file that was never written), and it falls back to the standard install directories because the installer never touches PATH. The profile flag uses `Path(profile).as_uri()` — pasting a native path after `file://` yields a URL whose *host* is `C:`, which LibreOffice resolves to nothing and then quietly falls back to the shared default profile, reintroducing the exact locking collision the flag exists to prevent.

- **`core/kernel.py`** — `python`: a persistent IPython kernel over `jupyter_client`. The one thing `powershell` structurally cannot do, since **state survives between calls**; it also covers plotting/data/symbolic work as plain imports instead of more tool slots. ANSI codes are stripped from tracebacks, inline images are reported but not returned (save to disk instead), and `restart: true` gives a clean namespace. **The ZeroMQ link to the kernel is encrypted** (`_start_encrypted()`): jupyter_client's default is plaintext on four loopback TCP ports carrying every line of code and every result, which is what `ipykernel` warns about on every start ("Kernel is running over TCP without encryption…") — and since the kernel inherits our stderr, that paragraph lands in the middle of the chat. `KernelManager(transport_encryption="required")` provisions a CurveZMQ keypair and passes it to the kernel in the mode-600 connection file (where the HMAC signing key already lives), so both ends talk CURVE. `"required"` rather than `"auto"` deliberately: `auto` provisions nothing and silently runs in the clear when the kernelspec doesn't declare `metadata.supported_encryption`, which is the one case worth hearing about. Three things this depends on, all checked at runtime rather than assumed: `jupyter_client>=8.9.1` (8.9.0 shipped the trait but broke restart, which this module uses), an `ipykernel` whose kernelspec advertises curve support, and a pyzmq built with libsodium. `_start_manager()` is the resulting ladder — **encrypted TCP → plaintext TCP**, the first tier printing why it fell through, with `CLAUDE_KERNEL_ENCRYPTION` to force or skip it. **There is no middle tier**, because ZeroMQ's `ipc://` transport has no Windows implementation — pyzmq raises `Operation not supported` on bind. That makes CurveZMQ load-bearing rather than merely preferred, since it is the only thing standing between the kernel link and four plaintext loopback ports carrying every line of code and every result. One upstream bug to know about, still live in 8.9.1 and worked around in `_new_client()`: `get_connection_info()` `.decode()`s the keypair to `str` while `client()` feeds it into `Bytes` traits, so `manager.client()` on an encrypted kernel raises `TraitError` before a single message moves — the keys are passed back in as bytes via the `**kwargs` override `client()` documents.

- **`core/processes.py`** — `interactive_run`: **pywinpty/ConPTY** on a real pseudo-console for commands that prompt (passwords, `[y/N]`, ssh host keys, winget and other installers, REPLs), which the `powershell` tool cannot answer and simply hangs on. One tool taking a scripted list of expect/send steps rather than a stateful spawn/send/expect trio. pywinpty's `read()` is blocking-only with no timeout, so a reader thread feeds a queue that this module polls against a per-step deadline. Four things worth knowing, all found by reading pywinpty's source rather than by testing:

  - **An empty read is not EOF.** pywinpty substitutes the literal marker `b'0011Ignore'` when the underlying pty read returned nothing, and `read()` turns that back into `''`. Real EOF raises `EOFError`. Treating `''` as EOF made the loop stop at the first idle moment — usually before the awaited prompt appeared — returning `steps_matched: 0` and a truncated transcript with no error, intermittently.
  - **Enter is `"\r"`, not `"\r\n"`.** Writing to a ConPTY is synthesised typing and the console supplies the line terminator; the extra `\n` is a Ctrl-J left in the buffer that the *next* read consumes as an empty line. A one-step script cannot show this.
  - **`close()` as well as `terminate()`**, or two sockets and a thread leak per call; and the exit wait is bounded, because pywinpty's own `wait()` is `while isalive(): sleep(0.1)` with no timeout and would otherwise outlive the `timeout` this tool advertises.
  - **`secret: true` protects the transcript, not the console, and the transport is not private.** ConPTY behaves like a real console from the child's point of view, so the *child* owns its echo setting and the host cannot turn it off; secrets are scrubbed from the buffer as defence in depth rather than suppressed at source. And pywinpty carries pty output over an **unauthenticated loopback TCP socket** whose first accepted connection wins (it opens one in `PtyProcess.__init__`). That is the same exposure class `core/kernel.py` uses CurveZMQ to avoid, on the one tool whose transcripts contain passwords, and it cannot be fixed from here. `secret: true` protects the returned transcript, which is its documented job — not the wire.

- **`core/config_edit.py`** — `config_edit`: round-trip YAML (`ruamel.yaml`), TOML (`tomlkit`), and JSON edits that **preserve comments**, key order, and quoting, which text substitution and stdlib YAML silently destroy. Dotted key paths with `[index]` support, `$…` JSONPath for read-only queries (`jsonpath-ng`), and writes go through a temp file + `os.replace`.

- **`core/data.py`** — `sql_query`: DuckDB against CSV/Parquet/JSON files in place, no import step. One in-memory connection is reused for the session, so views and temp tables persist across calls.

- **`core/files.py`** — `trash`: `send2trash` to the Recycle Bin, the only recoverable delete available here given there is no approval gate. That is stronger on Windows than it was upstream: **`Remove-Item` does not use the Recycle Bin and has no switch to make it**, so the shell's own delete is always permanent. Paths are made absolute (send2trash fails opaquely on relative ones) and `TrashPermissionError` is translated, since it arrives with an empty message — on Windows it means the volume has no Recycle Bin (network shares and most removable drives never do; it can also be disabled per-volume) rather than a permissions problem.

- **`core/output.py`** — `clip(text, limit)`, the one truncation helper the local tool modules share (shell/editor/kernel/ConPTY budget 12000 chars, browser 6000), plus `strip_ansi(text)`, `IMAGE_MEDIA_TYPES` and `image_result(...)`. `strip_ansi` is shared by the two tools whose output arrives as terminal bytes — the IPython kernel's coloured tracebacks and `interactive_run`'s ConPTY transcript — and is deliberately wider than a colour-code pattern, since a console transcript also carries cursor positioning, erase-in-line, private-mode toggles and OSC title sets. The latter builds the `{"__kind__": "image", ...}` marker that a tool returns instead of a string when its result is pixels (file-editor/memory `view` on an image, every computer screenshot); `Chat._local_result_to_content` turns it into a real `image` content block.

- **`core/claude.py`** (`Claude`) — thin Anthropic SDK wrapper. It posts to **`client.beta.messages.create`, not `client.messages.create`**, with `betas=BETAS` — the `computer` tool's schema is beta-gated and `local_tools` declares it on *every* request, so the header is unconditional; omitting it 400s the whole request, not just computer use. The beta endpoint is a superset, but it returns **`BetaMessage`, which is not a subclass of `Message`** — hence `_RESPONSE_TYPES`; an `isinstance(message, Message)` check alone silently stuffs the response object into `content` instead of its blocks. Three more things to know before editing `chat()`: **no sampling parameters** — current models reject a non-default `temperature`/`top_p`/`top_k` with a 400 and only accept the default, so sending one can only fail; and **no `budget_tokens`** — adaptive thinking replaced it (`{"type": "enabled", "budget_tokens": N}` is a 400 now), with `output_config={"effort": ...}` as the depth knob if ever needed. `stop_sequences` defaults to `None` (fixed from a mutable `[]` default — a 2026 ruff `B006` finding) and is only added to `params` when truthy, matching the existing `tools`/`system` pattern. Also open: `max_tokens=8000` is shared by thinking *and* the reply — a `/think` turn on a hard problem can end in `stop_reason: "max_tokens"`; raise it (streaming is advisable much above ~16K). `chat()` builds request params (max_tokens 8000, optional thinking/tools/system); helpers append user/assistant messages and extract text blocks.

- **`core/tools.py`** (`ToolManager`) — the MCP↔Anthropic bridge (remote server tools only). `get_all_tools` aggregates tool schemas across all MCP clients (called once per user turn by `Chat`, not per tool-use iteration); `execute_blocks` executes a list of `tool_use` blocks against the owning client, resolving owners via one `_tool_owners` map per call.

- **`core/cli.py`** (`CliApp`) — a minimal `prompt_toolkit` REPL (history + styling). Delegates all real work to `Chat`. Two commands: `/think <message>`, and **`/clear`** (also `/reset`). `/clear` is the recovery path from the two failures that *persist* for the life of the process — an unanswered `tool_use` block, which stays in `self.messages` and fails every later request however many turns later, and a conversation past the context window. Both present identically ("it started 400ing and won't stop"), and before `/clear` the only way out was killing the app, taking the browser page, the IPython kernel and every MCP connection with it. It deliberately leaves all of those, and `/memories`, alone — none of them is why the history is unusable. `Chat._report_api_failure` prints which failure you hit, checking for orphaned `tool_use` ids directly rather than inferring from the error text; its size report is in **characters, not tokens**, because `count_tokens` cannot measure this conversation at all (`web_search`/`web_fetch` are server tools and that endpoint rejects them).

- **`mcp_client.py`** (`MCPClient`) — async context-manager over an MCP `ClientSession`, supporting three transports: `stdio` (spawn `command`+`args`), `sse` (`url`), and `http` (Streamable HTTP `url`) — the last two accept `headers` for auth (e.g. a Bearer token). Exposes `list_tools`, `call_tool`, `list_prompts`, `get_prompt`, `read_resource`.

- **`mcp_server.py`** — the opposite direction: serves this agent to an MCP client (Claude Code) as a **single `delegate(task, session, thinking)` tool**, so the app is a server and a client at once. Four things decide its shape. **One tool, not 18** — `memory_20250818` and `computer_20251124` are learned schemas, and `computer` needs the `computer-use-2025-11-24` header on the request that *declares* it; that header belongs to this app's own API call, so re-exporting those tools over MCP would strip the trained schema. Wrapping `Chat.run()` keeps them intact and keeps `SYSTEM_PROMPT` in force, which is why the shell/editor overlap with the caller's own tools is deliberate rather than redundant, and why the ~30-50 tool ceiling doesn't apply. **It `chdir`s to the repo root**, because a client spawns it with the client's project as cwd and `CLAUDE_MEMORY_DIR` defaults to a *relative* `memories`. **Calls are serialised behind an `asyncio.Lock`** — one mouse, one browser page, one kernel. One `Chat` is kept per `session` id, so the caller can follow up on a previous delegation; a new id starts clean.

  **The stdout guard must run before any `core/` import**, and it is the hard part of this file — on stdio, fd 1 *is* the JSON-RPC channel, and the app `print()`s to stdout in ~22 places; it therefore `dup`s fd 1 for JSON-RPC and points fd 1 itself at stderr. **On Windows that is only half the job, and the missing half is silent.** Win32 keeps a standard-handle table separate from the C runtime's fd table, and `os.dup2` writes only to the latter — `GetStdHandle(STD_OUTPUT_HANDLE)` still returns the original pipe afterwards. Python's `subprocess` reads exactly that when a child does not redirect (`_get_handles` calls `GetStdHandle` rather than inheriting fd 1), so a child would write straight into the channel the guard believes it has taken away. The IPython kernel is the live case: `jupyter_client` launches it without capturing stdout, the same inheritance that puts ipykernel's startup warning in front of the user. Hence the `SetStdHandle` call alongside the `dup2`; a failure there warns rather than passing quietly, because the symptom — a client desyncing mid-session — points at nothing. The JSON-RPC writer also passes `newline=""`, since a `TextIOWrapper` at the default would translate every `\n` to `os.linesep` and CRLF-terminate every frame on a transport that delimits messages by newline.

  `--transport stdio|streamable-http` (default stdio, `--host` default `127.0.0.1`) drives both from the *same* `Server` object: the low-level MCP `Server.run()` takes only read/write streams, so a transport supplies streams rather than being a different server. **Don't port this to the high-level server** (`FastMCP` in mcp 1.x, renamed `MCPServer` in 2.0) — it would be a downgrade here, because its stdio path calls `stdio_server()` with no arguments and so insists on the real `sys.stdout`, which is precisely the descriptor the stdout guard has to take away; the guard, not the tool registration, is the hard part of this file. Two transport-conditional details: the stdout guard is **stdio-only** (under HTTP fd 1 isn't the wire), and under HTTP `sys.stdout` is set line-buffered because Python block-buffers a redirected stdout, which otherwise swallows the whole startup log — including the `listening on …` line — until the process exits cleanly.

  **HTTP auth is `config.toml`'s `token_env` contract inverted**, deliberately: `--token-env` (default `RESEARCHMESH_MCP_TOKEN`) names the variable holding a bearer token, the token never appears in a file or an argv, and an unset variable means unauthenticated rather than an error — matching `build_client()`'s "no `token_env` connects unauthenticated" and its warn-don't-fail posture when a named variable is empty. The consequence worth keeping: another ResearchMesh can consume this one with an ordinary `{ url = …, token_env = … }` line, no new concepts. `_bearer_auth()` is plain ASGI middleware wrapping the `Mount`, comparing with `hmac.compare_digest`. Note Starlette's router 307-redirects `/mcp` → `/mcp/` *before* the mounted app runs, so a request to the un-slashed path is redirected rather than 401'd — the endpoint itself is still guarded, but don't read a 307 in testing as the guard failing.

  **TLS is `--ssl-certfile`/`--ssl-keyfile`, passed straight through to `uvicorn.Config`**, and it is the serving half only: consuming an `https://` worker has never needed anything, because httpx2 verifies against the OS trust store (`truststore.SSLContext`, with `SSL_CERT_FILE`/`SSL_CERT_DIR` as the per-process override) and `create_mcp_http_client` therefore exposes no `verify` parameter to plumb. That asymmetry is the thing to remember: a company CA or a paid certificate needs work on the *worker*, none on the router. Both flags are required together because uvicorn ignores a lone `--ssl-certfile` and serves plain HTTP — silently doing the opposite of what was asked, so `_run_http()` refuses instead; both paths are also checked to exist before the port opens, since uvicorn's own failure for a missing file is a bare traceback. The startup line's scheme follows the flags rather than being hardcoded, and a plaintext non-loopback bind now says so, alongside the existing unauthenticated warning.

### Key conventions

- **Two parallel tool systems.** MCP tools live on remote servers (any number, listed under `[mcp]`) and are discovered/executed via `ToolManager`. Local tools are declared and executed in-process, aggregated by `local_tools`. `Chat` merges both into one `tools=` list and routes execution by owner.
- **"Learned" vs custom.** The distinction is whether Claude already knows the schema, *not* which file it lives in. `claude_learned_schemas.py` holds the small Anthropic-defined tools (text editor and the two server-side web tools); `memory.py` and `computer.py` are also Anthropic-defined but got their own modules because their implementations are substantial. None of them carry descriptions — Claude is already trained on those schemas, so writing one is at best redundant and at worst contradicts what it was trained on. Every other local module holds fully custom tools Claude learns at runtime from its descriptions, `powershell` now among them. Keep all of them separate from `tools.py`, which is strictly the MCP bridge.
- **A learned tool is exempt from the "must beat the shell" test below** — the schema already exists in the model, so the only question is whether you want the capability, not whether it earns a slot on novelty. The converse is the lesson of this project: **a learned schema is only an asset while its dialect matches the machine.** `bash_20250124` was dropped for exactly that reason — the training that would have made it free is training to emit the wrong shell.
- **A new local tool must beat `powershell` at something structural** — statefulness (`python`), interactivity (`interactive_run`), a correctness guarantee (`config_edit`), recoverability (`trash`), or context economy — since Claude can already shell out to any CLI. Wrapping a command PowerShell could run unaided just spends a tool slot.
- **Tool-selection accuracy degrades past roughly 30–50 loaded tools.** 18 local + whatever the connected MCP servers advertise leaves headroom; prefer one tool with a mode parameter (as `document_convert` and `config_edit` do) over one tool per variation.
- **`web_search` (discovery) and the browser tool (navigate/interact) are complementary**, not redundant — don't reimplement search inside Playwright.
- Add an MCP server by passing its script as argv (stdio) or adding another `MCPClient(...)` in `main.py` (e.g. `transport="http"` for another HTTP server). Its tools then appear to Claude automatically.
- `powershell` is **stateless between calls** (fresh process each time — `cd` and `$env:` changes don't persist; chain with `;`); the `python` kernel, the browser page, and the DuckDB connection **are** stateful within a session.
- **No approval gating** — Claude executes whatever shell commands, file edits, browser actions, conversions, kernel code, interactive commands, and MCP server tools it chooses. This is intended for local dev only, and is why `trash` exists. It is **not** elevated, though: anything needing Administrator fails with an access error rather than prompting, since there is no way to answer a UAC dialog from here.
- The app must run from the **repo root** (`main.py` and `mcp_client.py` live there; `core/` is the importable subpackage).

### Adding or removing a tool

A tool's name and behaviour are described in ~10 places, and nothing enforces agreement
between them. Removing `notify` needed every one of these, and so did introducing
`powershell`; missing any leaves a doc that lies or a prompt that names a tool Claude
doesn't have. Touch them all:

1. The module in `core/` — exposing `TOOLS`, `handles(name)`, and `await execute(name, input)`.
2. `MODULES` in `core/local_tools.py` (the import *and* the list entry).
3. `requirements.txt` and `pyproject.toml`, if it has a third-party dependency.
4. `SYSTEM_PROMPT` in `core/chat.py` — both the explicit roster **and** any tool-choice
   guidance, which is the half no automation can generate.
5. `README.md` — the tool table, the optional-package table, the project layout, and the
   tool count (stated more than once).
6. `CLAUDE.md` — the module bullet in Architecture, the count in Overview, and the count in
   Key conventions.
7. `BETAS` in `core/claude.py`, **only if the tool's schema is beta-gated** (as
   `computer_20251124` is). Export the flag from the tool's own module the way
   `computer.BETA_FLAG` does, so the header and the tool version can't drift apart. This
   one has global blast radius: the header rides every request, and a *missing* one 400s
   the whole conversation rather than just that tool.
8. `README.md`'s environment-variable table and `CLAUDE.md`'s "Runtime configuration", if
   the tool reads any env var of its own.
`main.py` no longer needs touching — its MCP messages don't enumerate tools.

Then verify instead of trusting the list: `grep -rni <toolname>` across `*.py`/`*.md`/
`*.toml`/`*.txt` should come back empty on a removal, and the roster inside `SYSTEM_PROMPT`
should still match `local_tools.TOOLS` exactly. Counting `len(local_tools.TOOLS)` beats
counting by hand.

### Removed from the original tutorial

The tutorial's `mcp_server.py` (the bundled stdio *document* server) and `core/cli_chat.py` (the `@mention` / `/command` document-resource layer) have been **deleted** — that whole `docs://documents` resource/prompt system was tutorial scaffolding and is gone. Don't reintroduce a `doc_client`. **The `mcp_server.py` in the repo today is unrelated** — it is the delegation server described in Architecture above, written from scratch, and shares nothing with the deleted one but the filename.
