"""Tests for how `computer` chooses a monitor.

    python test_computer_display.py

The list of monitors and the screen grab are replaced by fakes that record what
they are asked for, so these checks cover this module's logic: how a monitor
rectangle maps to coordinates and to a capture. They cannot show what Windows
reports for a real set of monitors, so display selection still needs a
hands-on run on Windows with two monitors.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import computer

FAILURES: list[str] = []
DW, DH = computer.DISPLAY_WIDTH, computer.DISPLAY_HEIGHT
computer._set_dpi_aware = lambda: None


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


class FakePyAutoGUI:
    def __init__(self, size=(1920, 1080)):
        self._size = size
        self.regions: list = []

    def size(self):
        return self._size

    def screenshot(self, region=None):
        from PIL import Image

        self.regions.append(region)
        w, h = (region[2], region[3]) if region else self._size
        return Image.new("RGB", (w, h), "white")


def main() -> int:
    # a monitor left of the primary, with a negative origin, and one to the right
    layout = [(1920, 0, 1280, 1024), (-2560, -200, 2560, 1440), (0, 0, 1920, 1080)]
    saved_env = os.environ.pop("CLAUDE_COMPUTER_MONITOR", None)
    saved_displays = computer._displays
    computer._displays = lambda: sorted(layout)
    pg = FakePyAutoGUI()
    try:
        print("which monitor")
        check("by default the primary monitor is used", computer._screen_rect(pg) == (0, 0, 1920, 1080))
        check("a coordinate maps inside the primary monitor", computer._to_native(pg, [DW // 2, DH // 2]) == (960, 540))

        os.environ["CLAUDE_COMPUTER_MONITOR"] = "2"
        check("index 2 is the rightmost monitor", computer._screen_rect(pg) == (1920, 0, 1280, 1024))
        check("its centre maps onto the virtual desktop", computer._to_native(pg, [DW // 2, DH // 2]) == (1920 + 640, 512), str(computer._to_native(pg, [DW // 2, DH // 2])))
        check("a point past the declared box is clamped to the monitor", computer._to_native(pg, [DW + 50, DH + 50]) == (1920 + 1279, 1023))

        os.environ["CLAUDE_COMPUTER_MONITOR"] = "0"
        check("index 0 is the leftmost monitor, with negative coordinates", computer._to_native(pg, [0, 0]) == (-2560, -200), str(computer._to_native(pg, [0, 0])))
        check("a point on it converts back to declared space", computer._to_declared(pg, (-2560 + 1280, -200 + 720)) == (DW // 2, DH // 2))
        check("round trip", computer._to_declared(pg, computer._to_native(pg, [300, 200])) == (300, 200))

        print("capture")
        with mock.patch("PIL.ImageGrab.grab") as grab:
            grab.return_value = "image"
            image = computer._grab(pg)
        check("a chosen monitor is captured as its own rectangle of the virtual desktop", grab.call_args.kwargs == {"bbox": (-2560, -200, 0, 1240), "all_screens": True} and image == "image", str(grab.call_args))
        del os.environ["CLAUDE_COMPUTER_MONITOR"]
        with mock.patch("PIL.ImageGrab.grab") as grab:
            computer._grab(pg)
        check("by default the grab is the plain primary-monitor grab, unchanged", grab.call_args == mock.call(), str(grab.call_args))
        os.environ["CLAUDE_COMPUTER_MONITOR"] = "2"
        with mock.patch("PIL.ImageGrab.grab", side_effect=OSError("denied")):
            computer._grab(pg)
        check("if the first grab fails, pyautogui is asked for the same rectangle", pg.regions == [(1920, 0, 1280, 1024)], str(pg.regions))

        print("capture() for other tools")
        with mock.patch("PIL.ImageGrab.grab", return_value="image"):
            check("it returns the image and no error", computer.capture() == ("image", None))
        os.environ["CLAUDE_COMPUTER_MONITOR"] = "9"
        image, error = computer.capture()
        check("a bad monitor is an error tuple, not an exception", image is None and error is not None and "CLAUDE_COMPUTER_MONITOR=9 but 3 monitor(s) were found" in error, str(error))
        os.environ["CLAUDE_COMPUTER_MONITOR"] = "left"
        image, error = computer.capture()
        check("a non-number is an error too", image is None and error is not None and "must be a monitor index" in error, str(error))
        out = computer._screenshot(pg, "x")
        check("a bad setting reaches the model as an error text", isinstance(out, str) and out.startswith("Error: could not capture the screen: CLAUDE_COMPUTER_MONITOR"), str(out)[:120])

        print("no monitor list")
        computer._displays = saved_displays
        os.environ["CLAUDE_COMPUTER_MONITOR"] = "0"
        try:
            computer._screen_rect(pg)
            check("without a monitor list a chosen monitor is an error, not a guess", False)
        except ValueError as e:
            check("without a monitor list a chosen monitor is an error, not a guess", "0 monitor(s) were found" in str(e), str(e))
    finally:
        computer._displays = saved_displays
        if saved_env is None:
            os.environ.pop("CLAUDE_COMPUTER_MONITOR", None)
        else:
            os.environ["CLAUDE_COMPUTER_MONITOR"] = saved_env

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
