import asyncio
import base64
from pathlib import Path

from core.output import IMAGE_MEDIA_TYPES, clip, image_result

# Anthropic-defined ("learned") tool schemas. Claude already knows how to use
# these, so they carry no description.
#   text_editor             -> client-executed (we run it below)
#   web_search + web_fetch  -> server-executed by Anthropic (no executor here)
#
# The shell tool is core/powershell.py, not a learned schema here. A learned
# schema is normally free to keep, but that is exactly what ruled this one out:
# Anthropic's `bash_20250124` trains the model to emit shell for that tool name,
# and no interpreter behind it can change what the name asks for. A custom
# schema is what lets the description say "write PowerShell cmdlets" and be
# believed.

TEXT_EDITOR_TOOL = {
    "type": "text_editor_20250728",
    "name": "str_replace_based_edit_tool",
}
# The web tools are versioned by capability, not superseded: each dated variant
# is a superset of the last, so we track the newest. 20260318 adds an optional
# `response_inclusion` to both (set it to "excluded" to drop dynamically-filtered
# result blocks from the response); web_fetch also carries `use_cache` from
# 20260309. Both are left at their defaults ("full" / true) here.
WEB_SEARCH_TOOL = {"type": "web_search_20260318", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260318", "name": "web_fetch"}

TOOLS = [TEXT_EDITOR_TOOL, WEB_SEARCH_TOOL, WEB_FETCH_TOOL]

# Tool names we execute locally. web_search / web_fetch run on Anthropic's side
# and never come back to us as tool_use blocks.
_LOCAL = {"str_replace_based_edit_tool"}

_MAX_OUTPUT = 12000


def handles(name: str) -> bool:
    return name in _LOCAL


async def execute(name: str, tool_input: dict) -> str:
    # Run blocking work off the event loop.
    if name == "str_replace_based_edit_tool":
        return await asyncio.to_thread(_run_text_editor, tool_input)
    return f"Error: {name} is not locally executable"


# --- text editor --------------------------------------------------------
# Line endings are preserved rather than translated, which on Windows takes
# deliberate effort. `Path.write_text` opens in text mode, and text mode on
# Windows rewrites every "\n" it is handed as "\r\n". Since `read_text`
# normalises the other way, the round trip in `str_replace`/`insert` is not
# neutral: editing one line of an LF file would silently rewrite every line
# ending in it to CRLF. That corrupts shell scripts, turns a one-line edit
# into a whole-file diff, and is invisible until someone runs `git diff`.
#
# `newline=""` on both sides disables the translation in both directions, so
# what is on disk stays on disk and only the edited region changes. `_newline`
# then picks the file's own dominant ending for any line this tool adds.
def _read(p: Path) -> str:
    with p.open("r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(p: Path, text: str) -> None:
    with p.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _newline(text: str) -> str:
    """The file's own dominant line ending, for lines we insert into it."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _run_text_editor(tool_input: dict) -> str:
    command = tool_input.get("command")
    path = tool_input.get("path", "")
    try:
        if command == "view":
            return _view(path, tool_input.get("view_range"))
        if command == "create":
            # Written through _write, so the model's "\n" reaches disk as "\n"
            # rather than being silently upgraded to CRLF. Predictable, and
            # consistent with every edit that follows; nothing on modern
            # Windows requires CRLF, including Notepad since 2018.
            _write(Path(path), tool_input.get("file_text", ""))
            return f"File created: {path}"
        if command == "str_replace":
            return _str_replace(
                path, tool_input.get("old_str", ""), tool_input.get("new_str", "")
            )
        if command == "insert":
            return _insert(
                path,
                int(tool_input.get("insert_line", 0)),
                tool_input.get("insert_text", ""),
            )
        return f"Error: unknown text_editor command {command!r}"
    except FileNotFoundError:
        return f"Error: no such file: {path}"
    except Exception as e:
        return f"Error: {e}"


def _view(path: str, view_range):
    p = Path(path)
    if p.is_dir():
        return "\n".join(sorted(x.name for x in p.iterdir())) or "(empty directory)"

    media_type = IMAGE_MEDIA_TYPES.get(p.suffix.lower())
    if media_type is not None:
        if not p.is_file():
            return f"Error: no such file: {path}"
        data = base64.standard_b64encode(p.read_bytes()).decode("ascii")
        return image_result(media_type, data, f"Displayed image: {p}")

    lines = p.read_text(encoding="utf-8").splitlines()
    start, end = 1, len(lines)
    if view_range:
        start, end = view_range[0], view_range[1]
        if end == -1:
            end = len(lines)
    start = max(start, 1)
    end = min(end, len(lines))
    numbered = [f"{i}\t{lines[i - 1]}" for i in range(start, end + 1)]
    return clip("\n".join(numbered), _MAX_OUTPUT) or "(empty file)"


# Matching happens on an LF-normalised copy, and the file's own ending is put
# back on write. Both halves are needed: `_view` shows Claude the file through
# `splitlines()`, which strips "\r", so the `old_str` it sends back always uses
# "\n" — against the raw CRLF bytes `_read` now returns, an exact match would
# find nothing and every edit to a CRLF file would fail with "old_str not
# found". The one thing this does not preserve is a file with *mixed* endings,
# which is normalised to its dominant one.
def _str_replace(path: str, old: str, new: str) -> str:
    p = Path(path)
    raw = _read(p)
    newline = _newline(raw)
    content = raw.replace("\r\n", "\n")
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")

    count = content.count(old)
    if count == 0:
        return "Error: old_str not found; no changes made"
    if count > 1:
        return f"Error: old_str matched {count} times; it must match exactly once"

    edited = content.replace(old, new, 1)
    _write(p, edited.replace("\n", newline) if newline == "\r\n" else edited)
    return f"Edited {path}"


def _insert(path: str, line: int, text: str) -> str:
    p = Path(path)
    raw = _read(p)
    newline = _newline(raw)
    lines = raw.splitlines()
    if line < 0 or line > len(lines):
        return f"Error: insert_line {line} out of range (0..{len(lines)})"
    lines.insert(line, text.replace("\r\n", "\n"))
    _write(p, newline.join(lines) + newline)
    return f"Inserted text after line {line} in {path}"
