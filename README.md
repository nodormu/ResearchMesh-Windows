# ResearchMesh-Windows, a Windows CLI Research Client for Claude

> *Unofficial, community-built client — not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.*

> [!WARNING]
> **Port in progress: `core/` is Windows-only now, but this README is not yet rewritten.**
> A fork of [ResearchMesh](https://github.com/nodormu/ResearchMesh) at commit `9e6959b`.
> Everything below still describes the Linux original and now contradicts the code in
> several places — most importantly, **there is no `bash` tool any more**; `powershell`
> replaces it. The setup steps below are still `apt`, and the `computer`/`interactive_run`
> sections still describe X11 and `pexpect`. The `core/` module docstrings are accurate;
> this file is not. Nothing has been run on Windows yet.

                    ┌── /think
                    │
                    ├── Bash / Linux
                    ├── Filesystem
                    ├── LibreOffice
     ResearchMesh ──┼── Playwright
                    ├── MCP #1
                    ├── MCP #2
                    ├── MCP #3
                    └── ...

A terminal chat client for the Anthropic API that hands Claude real tools on your own Linux
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
| `bash` | Shell commands as your user. Stateless — fresh subprocess each call |
| `str_replace_based_edit_tool` | View, create, and edit files |
| `web_search` · `web_fetch` | Anthropic's server-side search and page fetch |
| `memory` | A `/memories` store that **persists across sessions** — the only state that outlives the process |
| `computer` | Screenshots plus mouse/keyboard control of your desktop. **Needs an X11 session** ([see below](#full-setup-detail)) |
| `browser_navigate` · `_links` · `_click` · `_fill` · `_extract` · `_back` | Headless [Playwright](https://playwright.dev/) — real DOM surfing: renders JavaScript, follows links, fills forms |
| `document_convert` | LibreOffice + pandoc. Markdown → `.docx`/`.odt`/`.pdf`, or any office format to any other |
| `python` | Persistent IPython kernel — **variables survive between calls** |
| `interactive_run` | Commands that prompt: passwords, `[y/N]`, ssh host keys, installers, REPLs |
| `config_edit` | Edit YAML/TOML/JSON **without destroying your comments** |
| `sql_query` | DuckDB straight against CSV/Parquet/JSON — no import step |
| `trash` | Recoverable deletes instead of `rm` |

Claude chooses the tools and keeps working until it has an answer.

## Quick start

You need **Linux**, **Python 3.11+**, and an Anthropic **API key** — this is an API client,
so a Claude subscription won't work.

**MCP servers are optional.** The `[mcp]` block in `config.toml` ships with
`enabled = false` and every server commented out, so a fresh clone runs on the 18
local tools alone. The commented entries are kept as worked examples of both entry
shapes — the addresses and paths in them are machine-specific, so replace them with
your own before uncommenting and setting `enabled = true`.

mcp_client.py is just a script to connect to your MCP server and pull a list of tools, be sure you change the IP address in the code.

```bash
sudo apt install python3 python3-venv python3-dev build-essential \
                 libreoffice pandoc python3-tk scrot

python3 -m venv ~/claude-chat-plus-more-tools
source ~/claude-chat-plus-more-tools/bin/activate
pip install -r requirements.txt

playwright install chromium           # pip installs the package, not the browser
sudo playwright install-deps chromium

export ANTHROPIC_API_KEY=sk-ant-...   # add to ~/.bashrc to keep it, and put your N8N API key in .bashrc as well, or you will have to rewrite code to make it elsewhere if not exporting it before runnning main.py

python main.py
```

Then just type. **`/think <message>`** gives Claude longer to reason on hard problems;
**`/clear`** drops the conversation without restarting the app; **Ctrl-C** exits and
shuts everything down cleanly.

**If it starts returning 400s and won't stop, run `/clear`.** Two failures persist for
the life of the process — an unanswered `tool_use` block, and a conversation past the
context window — and both make every later turn fail the same way. The error report
names which one you hit. `/clear` recovers from either while keeping the browser page,
the kernel, your MCP connections and `/memories`.

**MCP servers are optional** — all 18 local tools work without any of them.

## Try it

```
Run uname -a and tell me what kernel I'm on.

What's the latest stable Python release? Cite your source.

Open news.ycombinator.com, list the top links, then open the first one and summarise it.

Load ~/data.csv and show me the five biggest rows by revenue.

Write a one-page summary of the Raft consensus algorithm as markdown,
then convert it to a .docx in ~/Documents.

Give me a Cisco IOS 17.15 config for a 9200 24-port switch: VTP client so my VLAN
database isn't overwritten, two uplinks active/standby at 1 Gbps, all 24 ports up and
ready for voice + data VLANs pushed from the VLAN server, uplink trunk on VLAN 100.
Note what I need to change for my environment, then write it to /tmp/switch.txt.

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
#     command    full argv as a list, e.g. ["node", "/path/to/bin.js"]
#     env        optional table of extra environment variables for it
servers = [
  { name = "n8n",    url = "http://192.168.2.12:5678/mcp-server/http", token_env = "N8N_MCP_TOKEN" },
  { name = "alpaca", url = "http://192.168.2.12:8000/mcp" },
  { name = "unreal", command = ["node", "$HOME/unreal-mcp/dist/bin.js"] },
]
```

A server that's unreachable (http) or fails to launch (stdio) prints a warning and is
skipped, so one being down doesn't stop the app. Tokens are never written in this file —
only the *name* of the variable that holds them.

`~`, `$USER`, `$HOME` and `${ANY_VAR}` are expanded in `command`, `url` and the *values* of
`env`, so the checked-in config doesn't have to name your home directory or mount point.
(`env`'s keys are variable names and are left alone.) An undefined variable is left as
written rather than expanding to nothing, so a typo shows up in the startup warning instead
of becoming a silently wrong path. Absolute paths beyond that are still machine-specific —
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
| `CLAUDE_COMPUTER_FORCE=1` | Let `computer` try anyway on a Wayland session |
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

```bash
claude mcp add researchmesh --scope user \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -- "$HOME/tif-env/bin/python" /path/to/ResearchMesh/mcp_server.py
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

```bash
python mcp_server.py --transport streamable-http --port 8765
# point the client at http://127.0.0.1:8765/mcp
```

`--host` defaults to **127.0.0.1**, reachable only from this machine. `--path`, `--port` and
`--json-response` are there too (`--json-response` returns one JSON body instead of an SSE
stream).

**Auth is the `token_env` arrangement from `config.toml`, pointed the other way.** Set the
variable and it's required; leave it unset and the endpoint is unauthenticated, which is
allowed by design and announced at startup:

```bash
export RESEARCHMESH_MCP_TOKEN=<token>          # see Tokens below
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

```bash
python mcp_server.py --transport streamable-http --host 0.0.0.0 \
    --ssl-certfile /etc/ssl/certs/worker-fullchain.pem \
    --ssl-keyfile  /etc/ssl/private/worker.key
```

The startup line then says `https://`. Give `--ssl-certfile` the **full chain** — leaf first,
then intermediates — which is what a company CA or a public issuer hands you; a leaf-only file
verifies on the box that has the intermediate cached and fails everywhere else. The two must be
given together (uvicorn quietly serves plain HTTP with only one, so this refuses instead), and
both paths are checked to exist before the port opens.

Nothing is configured on the client side to match: the URL becomes `https://…` and verification
goes through the connecting machine's own OS trust store, so a company CA already rolled out to
that machine is trusted, as is any public certificate. `SSL_CERT_FILE=/path/ca.pem` overrides
that per process if you'd rather not install a CA system-wide.

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

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

256 bits from the OS CSPRNG. There's deliberately no `generate_token.py` here: a file wrapping
one line of stdlib would be the same mistake as a tool wrapping a command `bash` could already
run.

The value lives in an environment variable; only its *name* goes in a file. Which file depends
on how the process starts, and this is the part that catches people:

| How it starts | Where the token has to be |
|---|---|
| You, from an interactive shell | `export RESEARCHMESH_MCP_TOKEN=…` in `~/.bashrc` |
| `systemd` unit | `EnvironmentFile=` — a unit does **not** read `~/.bashrc` |
| Spawned by an MCP client | the `env` block of that server's entry in the client config |

Two things to get right:

- **Never put the literal token in a committed file.** `.mcp.json` and `config.toml` are both
  in git — use `${RESEARCHMESH_MCP_TOKEN}` and `token_env` respectively.
- **One name is normally right.** It's one token, and each end reads the variable from its own
  environment, so both machines can call it `RESEARCHMESH_MCP_TOKEN`. You only need a second
  name if a *single* machine both serves an endpoint and consumes someone else's — then one
  variable would have to mean two different secrets. Rename either end with `--token-env VAR`
  or `token_env = "VAR"`.

**Can you just ask ResearchMesh to set it up?** Mostly. It can generate the token, append the
export to `~/.bashrc`, write a systemd `EnvironmentFile`, and update a consuming
`config.toml`. It *cannot* set the variable in your shell — the `bash` tool is a fresh
subprocess per call, and a child can't alter its parent's environment anyway — so you still
need a new shell (or `source ~/.bashrc`) and a server restart. Tell it not to write the
literal token into anything in the repo.

</details>

## Good to know

- **There is no approval prompt.** Claude runs the commands and file edits it decides on, as
  your user, with no y/n in between. Built for local development. `trash` exists so deletes
  are at least recoverable.
- It's your API key: one request can fan out into many tool calls (capped at 30 per turn).
- `bash` forgets everything between calls — `cd`, exports, activated venvs. Chain with `&&`,
  or use `python`, which keeps state.
- Ask for files by absolute path. If Claude offers a download link instead, tell it you need
  the file written to disk.
- Nothing under `/tmp` can be trashed (tmpfs has no trash), so deletes there would be
  permanent — the tool says so rather than pretending.
- **`computer` does not work on Wayland.** It drives the screen through X11/XTEST, which
  Wayland compositors ignore by design, so clicks and keystrokes never reach native windows
  and screenshots come back blank. Check with `echo $XDG_SESSION_TYPE`; if it prints
  `wayland`, the tool refuses up front and tells you why rather than clicking into the void.
  Fix it with an Xorg session or `xvfb-run -s '-screen 0 1280x800x24' python main.py` —
  details under [Full setup detail](#full-setup-detail). Every other tool is unaffected.
  There's a real, narrower exception if the actual application you need to control is itself
  an XWayland client (common — many Qt/GTK/Java desktop apps still run this way even on a
  Wayland desktop): Claude can drive *that one window* directly with plain X11-protocol
  calls, entirely outside the `computer` tool. See [Full setup
  detail](#full-setup-detail) for the recipe.
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
  — that would need LibreOffice, a browser, an X11 display and real API credits. If your venv
  happens to have `pylint`/`black` installed (neither is a project dependency) or the system
  has `shellcheck`, they're safe to run by hand — expect plenty of output, since nothing is
  configured for them.
- **Two things a linter will fight you on here** — worth knowing before you "fix" them.
  Broad `except Exception`/`except BaseException` is the design, not sloppiness: every local
  tool must catch anything and return an error string rather than crash the chat loop, which
  is why `BLE001` is switched off project-wide. And cleanup paths (`shutdown`, `close`) must
  not be able to fail *or* fail silently — narrowing one has already caused a real bug, since
  `zmq.ZMQError` isn't an `OSError` and escaping `shutdown()` turns an ordinary Ctrl-C into a
  traceback. Blanket catch plus a `print()` is the pattern.

<a id="full-setup-detail"></a>

<details>
<summary><b>Full setup detail</b> — OS libraries, document tools, which package backs which tool</summary>

**Playwright.** `pip` installs the Python package but not the browser or its OS libraries:

```bash
playwright install chromium            # the browser binary
sudo playwright install-deps chromium  # OS libraries (e.g. libmanette)
```

`playwright install` with no browser name fetches all three engines; this app only launches
Chromium, so the argument is worth keeping.

**Document conversion.** `soffice` (LibreOffice) handles docx/odt/xlsx/pptx/html/rtf/txt and
PDF output, each call in a throwaway user profile so two conversions can't collide on the
profile lock. `pandoc` handles markdown, because `soffice` has no dependable markdown
import; `md → pdf` goes through odt on the way, since pandoc's own PDF writer would need a
LaTeX engine. `libreoffice-writer`/`-calc`/`-impress` alone are enough if you don't want the
whole suite.

**Computer use needs two apt packages that pip won't install.** `pip install pyautogui`
succeeds without them, so the failure is misleading — the tool reports pyautogui as missing
when it is right there:

- **`python3-tk`** — `pyautogui` pulls in `mouseinfo`, which imports `tkinter` at module
  level. Without it, `import pyautogui` raises and `computer` returns its install hint for a
  package you already have.
- **`scrot`** — `pyscreeze` only has a screenshot path if `gnome-screenshot` is present (which
  lets it use Pillow's `ImageGrab`) or `scrot` is. With neither, capture fails on X11 even
  though every Python package is installed. Either works; `scrot` is the lighter one.

**Computer use also needs X11.** The `computer` tool synthesises input through X11/XTEST, which
Wayland compositors deliberately ignore — on a Wayland session clicks and keystrokes never
reach native windows and screenshots come back blank, so the tool refuses up front and says
so instead of failing silently. Check with `echo $XDG_SESSION_TYPE`. Options:

```bash
# 1. Log in to an "Xorg"/"X11" session at your display manager, or
# 2. Run the whole client inside a nested X server:
sudo apt install xvfb
xvfb-run -s '-screen 0 1280x800x24' python main.py
# 3. XWayland-only setup and you want to try regardless:
export CLAUDE_COMPUTER_FORCE=1
```

**A real exception: an XWayland-backed target application.** The refusal above is about
`computer`'s own generic approach — `pyautogui`'s screenshot backend needs a real X11 root
window to grab and a Wayland compositor doesn't expose one, so a *whole-desktop* capture
genuinely can't be made to work this way, full stop. But if the specific application you're
trying to control is itself an XWayland client — true for many desktop GUI toolkits that
haven't been ported to native Wayland (Qt, GTK, Java/Swing, Unity Editor, JetBrains IDEs, and
more) — it still has a real, addressable X11 window underneath, and Claude can drive *that one
window* directly with plain X11-protocol tools via `bash`/`python`, bypassing
`computer`/`pyautogui` entirely:

```bash
# 1. Confirm the target really is XWayland-backed (a normal X11 window entry, not absent):
xwininfo -root -tree | grep -i "<window title>"

# 2. Find its window id and raise it:
wmctrl -l
wmctrl -i -a 0x<id>

# 3. Screenshot just that window (a whole-screen grab still won't work):
import -window 0x<id> /tmp/shot.png     # ImageMagick

# 4. Send it genuine XTEST input -- works even without xdotool installed:
python3 -c "
from Xlib import X, XK, display
from Xlib.ext import xtest
d = display.Display()
xtest.fake_input(d, X.KeyPress, d.keysym_to_keycode(XK.XK_Escape))
d.sync()
xtest.fake_input(d, X.KeyRelease, d.keysym_to_keycode(XK.XK_Escape))
d.sync()
"
```

`xdotool` is the usual convenience wrapper for step 4, but if it isn't installed (and there's
no sudo to `apt install` it), `python-xlib` calls the exact same XTEST extension directly —
`Xlib.ext.xtest.fake_input` — so it's a full substitute, not a downgrade. One real gotcha
worth knowing up front: a mouse click has to land on an actual interactive control (a real
button, not empty space or a plain label) before a *subsequent* injected key event reliably
reaches the target app's own event handlers — clicking blank space to "just establish focus"
does not reliably work the same way. This does **not** make `computer` itself work on
Wayland — the refusal above still stands, and a full-desktop screenshot genuinely isn't
possible this way. It's a separate, manual technique for one already-identified XWayland
window, useful whenever a task needs to drive or inspect one specific already-running GUI
application from a Wayland session.

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
| `interactive_run` | `pexpect` |
| `config_edit` | `ruamel.yaml` (YAML), `tomlkit` (TOML), `jsonpath-ng` (`$…` queries); JSON needs nothing |
| `sql_query` | `duckdb` |
| `trash` | `send2trash` |
| `computer` | `pyautogui`, `pillow` — **plus `python3-tk` and `scrot` from apt, and an X11 display** (see below) |
| `memory` | nothing — standard library only |

To drop a tool entirely, remove its module from `MODULES` in `core/local_tools.py`.

**The `python` kernel's sockets are encrypted.** Everything that tool does — your code, your
data, the results — travels over ZeroMQ, which by default is plaintext on four loopback TCP
ports; `ipykernel` says so itself, warning on every start that the link "is susceptible to
eavesdropping". ResearchMesh has the kernel manager provision a CurveZMQ keypair instead, so
both ends talk CURVE. That needs `jupyter_client>=8.9.1` and `ipykernel>=7` (and a pyzmq built
with libsodium, which the wheels are); on anything older it falls back to a Unix socket in the
Jupyter runtime dir, and then to plaintext TCP, printing which and why each time it drops a
tier. Set `CLAUDE_KERNEL_ENCRYPTION=required` to make an unencrypted kernel a hard error
rather than a fallback — if you see that error, `pip install -U 'jupyter_client>=8.9.1'
'ipykernel>=7'` is the fix.

**Environment variables** must be exported for the user account you launch as — `main.py`
calls `os.getenv()` directly. Put them in `~/.bashrc` for interactive shells, or
`~/.bash_profile` / `~/.profile` for login shells (e.g. SSH). Note `export`, and **no spaces**
around `=`; `VAR = value` is a bash syntax error. Then open a fresh shell or `source` it, and
check without revealing anything:

```bash
echo "key: ${ANTHROPIC_API_KEY:+set}  token: ${N8N_MCP_TOKEN:+set}"   # per your token_env names
```

**Built and tested on** Ubuntu 26.04 LTS (kernel 7.0.0), Python 3.14.4, Playwright 1.61.0.
`pyproject.toml` requires 3.11+ (the floor is `tomllib`, used by `main.py`); 3.14 is just what
it was run on. The `install-deps` step assumes a Debian/Ubuntu `apt` system.

</details>

<details>
<summary><b>HTTPS and TLS</b> — for an MCP server with a self-signed or private-CA certificate</summary>

A server URL may be `http://` or `https://`. TLS is verified by the `httpx` client inside
`mcp_client.py`, offline, against a local CA bundle — the CA is not contacted at connect time.

A publicly-signed certificate (Let's Encrypt, DigiCert, …) works with no configuration. A
self-signed or internal-CA certificate isn't in `certifi`, so point `httpx` at a bundle that
contains your CA:

```bash
export SSL_CERT_FILE=/path/to/your-ca-chain.pem   # or SSL_CERT_DIR for a hashed dir
```

Two things that catch people out:

- `SSL_CERT_FILE` **replaces** the default trust store rather than adding to it. If the same
  process also needs public HTTPS hosts, concatenate:
  `cat "$(python -m certifi)" your-ca.pem > combined-ca.pem`
- Your server (or its reverse proxy) must present its **full chain**. A missing
  intermediate is the most common "the cert is valid but it still won't connect" cause, and
  the fix is on the server — the client only needs the root.

The OS trust store (`/etc/ssl/certs`) does not affect this app.

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
  claude_learned_schemas.py      bash, file editor, web_search, web_fetch
  memory.py                      /memories store, persists across sessions
  computer.py                    screenshots + mouse/keyboard (X11 only)
  browser.py                     Playwright DOM surfing
  documents.py                   LibreOffice / pandoc conversion
  kernel.py                      persistent IPython kernel
  processes.py                   pexpect — commands that prompt
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
  `bash` could already run.

Check every configured server on its own with `python mcp_client.py` — it connects to each
in turn, lists its tools, and reports failures without starting the chat.

</details>

<details>
<summary><b>Optional: MCP Inspector</b> — for debugging an MCP server (needs Node)</summary>

This project is **pure Python; Node.js is not a dependency.** For hand-calling the tools your
MCP endpoint exposes, the [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
runs on demand with no install step:

```bash
npx @modelcontextprotocol/inspector@latest
```

An external debugging aid, nothing in the repo depends on it.

</details>

## License

[MIT](LICENSE) — use it, fork it, ship it. No warranty; see the file for the full text.
