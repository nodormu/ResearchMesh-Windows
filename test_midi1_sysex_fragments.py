"""Tests for joining SysEx fragments in midi1's rtmidi input.

    python test_midi1_sysex_fragments.py

WinMM delivers a SysEx one driver buffer at a time (1,024 bytes by default), so a
long SysEx reaches the callback in pieces. These checks feed the callback the
fragment sequences that produces. They cannot show what a real Windows MIDI
driver does, so a long SysEx through a real port (loopMIDI) still needs a
hands-on run on Windows.
"""

import os
import sys
import time

import mido

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import midi1

FAILURES: list[str] = []
BUFFER = 1024


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


def port():
    delivered: list = []
    p = midi1._RtMidiInput.__new__(midi1._RtMidiInput)
    p._partial = None
    p._deliver = delivered.append
    p._burst = midi1._OpenBurst(time.time())
    return p, delivered


def feed(p, *fragments) -> None:
    for fragment in fragments:
        p._callback((list(fragment), 0.0), None)


def fragments_of(payload: bytes, size: int = BUFFER) -> list[bytes]:
    """What WinMM hands over: the SysEx cut into buffers of `size` bytes."""
    whole = b"\xf0" + payload + b"\xf7"
    return [whole[i:i + size] for i in range(0, len(whole), size)]


def main() -> int:
    print("the fact the fix rests on")
    first = fragments_of(bytes(range(100)) * 20)[0]
    try:
        mido.Message.from_bytes(first)
        check("a bare first fragment cannot be parsed on its own", False)
    except ValueError:
        check("a bare first fragment cannot be parsed on its own, so it used to be dropped", True)

    print("joining fragments")
    p, got = port()
    feed(p, b"\xf0\x7d\x01\x02\xf7")
    check("a SysEx that fits one buffer is delivered once", len(got) == 1 and got[0][1].type == "sysex" and got[0][1].data == (0x7D, 1, 2))

    p, got = port()
    payload = bytes(i % 128 for i in range(3000))
    pieces = fragments_of(payload)
    feed(p, *pieces)
    check("a SysEx over three buffers arrives as one message", len(pieces) == 3 and len(got) == 1, f"{len(pieces)} fragments, {len(got)} delivered")
    check("with every byte, in order", got and bytes(got[0][1].data) == payload)

    p, got = port()
    payload = bytes(i % 128 for i in range(100_000))
    feed(p, *fragments_of(payload))
    check("a 100 KB SysEx reassembles exactly", len(got) == 1 and bytes(got[0][1].data) == payload)

    p, got = port()
    payload = bytes(i % 128 for i in range(BUFFER - 1))  # F0 + payload fills one buffer; F7 is alone in the next
    pieces = fragments_of(payload)
    feed(p, *pieces)
    check("an F7 alone in the last buffer closes it", pieces[-1] == b"\xf7" and len(got) == 1 and bytes(got[0][1].data) == payload, str([len(x) for x in pieces]))

    print("what falls in between")
    p, got = port()
    pieces = fragments_of(bytes(i % 128 for i in range(2500)))
    feed(p, pieces[0], b"\xf8", pieces[1], b"\xfe", pieces[2])
    kinds = [m[1].type for m in got]
    check("real-time bytes inside a SysEx are delivered on their own", kinds == ["clock", "active_sensing", "sysex"], str(kinds))

    p, got = port()
    pieces = fragments_of(bytes(i % 128 for i in range(2500)))
    feed(p, pieces[0], b"\x90\x3c\x64")
    check("another status byte drops the cut-short SysEx and delivers the new message", [m[1].type for m in got] == ["note_on"] and p._partial is None, str([m[1].type for m in got]))
    feed(p, pieces[1], pieces[2])
    check("pieces that then arrive without a start are ignored", [m[1].type for m in got] == ["note_on"], str([m[1].type for m in got]))

    p, got = port()
    feed(p, b"\x90\x3c\x64", b"\xf0\x01\xf7", b"\x80\x3c\x00")
    check("short messages around a whole SysEx are untouched", [m[1].type for m in got] == ["note_on", "sysex", "note_off"], str([m[1].type for m in got]))

    p, got = port()
    feed(p, b"")
    check("an empty buffer is ignored", got == [] and p._partial is None)

    print("a SysEx that never ends")
    saved = midi1._MAX_SYSEX_BYTES
    midi1._MAX_SYSEX_BYTES = 2000
    try:
        p, got = port()
        pieces = fragments_of(bytes(i % 128 for i in range(10_000)))
        feed(p, *pieces[:4])
        check("it is dropped once it passes the size cap", p._partial is None and got == [], f"{p._partial is None} {got}")
    finally:
        midi1._MAX_SYSEX_BYTES = saved

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}" + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
