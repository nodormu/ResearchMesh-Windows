"""Shared output helpers for the local tools.

Every local tool clips what it returns so a single call can't flood the
context window. The per-tool budgets differ (shell/editor output is worth more
than a page of scraped HTML), so the limit is the caller's choice.

Tools that can return pixels rather than text (the file editor's `view` on an
image, memory's `view`, every computer-use screenshot) return the marker dict
built by `image_result`. Chat._local_result_to_content turns it into a real
`image` content block; anything that doesn't understand the marker just sees a
dict, never a mojibake string.

`strip_ansi` is shared by the two tools whose output arrives as terminal bytes
rather than clean text: the IPython kernel (which colours its tracebacks) and
`interactive_run` (whose ConPTY transcript is raw console output). Both are
pure context bloat once they reach Claude.
"""

import re

# Terminal escape sequences, which are noise in a transcript sent to a model.
# Deliberately wider than "colour codes": a ConPTY transcript carries cursor
# positioning, erase-in-line, private-mode toggles (`ESC[?25l`) and OSC title
# sets, none of which a CSI-with-final-letter pattern alone would catch.
#   OSC — ESC ] ... terminated by BEL or ST (ESC \)
#   CSI — ESC [ params intermediates final
#   two- and three-character escapes (ESC ( B, ESC =, ESC M, …)
_ANSI = re.compile(
    r"""
      \x1b\] .*? (?: \x07 | \x1b\\ )   # OSC, up to BEL or ST
    | \x1b\[ [0-?]* [ -/]* [@-~]       # CSI
    | \x1b [ -/]* [@-~]                # everything else introduced by ESC
    """,
    re.VERBOSE | re.DOTALL,
)


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences, leaving the text they decorated."""
    return _ANSI.sub("", text)


# Extensions the file-editor and memory tools advertise image support for
# (their descriptions both say "Image files (.jpg, .jpeg, or .png)").
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def clip(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"
    return text


def image_result(media_type: str, data: str, text: str) -> dict:
    """Marker for a tool result that is an image. `data` is base64 (no
    newlines); `text` is the caption sent alongside the pixels."""
    return {
        "__kind__": "image",
        "media_type": media_type,
        "data": data,
        "text": text,
    }
