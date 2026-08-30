# ResearchMesh-Windows, a Windows CLI Assistant/Research Client for use with Anthropic API, but can be subsidized as an agent for Claude Code/Desktop
> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

> [!NOTE]
> **Windows only.** `main.py` and `mcp_server.py` refuse to start on anything else rather
> than half-working. A fork of [ResearchMesh](https://github.com/nodormu/ResearchMesh) at
> commit `9e6959b`, rewritten for Windows. `ruff`, `mypy` and `smoke_test.py` pass, CI runs
> on `windows-latest`, and it has since been driven hands-on on a real Windows box too.
> `computer`'s biggest caveat — it cannot click into an elevated window — has been
> confirmed directly this way (see **Good to know** below); `document_convert` and
> `interactive_run` are the two still most worth double-checking in your own environment.

                            ┌── /think
						              	├── /clear
                            │
                            ├── PowerShell
                            ├── Filesystem
                            ├── LibreOffice
     ResearchMesh-Windows ──┼── Playwright
                            ├── MCP #1
                            ├── MCP #2
                            ├── MCP #3
                            └── ...

A terminal chat client for the Anthropic API that hands Claude real tools on your own Windows
machine: a shell, a file editor, a headless browser it can surf with, a persistent Python
session, desktop control, and document conversion. Ask it something and it can look it up,
read the pages, run the commands, and hand you back a finished `.docx` — in one conversation.

It works in both directions: it connects out to your own MCP servers, and it can itself be
added to **Claude Code** as one, so Claude Code can hand it the jobs it can't do —
[see below](#mcp-in-both-directions).

## What it can do

**18 local tools**, plus whatever your MCP servers expose:

| Tool | For |
|---|---|
| `powershell` | PowerShell commands as your user. Stateless — fresh process each call |
| `str_replace_based_edit_tool` | View, create, and edit files. Preserves each file's existing line endings |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control of your desktop ([caveats](#good-to-know)) |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` | Headless [Playwright](https://playwright.dev/) — real DOM surfing: renders JavaScript, follows links, fills forms |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, winget and other installers, REPLs |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes to the Recycle Bin. `Remove-Item` bypasses it entirely and has no switch to use it, so this is the only undo you get |

Claude chooses the tools and keeps working until it has an answer.

## Good to know

- **There is no approval prompt.** Claude runs the commands and file edits it decides on, as
  your user, with no y/n in between. Built for local development. `trash` exists so deletes
  are at least recoverable.
- It's your API key: one request can fan out into many tool calls (capped at 30 per turn).
- `powershell` forgets everything between calls — `cd`, `$env:` changes, activated venvs.
  Chain with `;` in one call, or use `python`, which keeps state.
- Ask for files by absolute path. If Claude offers a download link instead, tell it you need
  the file written to disk.
- Network shares and most removable drives have no Recycle Bin, so deletes there would be
  permanent — `trash` says so rather than pretending.
- **`computer` cannot touch an elevated window.** Windows blocks input from a normal process
  into one running as Administrator (UIPI), so if an elevated terminal, a UAC dialog, Task
  Manager or some installer has focus, clicks and keystrokes are silently discarded and the
  screenshot afterwards looks like a click that missed. Typing reports it; clicking cannot.
  If a sequence has no visible effect, suspect this first. Every other tool is unaffected.
  Confirmed directly: an ordinary window (e.g. Notepad) takes clicks and menu navigation
  immediately, while `mmc.exe` (Certificates snap-in, Group Policy, and friends) silently
  swallows every click the moment Windows has quietly elevated it — which it will, even
  from a non-admin shell, with no visible UAC prompt on some machines. If `UAC` is off for
  your account or set to auto-elevate, expect to hit this. Running the whole client
  elevated fixes it for elevated targets, at the obvious cost of also giving it admin.
- **`computer` is primary-monitor only** and assumes the client is DPI-aware, which it sets
  at startup. A second monitor is not captured.
- If Sonnet gets inconsistent on a complicated multi-tool request, set `model` to an Opus one.
- Optional packages are imported only when a tool is used, so a missing one breaks just that
  tool and tells you what to install.
- If a tool reports a missing package that `requirements.txt` already lists (e.g.
  `sql_query`'s `duckdb`, or `config_edit`'s `ruamel.yaml`/`jsonpath-ng`), that's not a docs
  gap — your venv just predates that line. Everything in `requirements.txt` is a `>=` floor
  rather than a pin (there's no lockfile), so a venv can satisfy it and still miss a package
  added later. Re-run `pip install -r requirements.txt`; you don't need to restart the app,
  because each optional package is imported at the moment its tool is called.
- **Linting: one linter is configured, `ruff`, and `ruff check .` should pass.**
  `pyproject.toml` has a `[tool.ruff.lint]` section. It adds no rules — it only switches
  three *off*, each with its reason written next to it, so a clean run is the expected
  baseline and any finding you do see is genuinely new: your own code, or a rule a newer
  ruff added. (The rule selection is left at ruff's defaults, which do shift between
  versions.) Ruff is **not** a dependency and nothing runs it for you — install it yourself
  if you want it. There's no `[tool.black]` and no `.pylintrc`.
- **Type checking: `mypy .` should pass too.** `pyproject.toml` has a `[tool.mypy]` section
  setting exactly one option (`ignore_missing_imports`, because the optional tool backings
  are lazily imported and legitimately absent from a bare venv); strictness stays at mypy's
  defaults, so unannotated function bodies aren't checked. It's worth having here because
  mypy checks against the packages you actually have installed, which makes it the gate that
  catches a dependency changing shape under you — it named every mcp 1.x → 2.x rename in one
  run, including the ones in `core/tools.py` that the smoke test can't reach.
- **`python smoke_test.py` before you commit.** Seconds, no API key, no network, no optional
  packages. It checks that everything imports, that the tool registry is well-formed, that the
  tool count in the docs still matches the code, and that `mcp_server.py` completes an MCP
  handshake. GitHub Actions runs it plus `ruff` and `mypy` on every push and PR to `main`
  (`.github/workflows/ci.yml`), on Python 3.11 and 3.14.
- **There are still no unit tests**, and CI deliberately doesn't exercise the tools themselves
  — that would need LibreOffice, a browser, a real desktop and real API credits. If your venv
  happens to have `pylint`/`black` installed (neither is a project dependency) they're safe to
  run by hand — expect plenty of output, since nothing is configured for them.
- **Two things a linter will fight you on here** — worth knowing before you "fix" them.
  Broad `except Exception`/`except BaseException` is the design, not sloppiness: every local
  tool must catch anything and return an error string rather than crash the chat loop, which
  is why `BLE001` is switched off project-wide. And cleanup paths (`shutdown`, `close`) must
  not be able to fail *or* fail silently — narrowing one has already caused a real bug, since
  `zmq.ZMQError` isn't an `OSError` and escaping `shutdown()` turns an ordinary Ctrl-C into a
  traceback. Blanket catch plus a `print()` is the pattern.
- ** Using it **
  For extended thinking, type. **`/think <message>`** gives Claude longer to reason on hard problems;
  **`/clear`** drops the conversation without restarting the app; **Ctrl-C** exits and shuts everything down cleanly.

## Setup (Windows)

**python 3.14, powershell 7.x and node 24.16 LTS or newer 24.x LTS is required**

### 1) install powershell 7.x and setup linux stuff for windows

- setup powershell 7 as default powershell, do NOT use legacy 5.x, it sucks
- <https://learn.microsoft.com/en-us/powershell/scripting/install/install-powershell-on-windows?view=powershell-7.6>
- if there is a newer version than 7.6, then look it up in a browser for the newest version link rather than what I have above
- add a shortcut for powershell 7 to the taskbar, be SURE you select the correct item from Microsoft's ignorant start/window menu
- if you want to verify yourself, then it probably installed it at: `C:\Program Files\PowerShell\7\pwsh.exe`
- you can go straight to the folder the file is in: `C:\Program Files\PowerShell\7\`, and right click on pwsh.exe, and Pin to start or add to favorites, whichever you like better
- right click and launch python 7 as admin
- type: `wsl-install`
- exit powershell admin session

### 2) setup npm on windows: <https://learn.microsoft.com/en-us/windows/dev-environment/javascript/nodejs-on-windows>

- download npm-setup.zip (scroll past the antivirus links for checking files)
- unzip, and run as regular user.
- check all options
- install npm

### 3) install node

- open powershell as regular user (and to help everyone, you just type what is inside the ` ` slanted quotes)
- type: `npm install 24.16` (or newer LTS only)
- type: `nvm list`
- type: `nvm use` (whatever newer LTS version, revert to 24.16 if you have issues with newer versions. Stay away from bleeding edge. I tested with 24.16)
- type: `node -v`
- type: `npm -v`
- exit powershell

### 4) install python 3.14

- <https://docs.python.org/3/using/windows.html>
- use the microsoft store installer, install version 3.14 newest version
- check all options, if any
- go to settings -> apps -> advanced app settings -> app execution aliases
- enable python python.exe (default) # "in the GUI settings"
- enable python python3.exe (default) # "in the GUI settings"
- enable Python install manager pymanager.exe # "in the GUI settings"
- enable Python install manager py.exe # in the "do you understand now? wth"
- enable Python install manager (windowed) pywmanager.exe # ditto
- enable Python install manager (windowed) pyw.exe # ditto

### 5) setup your Anthropic API key and the following for your windows environment (so you don't have to put the key in ReserachMesh itself)

- If you use Claude Desktop/Claude Code (node cli) and have a subscription, you will also want an alias so it doesn't use your API key
- you will have to create a function in your powershell profile

```powershell
function claude {
    $oldKey = $env:ANTHROPIC_API_KEY
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

    try {
        & claude.exe @args
    }
    finally {
        $env:ANTHROPIC_API_KEY = $oldKey
    }
}
```

- BUT FIRST, check whether you have a profile
- type, as long as you have a user powershell user session open: `$PROFILE`
- then launch your code editor. I use notepad++ because its awesome, windows notepad sucks because of semi-recent changes they have made to it
- the output of '$PROFILE' will produce a path to a file for the profile, but it doesn't mean the folder OR file exists if you have never had to have it before
- if you don't have that folder and file from the output of '$PROFILE', create the folder/file if needed, then open up notepad++, and put following in it.
- You may have to chatgpt/claude/glm if you can't get it to work with your environment, as i don't how your environment is setup
- if you do have that profile file, then open it up in notepad++ and add this to the very bottom, you can put notes above it
- If you don't have a claude code subscription, then don't add the alias function.
- type or copy/paste the following at the bottom of the profile file:

```powershell
# ============================================================
# Anthropic / Claude Configuration
# ============================================================
#
# ANTHROPIC_API_KEY
#   Available to third-party agents and other applications
#   that use the Anthropic API.
#
# CLAUDE_KERNEL_ENCRYPTION
#   Requires Claude Kernel encryption.
#
# Claude Code
#   The `claude` function below temporarily removes
#   ANTHROPIC_API_KEY from Claude Code's environment.
#
#   This mirrors the Linux setup:
#
#       alias claude='env -u ANTHROPIC_API_KEY claude'
#
#   The API key is restored to this PowerShell session
#   after Claude Code exits.
# ============================================================


# Make the Anthropic API key available to programs launched
# from this PowerShell session.
$env:ANTHROPIC_API_KEY = "YOUR_API_KEY_HERE"

# Require Claude Kernel encryption.
$env:CLAUDE_KERNEL_ENCRYPTION = "required"


# ============================================================
# Claude Code launcher
# ============================================================

function claude {
    # Save the API key currently in this PowerShell session.
    $savedKey = $env:ANTHROPIC_API_KEY

    # Remove it from Claude Code's environment.
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

    try {
        # Start Claude Code and pass through any arguments.
        & claude.exe @args
    }
    finally {
        # Restore the API key after Claude Code exits.
        if ($null -ne $savedKey) {
            $env:ANTHROPIC_API_KEY = $savedKey
        }
    }
}
```

- stop copy/pasting above this line after the last '}'. save the file at the path/filename that was output at '$PROFILE' showed if it didn't already exist and you are editting the existing file, then just save it

### 6) open powershell as regular user, check to make sure you launched powershell 7 by typing: `$PSVersionTable.PSVersion`

- create python sandbox, type: `python -m venv pvenv`
- type: `C:\Users\YOUR USER NAME\Scripts\Activate.ps1`
- change directory to wherever you want to keep the clone of ReserachMesh-Windows
- type: `git clone -h` # to see all the git clone options if you did a full install of git on your windows box, not covered in this repo
- type: `git clone https://github.com/nodormu/ResearchMesh-Windows`
- type: `cd ResearchMesh-Windows`
- type: `pip install -r requirements` # hopefully you don't get any errors, conflicts or wheel issues, if so then just chatgpt/claude/glm that issue for a fix, 
	just be careful about it leading you down rabbit holes of "you must have done this", or "or lets check for sure" etc etc etc and try again. 
	AIs will feed you BS. Be VERY specific with memories and context before ANY prompt/request.
- type: `playwright install chromium`

### 6) Install remaining deps while in powershell as regular user

```powershell
winget install TheDocumentFoundation.LibreOffice   # document_convert
winget install JohnMacFarlane.Pandoc              # document_convert, markdown path
```

### 7) exit powershell in case and restart as regular user just to make it easier instead of establishing environment variables at the CLI

- type: `cd C:\Users\YOUR USER NAME\path\to\ResearchMesh-Windows`
- type: `C:\Users\YOUR USERNAME\pvenv\Scripts\Activate.ps1` # or whatever your python sandbox and $PROFILE file name is if its not Activate.ps1
- to start the CLI Assistant, type: `python .\main.py`
- to start the MCP server, type: `python .\mcp_server.py`
- if you want to utilize ResearchMesh as an MCP Client for usage with the CLI assistant AND/OR MCP server, edit the config.toml for your environment and run either or both above commands and see if you can see the tools in your connected MCP servers. You can have 2 different powershell sessions open so you can run ResearchMesh as an MCP and use the CLI assistant at the same time.
- if you want to test connection from ResearchMesh-Windows to any MCP servers, then type: `python .\mcp_client` but only if you have node installed for the USER, NOT admin, otherwise it won't work. Besides, what human in their right mind would give control of their Windows Desktop/Workstation over to an AI? Just sayin.

### 8) Test it.

Now you have a second "you" on your computer as your user. Think about it. Do you ask yourself for approval when you need to open a document or surf to a website? No, you just do it. That's the whole point of ResearchMesh. Its YOU on your computer, allowing you to talk naturally to it while it handles the technical details, however; proper context with prompts can help AI responses zero in on your request. You can change the Anthropic model in the config.toml file also. Have fun. Be safe with this.

### 9) Important:

If it can't do something controlling your mouse and keyboard, it can probably do it with powershell if your user in powershell can do it. UAC may cause you headache getting this to function, so if you have UAC on, it's not much help for you as a project to use a duplicate you. Also, Windows UIPI wil invisibly block synthetic input from a Medium-integrity source into a High-integrity window.

## Try it

```
Run Get-ComputerInfo and tell me what build of Windows I'm on.

What's the latest stable Python release? Cite your source.

Open news.ycombinator.com, list the top links, then open the first one and summarize it.

Run some common read commands about my Windows system through Powershell 7.x and tell me the results.

Write a one-page summary of the Raft consensus algorithm as markdown,
then convert it to a .docx in my Documents folder.

Give me a Cisco IOS 17.15 config for a 9200 24-port switch: VTP client so my VLAN
database isn't overwritten, two uplinks active/standby at 1 Gbps, all 24 ports up and
ready for voice + data VLANs pushed from the VLAN server, uplink trunk on VLAN 100.
Note what I need to change for my environment, then write it to my Desktop.

What is the airspeed velocity of an unladen swallow?
```

## Configuration

Non-secret settings live in `config.toml`. Secrets stay in the environment — the app does
**not** read a `.env` file.

```toml
[claude]
model = "claude-sonnet-5"   # CLAUDE_MODEL overrides this

[mcp]
enabled = true              # false skips every server; local tools still work

# One line per server. Add as many as you like — every reachable/launchable one
# connects and its tools join the same list Claude sees. Two entry shapes:
#
#   Streamable HTTP (a server already running elsewhere):
#     url        the server's endpoint
#     token_env  names the environment variable holding that server's bearer
#                token; omit it if the server needs none
#
#   stdio (a local server main.py launches itself, no separate process to start
#   by hand — it talks JSON-RPC over the subprocess's stdin/stdout):
#     command    full argv as a list, e.g. ["node", "C:/path/to/bin.js"]
#     env        optional table of extra environment variables for it
servers = [
  { name = "n8n",    url = "http://192.168.2.12:5678/mcp-server/http", token_env = "N8N_MCP_TOKEN" },
  { name = "alpaca", url = "http://192.168.2.12:8000/mcp" },
  { name = "unreal", command = ["node", "%USERPROFILE%/unreal-mcp/dist/bin.js"] },
]
```

A server that's unreachable (http) or fails to launch (stdio) prints a warning and is
skipped, so one being down doesn't stop the app. Tokens are never written in this file —
only the *name* of the variable that holds them.

`~`, `%USERPROFILE%`, `%USERNAME%` and any other `%VAR%` are expanded in `command`, `url` and
the *values* of `env`, so the checked-in config doesn't have to name your user account.
`$VAR` and `${VAR}` work too, but use the Windows **names**: there is no `$HOME` or `$USER`
here, and a path copied from a config written for another OS passes through unexpanded.
(`env`'s keys are variable names and are left alone.) An undefined variable is left as
written rather than expanding to nothing, so a typo shows up in the startup warning instead
of becoming a silently wrong path.

Backslashes need care in TOML: `"C:\Users\me"` is invalid, because `\` starts an escape
inside a basic string. Use forward slashes (every Windows API accepts them) or a literal
single-quoted string, `'C:\Users\me'`. Absolute paths beyond that are machine-specific —
those you edit by hand.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required |
| *(per server)* | Whatever each `token_env` names, e.g. `N8N_MCP_TOKEN` |
| `RESEARCHMESH_MCP_TOKEN` | Bearer token clients must present to `mcp_server.py --transport streamable-http`; unset = no auth |
| `CLAUDE_MODEL` | Override the model |
| `CLAUDE_SHOW_USAGE=1` | Print token and prompt-cache counts per request |
| `CLAUDE_MEMORY_DIR` | Where `memory` stores `/memories` (default `./memories`) |
| `CLAUDE_DISPLAY_SIZE` | Logical screen size `computer` reports, e.g. `1280x800` |
| `CLAUDE_KERNEL_ENCRYPTION` | `auto` (default) encrypts the `python` kernel's sockets with CurveZMQ and falls back if it can't; `required` fails the tool instead of running unencrypted; `off` skips it |

## MCP, in both directions

ResearchMesh is a client and a server at the same time. The two are independent — use either,
both, or neither:

```
   Claude Code  ──delegate──▶  ResearchMesh  ──▶  n8n / Unreal / Unity / …
   (any MCP client)            (server AND client)     (its own MCP servers)
        │                            │                          │
     mcp_server.py            18 local tools           [mcp] in config.toml
```

**As a client**, it connects out to MCP servers and merges their tools with its own — that's
`[mcp]` in [Configuration](#configuration) above. **As a server**, it hands another client the
whole agent as one `delegate` tool, so Claude Code can offload what it structurally can't do
itself: drive GUI apps, answer password / `[y/N]` prompts, keep a live Python kernel between
steps, surf a real DOM, and reach ResearchMesh's own MCP servers.

### Add it to Claude Code

```powershell
claude mcp add researchmesh --scope user `
  --env ANTHROPIC_API_KEY="$env:ANTHROPIC_API_KEY" `
  -- "$HOME\researchmesh\Scripts\python.exe" C:\path\to\ResearchMesh-Windows\mcp_server.py
```

That's it — no token, no ports, nothing to start. Claude Code launches the server itself when
it needs it. Then just ask it to delegate something: *"use researchmesh to take a screenshot
and tell me what window is focused."*

Two ways it fails, both at the first call:

- **`ANTHROPIC_API_KEY` not set** — a client passes stdio servers only a small safe subset of
  the environment, so exporting it in your shell isn't enough. That's what `--env` above is
  for. The server says so at startup rather than failing cryptically later.
- **Wrong python** — use the venv interpreter that has the dependencies, not bare `python`.
  The client spawns this with no `PATH` of yours and no activated venv.

A `.mcp.json` ships in the repo as a working equivalent if you'd rather commit the config than
run the command.

<details>
<summary><b>Streamable HTTP</b> — for clients that connect to an already-running endpoint</summary>

stdio (above) is right whenever the client launches its own server — Claude Code, Claude
Desktop, most editors. Use HTTP instead to share one agent between several clients, or for a
client that only speaks HTTP:

```powershell
python mcp_server.py --transport streamable-http --port 8765
# point the client at http://127.0.0.1:8765/mcp
```

`--host` defaults to **127.0.0.1**, reachable only from this machine. `--path`, `--port` and
`--json-response` are there too (`--json-response` returns one JSON body instead of an SSE
stream).

**Auth is the `token_env` arrangement from `config.toml`, pointed the other way.** Set the
variable and it's required; leave it unset and the endpoint is unauthenticated, which is
allowed by design and announced at startup:

```powershell
$env:RESEARCHMESH_MCP_TOKEN = "<token>"        # see Tokens below
python mcp_server.py --transport streamable-http --host 0.0.0.0
```

Clients send `Authorization: Bearer <token>` — exactly what a `token_env` entry produces, so
another ResearchMesh consumes this one with a plain `config.toml` line. Same token, same
variable name, set on both machines:

```toml
{ name = "desktop", url = "http://192.168.2.5:8765/mcp", token_env = "RESEARCHMESH_MCP_TOKEN" }
```

Unauthenticated *and* bound off-loopback prints a warning, because at that point anyone who
can reach the port has unrestricted shell and desktop control of the machine. The token is
read from the environment, never passed as an argument, so it stays out of `ps` and shell
history. `--token-env VAR` renames the variable.

**TLS is a pair of paths, not a mode.** Without them the endpoint is plain HTTP — the bearer
token and every task and result cross the network in the clear, which is called out at startup
on a non-loopback bind:

```powershell
python mcp_server.py --transport streamable-http --host 0.0.0.0 `
    --ssl-certfile C:\certs\worker-fullchain.pem `
    --ssl-keyfile  C:\certs\worker.key
```

The startup line then says `https://`. Give `--ssl-certfile` the **full chain** — leaf first,
then intermediates — which is what a company CA or a public issuer hands you; a leaf-only file
verifies on the box that has the intermediate cached and fails everywhere else. The two must be
given together (uvicorn quietly serves plain HTTP with only one, so this refuses instead), and
both paths are checked to exist before the port opens.

Nothing is configured on the client side to match: the URL becomes `https://…` and verification
goes through the connecting machine's own OS trust store, so a company CA already rolled out to
that machine is trusted, as is any public certificate. `$env:SSL_CERT_FILE` overrides that per
process if you'd rather not import a CA into the machine store.

Both transports are the same server object — no separate build, no high-level-server rewrite. Under HTTP
the stdout guard is skipped (fd 1 isn't the wire there) so the app's messages become ordinary
service logs, line-buffered so a redirected log fills in live rather than on exit. Running the
stdio form by hand just waits on stdin, which is a healthy stdio server behaving normally.

</details>

<details>
<summary><b>Tokens</b> — generating one, and where it actually has to live</summary>

**Only needed for `--transport streamable-http`.** Under stdio there's no port and nothing to
authenticate.

Generate one with the interpreter this project already requires — no `openssl` needed:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

256 bits from the OS CSPRNG. There's deliberately no `generate_token.py` here: a file wrapping
one line of stdlib would be the same mistake as a tool wrapping a command PowerShell could already
run.

The value lives in an environment variable; only its *name* goes in a file. Which file depends
on how the process starts, and this is the part that catches people:

| How it starts | Where the token has to be |
|---|---|
| You, from an interactive shell | `setx RESEARCHMESH_MCP_TOKEN …` (persists; new shells only) plus `$env:RESEARCHMESH_MCP_TOKEN = …` for the current one |
| A Windows service or Scheduled Task | Set it machine-wide or in the service's own environment — neither inherits your interactive shell |
| Spawned by an MCP client | the `env` block of that server's entry in the client config |

Two things to get right:

- **Never put the literal token in a committed file.** `.mcp.json` and `config.toml` are both
  in git — use `${RESEARCHMESH_MCP_TOKEN}` and `token_env` respectively.
- **One name is normally right.** It's one token, and each end reads the variable from its own
  environment, so both machines can call it `RESEARCHMESH_MCP_TOKEN`. You only need a second
  name if a *single* machine both serves an endpoint and consumes someone else's — then one
  variable would have to mean two different secrets. Rename either end with `--token-env VAR`
  or `token_env = "VAR"`.

**Can you just ask ResearchMesh to set it up?** Mostly. It can generate the token, persist it
with `setx`, and update a consuming `config.toml`. It *cannot* set the variable in the shell
you are sitting in — the `powershell` tool is a fresh process per call, and a child cannot
alter its parent's environment anyway — so you still need a new shell and a server restart.
Tell it not to write the literal token into anything in the repo.

</details>

<a id="full-setup-detail"></a>

<details>
<summary><b>Full setup detail</b> — OS libraries, document tools, which package backs which tool</summary>

**Playwright.** `pip` installs the Python package but not the browser itself:

```powershell
playwright install chromium            # the browser binary
```

`playwright install` with no browser name fetches all three engines; this app only launches
Chromium, so the argument is worth keeping. There is no `install-deps` step — that installs
shared libraries for other operating systems and does not apply here.

**Document conversion.** `soffice` (LibreOffice) handles docx/odt/xlsx/pptx/html/rtf/txt and
PDF output, each call in a throwaway user profile so two conversions can't collide on the
profile lock. `pandoc` handles markdown, because `soffice` has no dependable markdown
import; `md → pdf` goes through odt on the way, since pandoc's own PDF writer would need a
LaTeX engine. `libreoffice-writer`/`-calc`/`-impress` alone are enough if you don't want the
whole suite.

**Computer use needs no system packages.** `pyautogui` and `pillow` from
`requirements.txt` are the whole dependency and screen capture works out of the box.

Two limits worth knowing before you rely on it:

- **Elevated windows are unreachable.** Windows blocks input from a normal-integrity process
  into a window owned by an elevated one (User Interface Privilege Isolation). If an
  Administrator terminal, a UAC consent dialog, Task Manager or an installer has focus,
  clicks and keystrokes are discarded — and the screenshot afterwards looks exactly like a
  click that missed. `type` detects it (`SendInput` reports how many events landed) and says
  so; the mouse actions get no such signal from Windows and fail silently. Running the whole
  client elevated fixes it for elevated targets, at the obvious cost.
- **Primary monitor only.** Capture is deliberately limited to the primary display: the
  coordinate maths scales against that screen's size, so grabbing the whole virtual desktop
  would put every click on the wrong monitor. Proper multi-monitor support needs the virtual
  desktop's bounds *and* origin, which can be negative.

The client declares per-monitor DPI awareness at startup. Without it Windows reports
virtualised coordinates on a scaled display while screenshots come back at physical
resolution, and every click drifts further off toward the bottom-right.

The tool reports a fixed logical screen size (`CLAUDE_DISPLAY_SIZE`, default `1280x800`)
and downscales every screenshot to exactly that, scaling Claude's coordinates back up to
your real resolution. That's what keeps clicks landing where Claude aims — the declared
size and the image it sees can never drift apart. Below roughly `1280x720`, accuracy drops.

**Memory** writes to `./memories` by default (`CLAUDE_MEMORY_DIR` to relocate). Claude sees
it as `/memories`; every command is confined to that directory, so a traversal path like
`/memories/../../.ssh/id_rsa` is rejected rather than served. It's a private scratchpad for
Claude, not a place for your project files — and it persists until you delete it.

**Optional Python packages** (all in `requirements.txt`; each is imported lazily):

| Tool | Needs |
|---|---|
| `python` | `jupyter_client>=8.9.1`, `ipykernel>=7` — older versions work, but unencrypted (see below) |
| `interactive_run` | `pywinpty` (ConPTY) |
| `config_edit` | `ruamel.yaml` (YAML), `tomlkit` (TOML), `jsonpath-ng` (`$…` queries); JSON needs nothing |
| `sql_query` | `duckdb` |
| `trash` | `send2trash` |
| `computer` | `pyautogui`, `pillow` |
| `memory` | nothing — standard library only |

To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py`.

**The `python` kernel's sockets are encrypted.** Everything that tool does — your code, your
data, the results — travels over ZeroMQ, which by default is plaintext on four loopback TCP
ports; `ipykernel` says so itself, warning on every start that the link "is susceptible to
eavesdropping". ResearchMesh has the kernel manager provision a CurveZMQ keypair instead, so
both ends talk CURVE. That needs `jupyter_client>=8.9.1` and `ipykernel>=7` (and a pyzmq built
with libsodium, which the wheels are); on anything older it falls back to plaintext TCP,
printing why. There is no middle tier — ZeroMQ's `ipc://` transport has no Windows
implementation — so CurveZMQ is the only thing between that link and an open loopback port. Set `CLAUDE_KERNEL_ENCRYPTION=required` to make an unencrypted kernel a hard error
rather than a fallback — if you see that error, `pip install -U 'jupyter_client>=8.9.1'
'ipykernel>=7'` is the fix.

**Environment variables** must be set for the user account you launch as — `main.py` calls
`os.getenv()` directly. `setx VAR "value"` writes them to the user environment but only
affects shells started *afterwards*, so set `$env:VAR = "value"` as well for the shell you are
in. Then check without revealing anything:

```powershell
# Prints True/False without echoing the value.
[bool]$env:ANTHROPIC_API_KEY, [bool]$env:N8N_MCP_TOKEN
```

**Targets** Windows 10/11 and Python 3.11+ (`pyproject.toml`; the floor is `tomllib`, used by
`main.py`). CI runs 3.11 and 3.14 on `windows-latest`.

</details>

<details>
<summary><b>HTTPS and TLS</b> — for an MCP server with a self-signed or private-CA certificate</summary>

A server URL may be `http://` or `https://`. TLS is verified by the `httpx2` client inside
`mcp_client.py`, offline — the CA is not contacted at connect time.

**Verification goes through the Windows certificate store.** `httpx2` builds its default
context with `truststore.SSLContext`, which defers to the OS rather than to a bundled
`certifi` list. So a publicly-signed certificate (Let's Encrypt, DigiCert, …) works with no
configuration, and the fix for an internal CA is the ordinary Windows one — import it once
and every tool on the machine trusts it:

```powershell
# Machine-wide (needs an elevated shell); use Cert:\CurrentUser\Root for just you.
Import-Certificate -FilePath C:\certs\your-ca.crt -CertStoreLocation Cert:\LocalMachine\Root
```

If you would rather scope it to this process only, `SSL_CERT_FILE` still overrides:

```powershell
$env:SSL_CERT_FILE = "C:\certs\your-ca-chain.pem"   # or SSL_CERT_DIR for a hashed dir
```

Two things that catch people out:

- `SSL_CERT_FILE` **replaces** the trust store rather than adding to it, so a process using it
  loses the Windows store — including every public CA. Importing into `Cert:\LocalMachine\Root`
  avoids that problem entirely, which is why it is the first suggestion above.
- Your server (or its reverse proxy) must present its **full chain**. A missing
  intermediate is the most common "the cert is valid but it still won't connect" cause, and
  the fix is on the server — the client only needs the root.

</details>

<details>
<summary><b>Project layout and extending</b></summary>

```
main.py                          entrypoint — connects the MCP servers, wires Chat + REPL
mcp_client.py                    MCP client (stdio / SSE / Streamable HTTP)
mcp_server.py                    the other direction — serve this agent to an MCP client
.mcp.json                        example Claude Code registration for mcp_server.py
smoke_test.py                    fast wiring checks — no API key, no network
.github/workflows/ci.yml         runs ruff + smoke_test.py on push and PR
config.toml                      model + MCP server list (no secrets; committed)
pyproject.toml                   metadata, deps, and the ruff exemptions (lint config)
requirements.txt                 the same deps, for `pip install -r`
CLAUDE.md                        architecture + conventions, for AI coding agents
core/
  chat.py                        agentic loop, tool routing, SYSTEM_PROMPT
  claude.py                      Anthropic SDK wrapper
  local_tools.py                 registry of every locally-executed tool
  tools.py                       MCP <-> Anthropic bridge
  claude_learned_schemas.py      file editor, web_search, web_fetch
  memory.py                      /memories store, persists across sessions
  computer.py                    screenshots + mouse/keyboard
  browser.py                     Playwright DOM surfing
  documents.py                   LibreOffice / pandoc conversion
  kernel.py                      persistent IPython kernel
  processes.py                   ConPTY — commands that prompt
  config_edit.py                 comment-preserving YAML/TOML/JSON edits
  data.py                        DuckDB queries
  files.py                       recoverable deletes
  output.py                      shared output trimming + image results
  cli.py                         prompt_toolkit REPL
```

- **Add an MCP server:** add an entry under `[mcp].servers` in `config.toml` — see
  "Configuration" above for both entry shapes (`url` for Streamable HTTP, `command` for a
  local stdio server main.py launches itself). Its tools appear to Claude automatically once
  it connects. A one-off Python stdio script can also be passed as an argument instead
  (`python main.py path/to/server.py`) without touching config.toml.
- **Add a local tool:** write a module exposing `TOOLS`, `handles(name)`, and
  `async execute(name, tool_input)`, then add it to `MODULES` in `core/local_tools.py`.
  That's the only registration step. Update `SYSTEM_PROMPT` in `core/chat.py` too — it
  describes the tool set to Claude.
- **Keep the list lean.** Tool-selection accuracy degrades past roughly 30–50 tools, so prefer
  one tool with a mode parameter over several near-duplicates, and don't wrap a command
  PowerShell could already run.

Check every configured server on its own with `python mcp_client.py` — it connects to each
in turn, lists its tools, and reports failures without starting the chat.

</details>

<a id="inspecting-an-mcp-server"></a>

<details>
<summary><b>Inspecting an MCP server</b> — <code>mcp_client.py</code></summary>

`mcp_client.py` is the inspector for this project. It connects to your servers, lists what
they expose, and calls a tool — no install, no Node, no browser tab:

```powershell
python mcp_client.py                          # every configured server: connect, list tools
python mcp_client.py -s unreal --schema       # one server, with each tool's input schema
python mcp_client.py --prompts --resources    # also list prompts and resources
python mcp_client.py -s n8n --call list_flows --args '{"limit": 5}'
python mcp_client.py --url http://host:8000/mcp --token-env N8N_MCP_TOKEN
```

**It builds each client through the same `config.toml` and the same `build_client()` the app
uses**, which is the reason to prefer it over the generic
[MCP Inspector](https://github.com/modelcontextprotocol/inspector). That one is a Node package
run through `npx`, and it asks you to retype each server's address and token into a browser —
so what it tests is not what the app is configured to do. This has already mattered here: an
earlier version of this script built its own clients and forced HTTP on every entry, which
made it report a stdio server as unreachable while `python main.py` was talking to it happily.

`--token-env` names the variable holding the bearer token rather than taking the token, so it
stays out of your shell history. A server that implements no prompts or resources says so
rather than looking broken.

Two things Node *is* still involved in, neither of which you install: **Playwright bundles its
own runtime** — `playwright/driver/node.exe`, about 92 MB, launched against
`driver/package/cli.js` — because its Python package is a client for a JavaScript driver. It
is vendored inside the pip package, is not on your PATH, and `PLAYWRIGHT_NODEJS_PATH`
overrides it. And a `command = ["node", …]` entry in `config.toml` runs whatever MCP server
*you* point it at; that one is your dependency, not this project's.

</details>

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
