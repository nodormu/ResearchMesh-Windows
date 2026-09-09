"""Registry of the client-executed tools.

Every module listed in MODULES exposes the same three names — `TOOLS` (Anthropic
tool schemas), `handles(name)`, and `await execute(name, input)` — so adding a
tool means writing one module and adding it here, rather than editing the chat
loop's declaration list and its routing chain separately.

Optional third-party packages are imported inside each module's `execute`, so a
tool whose dependency is missing declares itself normally and returns an install
hint if the model reaches for it.
"""

import inspect

from core import (
    browser,
    computer,
    config_edit,
    data,
    documents,
    files,
    kernel,
    listen,
    memory,
    midi1,
    powershell,
    processes,
    speak,
    text_embeddings,
    vision,
)
from core import claude_learned_schemas as learned

MODULES = [
    learned,     # text editor, web_search, web_fetch
    powershell,  # the shell
    memory,      # cross-session memory (learned schema)
    computer,    # screen/mouse/keyboard control (learned schema, beta-gated)
    browser,     # Playwright DOM surfing
    documents,   # LibreOffice / pandoc conversion
    kernel,      # stateful IPython
    processes,   # ConPTY interactive commands
    config_edit,  # comment-preserving YAML/TOML/JSON edits
    data,        # DuckDB
    files,       # trash
    text_embeddings,  # vector embeddings from a user-supplied HTTP server
    vision,       # vision-capable image queries from a user-supplied HTTP server
    speak,        # local text-to-speech via Piper, config-driven
    listen,       # local speech-to-text via faster-whisper, config-driven
    midi1,        # MIDI 1.0 device I/O via mido/python-rtmidi — device
                  # discovery, open/close/send/poll (poll carries real
                  # per-message timestamps and an optional blocking wait),
                  # every standard channel/System-Common/System-Real-Time
                  # message, generic SysEx, full .mid/.syx file read+write,
                  # and a large set of typed SysEx convenience messages
                  # (MTC incl. Quarter Frame and NAK, MMC incl. the full
                  # Information-Field register/masked_write and a
                  # decode_mmc_response action, MSC, RPN/NRPN, General
                  # MIDI system, device inquiry/control, channel mode,
                  # MIDI tuning, notation, and more). midi2 (MIDI 2.0/UMP)
                  # was removed from this project and moved to its own
                  # standalone project for further work.
]

TOOLS = [tool for module in MODULES for tool in module.TOOLS]

_DUPLICATES = {
    name
    for name in (t["name"] for t in TOOLS)
    if [t["name"] for t in TOOLS].count(name) > 1
}
if _DUPLICATES:
    raise ValueError(f"duplicate local tool names: {sorted(_DUPLICATES)}")


def handles(name: str) -> bool:
    return any(module.handles(name) for module in MODULES)


async def execute(name: str, tool_input: dict) -> str | None:
    """Run a local tool, or return None if no local module owns that name."""
    for module in MODULES:
        if module.handles(name):
            return await module.execute(name, tool_input)
    return None


async def shutdown():
    """Release everything a local tool may have started. Safe if unused.

    Each step is isolated: these run as an AsyncExitStack callback on the way
    out, so an exception escaping one of them would both skip every later step
    (leaking whatever it owns) and turn an ordinary Ctrl-C into a traceback.
    One tool failing to clean up must not stop the others from trying.
    """
    for label, close in (
        ("browser", browser.shutdown),
        ("kernel", kernel.shutdown),
        ("sql_query", data.close),
        ("midi1", midi1.close_all),
    ):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            print(f"[shutdown] {label} cleanup failed (ignored): {e}")
