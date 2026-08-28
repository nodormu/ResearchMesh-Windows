"""`computer` — Anthropic's client-executed computer use tool (`computer_20251124`).

A learned schema: Claude already knows the action vocabulary (screenshot, clicks,
type, key, scroll, drag, zoom, …), so there is no description to write. This
module supplies the eyes and hands — screen capture via Pillow/pyautogui, input
via pyautogui — and, more importantly, the coordinate contract.

**Coordinates.** Claude returns coordinates in the space of the image it was
sent, so the declared `display_width_px`/`display_height_px` must match the
screenshot's real pixel dimensions or every click lands offset. Real screens are
usually larger than the ~1.15MP that reads well, so this module declares one
fixed logical size (`CLAUDE_DISPLAY_SIZE`, default 1280x800), always downscales
captures to exactly that, and scales Claude's coordinates back up to native
screen space. Declared size and sent image can therefore never drift apart.

**This tool is beta-gated.** `computer_20251124` requires the
`computer-use-2025-11-24` beta header, which is why core/claude.py posts to
`client.beta.messages.create` — see BETAS there. Declaring this tool without
that header is a 400 on every request, not just computer-use ones.

**DPI scaling.** Windows' own version of the coordinate problem above, and the
reason `_set_dpi_aware()` runs before pyautogui is ever asked anything. A
process that has not declared DPI awareness is lied to by the OS: on a display
at 150% scaling, `pyautogui.size()` reports the *virtualised* 1280x800 while
`ImageGrab` captures the real 1920x1200 framebuffer, and `SetCursorPos` is
silently rescaled underneath. `_to_native()` divides by one and multiplies by
the other, so every click lands progressively further off toward the
bottom-right. Declaring per-monitor awareness makes both APIs report true
physical pixels, which is the only state in which this module's arithmetic is
correct.

**Elevation (UIPI).** Windows does have one case where synthesised input goes
nowhere, and it is the closer analogue of the POSIX build's Wayland problem
than the DPI note above: User Interface Privilege Isolation stops a process at
medium integrity from sending input to a window owned by a higher-integrity
one. If anything running as Administrator has focus — an elevated terminal,
UAC's own consent dialog, some installers, Task Manager — clicks and keystrokes
from here are discarded, and the screenshot afterwards looks exactly like a
click that did nothing.

It is not guarded up front the way Wayland was, because unlike Wayland it is a
property of whichever window happens to have focus at that instant rather than
of the session, so a refusal at tool-entry would be wrong most of the time.
Instead `_type` checks how many events `SendInput` actually delivered and says
so, which is the only point where Windows reports the failure at all. The mouse
actions have no equivalent signal — `SendInput` for a click succeeds whether or
not the target accepts it — so a click on an elevated window is genuinely
silent. If a sequence of actions is having no visible effect, this is the first
thing to suspect.
"""

import asyncio
import base64
import ctypes
import io
import os
import time
from typing import ClassVar

from core.output import image_result

# Anthropic's benchmarked baselines are 1280x800 for web apps and 1024x768 /
# 1280x720 for general desktop work; below ~1280x720 accuracy drops off.
_DEFAULT_SIZE = (1280, 800)


def _declared_size() -> tuple[int, int]:
    raw = os.getenv("CLAUDE_DISPLAY_SIZE", "")
    if raw:
        try:
            w, h = (int(part) for part in raw.lower().split("x", 1))
            if w > 0 and h > 0:
                return w, h
        except ValueError:
            pass
    return _DEFAULT_SIZE


DISPLAY_WIDTH, DISPLAY_HEIGHT = _declared_size()

COMPUTER_TOOL = {
    "type": "computer_20251124",
    "name": "computer",
    "display_width_px": DISPLAY_WIDTH,
    "display_height_px": DISPLAY_HEIGHT,
    # Lets Claude re-inspect a region at native resolution instead of asking for
    # a second full screenshot — cheaper, and the only way to read small text
    # once the capture has been downscaled to the declared size.
    "enable_zoom": True,
}
TOOLS = [COMPUTER_TOOL]

# The beta header this tool requires. core/claude.py reads it from here so the
# header and the tool version can never drift apart.
BETA_FLAG = "computer-use-2025-11-24"

# Let the UI repaint before we capture the result of an action.
_SETTLE = 0.4

# Claude uses xdotool-style key names; pyautogui has its own spelling.
_KEY_ALIASES = {
    "return": "enter",
    "kp_enter": "enter",
    "escape": "esc",
    "backspace": "backspace",
    "page_up": "pageup",
    "prior": "pageup",
    "page_down": "pagedown",
    "next": "pagedown",
    "super": "win",
    "super_l": "win",
    "meta": "win",
    "control": "ctrl",
    "control_l": "ctrl",
    "alt_l": "alt",
    "shift_l": "shift",
}

# Actions that never get a follow-up screenshot appended: `wait` changes nothing
# worth re-capturing, and screenshot/zoom already *are* the capture — appending
# to them would double the image on success and double the error on failure.
_NO_SCREENSHOT = {"wait", "screenshot", "zoom"}


def handles(name: str) -> bool:
    return name == "computer"


async def execute(name: str, tool_input: dict) -> str | dict:
    if name != "computer":
        return f"Error: {name} is not handled by the computer tool"
    return await asyncio.to_thread(_run, tool_input)


_dpi_done = False


def _set_dpi_aware() -> None:
    """Tell Windows to report real pixels, once per process.

    Without this the OS virtualises coordinates for a scaled display and the
    module's declared-size arithmetic silently drifts — see the module
    docstring. Per-monitor-v2 is the mode that stays correct when the pointer
    crosses between monitors at different scale factors; the two older APIs
    are tried in turn for Windows releases that predate it.

    Failure here is not fatal: it means the process was already marked aware
    (the usual reason — the call raises OSError when awareness is already set,
    which is the outcome we wanted anyway) or the box is old enough to lack
    all three entry points, in which case the tool still works and is merely
    inaccurate on a scaled display. Reported rather than swallowed so that
    inaccuracy is traceable.
    """
    global _dpi_done
    if _dpi_done:
        return
    _dpi_done = True

    def per_monitor_v2():
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
            raise OSError("SetProcessDpiAwarenessContext returned false")

    def per_monitor():
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

    def system_wide():
        ctypes.windll.user32.SetProcessDPIAware()  # Vista+

    reasons = []
    for attempt in (per_monitor_v2, per_monitor, system_wide):
        try:
            attempt()
            return
        except Exception as e:
            reasons.append(f"{attempt.__name__}: {e}")
    # Only worth saying once every tier has failed: any single failure is
    # normal (the newer entry points simply do not exist on older Windows,
    # and all three raise if awareness was already set by a manifest or by an
    # earlier call, which is the outcome we wanted anyway).
    print(f"[computer] could not set DPI awareness ({'; '.join(reasons)}); "
          "coordinates may be inaccurate on a scaled display")


def _run(tool_input: dict) -> str | dict:
    _set_dpi_aware()

    action = tool_input.get("action")
    if not action:
        return "Error: no action provided"

    try:
        import pyautogui
    except Exception as e:  # ImportError, or X11 lookup failure at import
        return (
            f"Error: the computer tool needs pyautogui ({e}). "
            "Install it with: pip install pyautogui pillow"
        )
    # Its default is to abort on a corner-of-screen mouse position; that turns a
    # legitimate click at (0, 0) into a crash.
    pyautogui.FAILSAFE = False

    try:
        result = _dispatch(pyautogui, action, tool_input)
    except Exception as e:
        return f"Error: {action} failed: {e}"

    if isinstance(result, dict) or action in _NO_SCREENSHOT:
        return result
    time.sleep(_SETTLE)
    shot = _screenshot(pyautogui, caption=f"After {action}.")
    if isinstance(shot, dict):
        return shot
    return f"{result} (screenshot unavailable: {shot})"


def _dispatch(pyautogui, action: str, ti: dict):
    if action == "screenshot":
        return _screenshot(pyautogui, caption="Current screen.")

    if action == "zoom":
        return _zoom(pyautogui, ti.get("region"))

    if action == "wait":
        duration = min(float(ti.get("duration", 1)), 30.0)
        time.sleep(duration)
        return f"Waited {duration}s."

    if action == "mouse_move":
        x, y = _to_native(pyautogui, ti.get("coordinate"))
        pyautogui.moveTo(x, y)
        return f"Moved to {ti['coordinate']}."

    if action in ("left_mouse_down", "left_mouse_up"):
        if ti.get("coordinate"):
            x, y = _to_native(pyautogui, ti["coordinate"])
            pyautogui.moveTo(x, y)
        (pyautogui.mouseDown if action == "left_mouse_down" else pyautogui.mouseUp)()
        return f"{action} done."

    if action in (
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
    ):
        return _click(pyautogui, action, ti)

    if action == "left_click_drag":
        start = ti.get("start_coordinate")
        if start:
            sx, sy = _to_native(pyautogui, start)
            pyautogui.moveTo(sx, sy)
        ex, ey = _to_native(pyautogui, ti.get("coordinate"))
        pyautogui.mouseDown()
        pyautogui.moveTo(ex, ey, duration=0.2)
        pyautogui.mouseUp()
        return f"Dragged to {ti['coordinate']}."

    if action == "type":
        return _type(ti.get("text", ""))

    if action == "key":
        keys = _keys(ti.get("text", ""))
        if not keys:
            return "Error: key action needs a `text` key combination"
        pyautogui.hotkey(*keys)
        return f"Pressed {ti.get('text')}."

    if action == "hold_key":
        keys = _keys(ti.get("text", ""))
        duration = min(float(ti.get("duration", 1)), 30.0)
        for k in keys:
            pyautogui.keyDown(k)
        time.sleep(duration)
        for k in reversed(keys):
            pyautogui.keyUp(k)
        return f"Held {ti.get('text')} for {duration}s."

    if action == "scroll":
        return _scroll(pyautogui, ti)

    return f"Error: unsupported action {action!r}"


# --- typing ---------------------------------------------------------------
# `pyautogui.write` is not usable for this action on Windows, and fails in the
# worst way: silently. Reading its source (_pyautogui_win.py), `keyboardMapping`
# is populated only for `chr(32)` through `chr(127)`, and `_keyDown` begins
# `if key not in keyboardMapping or keyboardMapping[key] is None: return`. So
# every non-ASCII character is dropped with no error — a name with an accent, a
# curly quote pasted out of a document, an em dash, any non-Latin script — while
# the tool still reports the full length as typed.
#
# It is worse than dropping on a non-US layout. The map is built with
# `VkKeyScanA`, which returns -1 for a character the current layout cannot
# produce. -1 is not None, so it passes that guard, and `divmod(-1, 0x100)`
# yields a garbage virtual-key code: the wrong character gets typed rather than
# none. A US-layout dev box hides this entirely.
#
# SendInput with KEYEVENTF_UNICODE sidesteps the layout: it injects a UTF-16
# code unit directly instead of naming a key to press. Non-BMP characters
# (emoji) are sent as their two surrogates, which is what the API expects.
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1


class _MOUSEINPUT(ctypes.Structure):
    # Declared only so the union below is the true size of a Win32 INPUT.
    # SendInput validates the `cbSize` it is given against its own idea of
    # that size and returns 0 — sending nothing — if they disagree, so
    # omitting the larger union member would break every call.
    #
    # c_int32 rather than c_long for the Win32 LONG fields. Both are 4 bytes on
    # Windows so this changes nothing there, but c_long follows the host's C
    # `long` and is 8 bytes on 64-bit Linux — which made these structures
    # compute a 48-byte INPUT when checked from the machine this port was
    # written on, and left no way to tell a real layout error from an artefact
    # of the checking host. Fixed-width types make the arithmetic the same
    # everywhere, so `ctypes.sizeof(_INPUT) == 40` is a claim that can be
    # tested off Windows.
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUTUNION(ctypes.Union):
    # Annotated ClassVar only to satisfy RUF012, which special-cases
    # ctypes.Structure but not ctypes.Union. The value must stay a plain list;
    # ctypes reads it at class-creation time and the annotation does not change
    # what is assigned.
    _fields_: ClassVar = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("union", _INPUTUNION)]


def _utf16_units(text: str) -> list[int]:
    """Every UTF-16 code unit in `text` — two for anything above the BMP.

    SendInput's unicode path carries one UTF-16 unit per event, so an emoji is
    delivered as its high and low surrogate in sequence. Encoding the whole
    string at once and walking it in pairs gets that for free.
    """
    encoded = text.encode("utf-16-le")
    return [
        int.from_bytes(encoded[i : i + 2], "little")
        for i in range(0, len(encoded), 2)
    ]


def _unicode_events(text: str) -> list:
    """A keydown/keyup INPUT pair per UTF-16 code unit, in order."""
    events = []
    for code_unit in _utf16_units(text):
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            events.append(
                _INPUT(
                    type=_INPUT_KEYBOARD,
                    union=_INPUTUNION(
                        ki=_KEYBDINPUT(
                            wVk=0,
                            wScan=code_unit,
                            dwFlags=flags,
                            time=0,
                            dwExtraInfo=None,
                        )
                    ),
                )
            )
    return events


def _type(text: str) -> str:
    if not text:
        return "Typed 0 characters."
    events = _unicode_events(text)
    array = (_INPUT * len(events))(*events)
    send_input = ctypes.windll.user32.SendInput
    # Declared rather than left to ctypes' defaults: the middle argument is a
    # pointer, and an undeclared pointer argument is passed as a C int, which
    # truncates it on 64-bit Windows.
    send_input.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    send_input.restype = ctypes.c_uint32
    sent = send_input(len(events), ctypes.byref(array), ctypes.sizeof(_INPUT))
    if sent == len(events):
        return f"Typed {len(text)} characters."
    # Partial or zero delivery. The usual cause is UIPI: a process at medium
    # integrity cannot inject input into a window owned by an elevated one, and
    # SendInput reports that by silently delivering fewer events rather than
    # failing loudly. Say so instead of claiming success — this is the one
    # Windows equivalent of the POSIX build's "clicking into the void", and it
    # applies to every action here, not just typing.
    return (
        f"Error: typed only {sent} of {len(events)} key events. The usual cause "
        "is a focused window running as Administrator: Windows blocks input "
        "from a non-elevated process into an elevated one (UIPI), silently. "
        "Click a non-elevated window first, or restart this client elevated if "
        "the target genuinely requires it."
    )


def _click(pyautogui, action: str, ti: dict) -> str:
    if ti.get("coordinate"):
        x, y = _to_native(pyautogui, ti["coordinate"])
        pyautogui.moveTo(x, y)
    button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
    clicks = {"double_click": 2, "triple_click": 3}.get(action, 1)
    # On click/scroll actions `text` carries modifiers to hold, not text to type.
    with _modifiers(pyautogui, ti.get("text")):
        pyautogui.click(button=button, clicks=clicks, interval=0.05)
    return f"{action} at {ti.get('coordinate')}."


def _scroll(pyautogui, ti: dict) -> str:
    if ti.get("coordinate"):
        x, y = _to_native(pyautogui, ti["coordinate"])
        pyautogui.moveTo(x, y)
    direction = (ti.get("scroll_direction") or "down").lower()
    amount = int(ti.get("scroll_amount", 3))
    with _modifiers(pyautogui, ti.get("text")):
        if direction in ("up", "down"):
            pyautogui.scroll(amount if direction == "up" else -amount)
        else:
            pyautogui.hscroll(amount if direction == "right" else -amount)
    return f"Scrolled {direction} by {amount}."


class _modifiers:
    """Hold modifier keys for the duration of a click or scroll."""

    def __init__(self, pyautogui, text):
        self.pyautogui = pyautogui
        self.keys = _keys(text or "")

    def __enter__(self):
        for k in self.keys:
            self.pyautogui.keyDown(k)

    def __exit__(self, *exc):
        for k in reversed(self.keys):
            self.pyautogui.keyUp(k)
        return False


def _keys(text: str) -> list[str]:
    parts = [p for p in str(text).replace(" ", "").split("+") if p]
    return [_KEY_ALIASES.get(p.lower(), p.lower()) for p in parts]


def _to_native(pyautogui, coordinate) -> tuple[int, int]:
    """Declared-space coordinate from Claude -> real screen pixels."""
    if not coordinate or len(coordinate) != 2:
        raise ValueError("this action needs a [x, y] coordinate")
    native_w, native_h = pyautogui.size()
    x = round(int(coordinate[0]) * native_w / DISPLAY_WIDTH)
    y = round(int(coordinate[1]) * native_h / DISPLAY_HEIGHT)
    # Clamp: a coordinate slightly outside the declared box is a rounding
    # artefact, not a reason to fail the action.
    return max(0, min(x, native_w - 1)), max(0, min(y, native_h - 1))


def _grab(pyautogui):
    """Native-resolution PIL screenshot, trying each backend in turn.

    Both backends are Pillow underneath on Windows, but they are not the same
    call: pyautogui's `screenshot()` goes through pyscreeze, and the direct
    `ImageGrab.grab()` below skips it — worth having as the first tier, since
    pyscreeze is the layer that historically breaks.

    **Primary monitor only, deliberately.** Pillow offers `all_screens=True`
    to capture the whole virtual desktop, and it is the wrong choice here: it
    would silently break the coordinate contract this module exists to keep.
    `_to_native()` scales Claude's coordinates using `pyautogui.size()`, which
    reports the *primary* display, so against a two-monitor capture every
    click would be divided by the wrong width and land on the wrong screen.
    Supporting multiple monitors properly means teaching `_to_native` the
    virtual-desktop bounds *and* its origin, which can be negative when a
    monitor sits left of the primary — a real feature, not a flag flip.

    The POSIX build had a third tier shelling out to ImageMagick's `import`,
    which is X11-only and has no Windows counterpart; two backends is the
    whole ladder here.
    """
    errors = []
    try:
        from PIL import ImageGrab

        return ImageGrab.grab()
    except Exception as e:
        errors.append(f"PIL.ImageGrab: {e}")
    try:
        return pyautogui.screenshot()
    except Exception as e:
        errors.append(f"pyautogui: {e}")
    raise RuntimeError("; ".join(errors))


def _encode(image, caption: str) -> dict:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    data = base64.standard_b64encode(buffer.getvalue()).decode("ascii")
    return image_result("image/png", data, caption)


def _screenshot(pyautogui, caption: str) -> str | dict:
    try:
        image = _grab(pyautogui)
    except Exception as e:
        return f"Error: could not capture the screen: {e}"
    # Resize to exactly the declared size. This is what keeps Claude's
    # coordinate space and the declared display size in agreement.
    if image.size != (DISPLAY_WIDTH, DISPLAY_HEIGHT):
        from PIL import Image

        # `Image.Resampling.LANCZOS`, not the older `Image.LANCZOS` alias:
        # the enum is the canonical spelling since Pillow 9.1, and the alias
        # is absent from the type stubs.
        image = image.resize(
            (DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.Resampling.LANCZOS
        )
    return _encode(image, f"{caption} Screen is {DISPLAY_WIDTH}x{DISPLAY_HEIGHT}.")


def _zoom(pyautogui, region) -> str | dict:
    if not region or len(region) != 4:
        raise ValueError("zoom needs a [x1, y1, x2, y2] region")
    try:
        image = _grab(pyautogui)
    except Exception as e:
        return f"Error: could not capture the screen: {e}"

    native_w, native_h = image.size
    x1, y1, x2, y2 = (int(v) for v in region)
    scale_x, scale_y = native_w / DISPLAY_WIDTH, native_h / DISPLAY_HEIGHT
    box = (
        max(0, round(x1 * scale_x)),
        max(0, round(y1 * scale_y)),
        min(native_w, round(x2 * scale_x)),
        min(native_h, round(y2 * scale_y)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return f"Error: empty zoom region {list(region)}"
    crop = image.crop(box)
    return _encode(
        crop,
        f"Zoom of region {list(region)} at native resolution ({crop.width}x{crop.height}).",
    )
