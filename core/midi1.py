"""MIDI 1.0 tool: device discovery, port I/O, typed message building, and
.mid/.syx files.

Built on mido for messages and files, and mido's python-rtmidi backend for
port listing. Ports come from one of two backends (_PORT_BACKEND):
  - "alsa" on Linux: ALSA sequencer clients midi1 opens through cffi
    (_AlsaInput, _AlsaOutput), described below.
  - "rtmidi" elsewhere (CoreMIDI on macOS, WinMM on Windows): rtmidi.MidiIn
    and MidiOut opened directly (_RtMidiInput, _RtMidiOutput).
If mido isn't installed, the module still imports and every action returns
an install hint.

Actions (dispatched by `_run`):
  - list_devices        input and output port names.
  - open / close        open a named port as "input" or "output"; `open`
                        returns a handle for later actions.
  - send                build a message from a typed dict and send it.
                        Most types are one wire message; rpn, nrpn and
                        mtc_quarter_frame_sequence are several. 'sent' in the
                        response is a string for one message and a list for
                        several.
  - poll                return messages buffered on an input handle: mido's
                        text, the raw hex, the decoded dict (_decode_message),
                        any RPN/NRPN or Quarter Frame change it completes
                        (_StreamDecoder), and a wall-clock 'received_at'
                        taken when the message arrived. An entry with
                        'overflow': true marks where the input queue
                        overflowed and messages were lost. 'at_open': true
                        marks the burst that arrives as the input opens
                        (_OpenBurst): normally what the device stored while
                        the port was closed, such as earlier playing or a
                        reply to a request sent before the open. A message
                        that arrives live within 20 ms of the open is
                        marked too.
                        'timeout_seconds' (0-60, default 0) waits until a
                        message arrives or the timeout passes.
  - read_midi_file      .mid/.midi (mido.MidiFile) or .syx
                        (mido.read_syx_file): file info plus a per-track
                        summary and message text, capped by 'max_messages'.
  - write_midi_file     build a .mid/.midi or .syx file from typed message
                        dicts, then re-read it with read_midi_file as a check.
  - decode_mmc_response decode an MMC Response SysEx (F0 7F <dev> 07 ... F7,
                        passed with F0/F7 included).
  - run_clock           send MIDI Clock (24 per quarter note) at a set BPM for
                        a fixed duration, with Start/Continue before and Stop
                        after. One 'send' per tool call is too slow and uneven
                        to drive a device's tempo.
  - describe            the fields of one message type (and command) from
                        _DOCS, with an example; with no type, the type list.

Where messages are built: single wire messages in `_build_message`,
multi-message types in `_build_message_sequence`, file-only meta events in
`_build_meta_message`. The 'type' enum in TOOLS lists every message type.

SysEx 'data' excludes F0/F7; mido adds them on send and strips them on
receive. Every data byte must be 0-127.

Input ports are _AlsaInput, not mido ports. rtmidi's input client has a
200-event kernel queue; a faster burst (a device's backlog at open, a large
SysEx) overflows it, and the kernel then discards everything queued.
_AlsaInput's client has the kernel's largest queue, 2000 events, and reports
an overflow. mido's rtmidi input also always drops Active Sensing; 'open'
with 'active_sensing': true passes it through to 'poll'.

Output ports are _AlsaOutput. rtmidi's output sends a SysEx as one event, and
alsa-lib refuses an event as large as its 16,384-byte output buffer, so a
SysEx over 16,353 data bytes couldn't be sent. _AlsaOutput splits SysEx into
256-byte events, ALSA's own transport for large SysEx, and retries while a
receiving queue is full.

Open ports and input buffers live in process memory; handles don't survive a
ResearchMesh restart.
"""

import asyncio
import errno
import itertools
import json
import os
import re
import select
import sys
import threading
import time
from collections import deque

try:
    import mido
    _MIDO_IMPORT_ERROR: Exception | None = None
except ImportError as e:
    mido = None  # type: ignore[assignment]
    _MIDO_IMPORT_ERROR = e

# ALSA sequencer, for input and output ports (_AlsaInput, _AlsaOutput).
# Declarations match /usr/include/alsa/seq.h, seqmid.h, seq_event.h and
# seq_midi_event.h. Of snd_seq_event_t only the header fields are named;
# snd_midi_event_encode_byte/decode fill and read the 12-byte data union.
try:
    from cffi import FFI
    _ALSA_FFI = FFI()
    _ALSA_FFI.cdef("""
        typedef struct _snd_seq snd_seq_t;
        typedef struct { unsigned char client; unsigned char port; } snd_seq_addr_t;
        typedef struct { unsigned int tv_sec; unsigned int tv_nsec; } snd_seq_real_time_t;
        typedef union { unsigned int tick; snd_seq_real_time_t time; } snd_seq_timestamp_t;
        typedef struct snd_seq_event {
            unsigned char type; unsigned char flags; unsigned char tag;
            unsigned char queue; snd_seq_timestamp_t time;
            snd_seq_addr_t source; snd_seq_addr_t dest;
            unsigned char data[12];
        } snd_seq_event_t;
        typedef struct _snd_seq_client_pool snd_seq_client_pool_t;
        typedef struct _snd_seq_client_info snd_seq_client_info_t;
        typedef struct snd_midi_event snd_midi_event_t;
        struct pollfd { int fd; short events; short revents; };
        int snd_seq_open(snd_seq_t **handle, const char *name, int streams, int mode);
        int snd_seq_close(snd_seq_t *handle);
        int snd_seq_set_client_name(snd_seq_t *seq, const char *name);
        int snd_seq_create_simple_port(snd_seq_t *seq, const char *name,
                                       unsigned int caps, unsigned int type);
        int snd_seq_connect_from(snd_seq_t *seq, int my_port, int src_client, int src_port);
        int snd_seq_connect_to(snd_seq_t *seq, int my_port, int dest_client, int dest_port);
        int snd_seq_client_info_malloc(snd_seq_client_info_t **ptr);
        void snd_seq_client_info_free(snd_seq_client_info_t *ptr);
        int snd_seq_get_any_client_info(snd_seq_t *handle, int client,
                                        snd_seq_client_info_t *info);
        int snd_seq_client_info_get_card(const snd_seq_client_info_t *info);
        int snd_seq_event_output(snd_seq_t *handle, snd_seq_event_t *ev);
        int snd_seq_drain_output(snd_seq_t *handle);
        int snd_seq_drop_output(snd_seq_t *handle);
        long snd_midi_event_encode_byte(snd_midi_event_t *dev, int c, snd_seq_event_t *ev);
        int snd_seq_set_client_pool_input(snd_seq_t *seq, size_t size);
        int snd_seq_client_pool_malloc(snd_seq_client_pool_t **ptr);
        void snd_seq_client_pool_free(snd_seq_client_pool_t *ptr);
        int snd_seq_get_client_pool(snd_seq_t *handle, snd_seq_client_pool_t *info);
        size_t snd_seq_client_pool_get_input_pool(const snd_seq_client_pool_t *info);
        int snd_seq_poll_descriptors(snd_seq_t *handle, struct pollfd *pfds,
                                     unsigned int space, short events);
        int snd_seq_event_input(snd_seq_t *handle, snd_seq_event_t **ev);
        int snd_midi_event_new(size_t bufsize, snd_midi_event_t **rdev);
        void snd_midi_event_free(snd_midi_event_t *dev);
        void snd_midi_event_no_status(snd_midi_event_t *dev, int on);
        long snd_midi_event_decode(snd_midi_event_t *dev, unsigned char *buf,
                                   long count, const snd_seq_event_t *ev);
    """)
    _ALSA = _ALSA_FFI.dlopen("libasound.so.2")
    # seq_event.h's event is 28 bytes; a different size means the layout above
    # doesn't match this platform.
    if _ALSA_FFI.sizeof("snd_seq_event_t") != 28:
        raise OSError("snd_seq_event_t layout doesn't match this platform")
    _ALSA_IMPORT_ERROR: Exception | None = None
except (ImportError, OSError) as e:
    _ALSA_FFI = _ALSA = None
    _ALSA_IMPORT_ERROR = e

# "alsa" on Linux when cffi and libasound load, "rtmidi" everywhere else.
# MIDI1_PORT_BACKEND=rtmidi picks rtmidi on Linux too, which is how the rtmidi
# path is tested on a Linux machine.
_PORT_BACKEND = os.environ.get("MIDI1_PORT_BACKEND") or (
    "alsa" if sys.platform.startswith("linux") and _ALSA is not None else "rtmidi"
)

# --- Command tables ------------------------------------------------------
# For each message type with a 'command' field: command name -> the byte that
# selects it on the wire. _build_message validates 'command' against these,
# and the TOOLS 'command' enum is generated from them.

# MMC (RP-013) commands that carry no data: F0 7F <device_id> 06 <opcode> F7.
_MMC_NO_DATA_COMMANDS = {
    "stop": 0x01, "play": 0x02, "deferred_play": 0x03, "fast_forward": 0x04,
    "rewind": 0x05, "record_strobe": 0x06, "record_exit": 0x07,
    "record_pause": 0x08, "pause": 0x09, "eject": 0x0A, "chase": 0x0B,
    "command_error_reset": 0x0C, "mmc_reset": 0x0D,
}

# Non-Real Time MTC Cueing special types, sent where the event number goes.
_MTC_CUEING_NRT_SPECIAL_TYPES = {
    "special_time_code_offset": 0x00,
    "special_enable_event_list": 0x01,
    "special_disable_event_list": 0x02,
    "special_clear_event_list": 0x03,
    "special_system_stop": 0x04,
    "special_event_list_request": 0x05,
}

# File Dump handshakes: each is its own Non-Real Time sub-ID#1.
_FILE_DUMP_HANDSHAKE = {
    "eof": 0x7B, "wait": 0x7C, "cancel": 0x7D, "nak": 0x7E, "ack": 0x7F,
}

_COMMANDS: dict = {
    # MMC opcodes (RP-013).
    "mmc": {
        **_MMC_NO_DATA_COMMANDS,
        "write": 0x40, "masked_write": 0x41, "read": 0x42, "update": 0x43,
        "locate": 0x44, "variable_play": 0x45, "search": 0x46,
        "shuttle": 0x47, "step": 0x48, "assign_system_master": 0x49,
        "generator_command": 0x4A, "midi_time_code_command": 0x4B,
        "move": 0x4C, "add": 0x4D, "subtract": 0x4E,
        "drop_frame_adjust": 0x4F, "procedure": 0x50, "event": 0x51,
        "group": 0x52, "command_segment": 0x53, "deferred_variable_play": 0x54,
        "record_strobe_variable": 0x55, "wait": 0x7C, "resume": 0x7F,
    },
    # MSC (RP-002/014): General Category 01-0B, Sound Commands 11-1E,
    # Two-Phase Commit 20-26.
    "msc": {
        "go": 0x01, "stop": 0x02, "resume": 0x03, "timed_go": 0x04,
        "load": 0x05, "set": 0x06, "fire": 0x07, "all_off": 0x08,
        "restore": 0x09, "reset": 0x0A, "go_off": 0x0B,
        "standby_plus": 0x11, "standby_minus": 0x12, "sequence_plus": 0x13,
        "sequence_minus": 0x14, "start_clock": 0x15, "stop_clock": 0x16,
        "zero_clock": 0x17, "set_clock": 0x18, "mtc_chase_on": 0x19,
        "mtc_chase_off": 0x1A, "open_cue_list": 0x1B, "close_cue_list": 0x1C,
        "open_cue_path": 0x1D, "close_cue_path": 0x1E,
        "standby": 0x20, "standing_by": 0x21, "go_2pc": 0x22,
        "complete": 0x23, "cancel": 0x24, "cancelled": 0x25, "abort": 0x26,
    },
    # Universal Non-Real Time 09 <code>. on = GM1 System On; gm2_on = GM2
    # System On (GM2 4.9.1).
    "gm_system": {"on": 0x01, "off": 0x02, "gm2_on": 0x03},
    # Universal Non-Real Time 06 <code>.
    "device_inquiry": {"request": 0x01, "reply": 0x02},
    # Universal Real Time 04 <code> (MIDI 1.0 Detailed Spec; CA-025; GM2 4.4).
    "device_control": {
        "master_volume": 0x01, "master_balance": 0x02,
        "master_fine_tuning": 0x03, "master_coarse_tuning": 0x04,
        "global_parameter_control": 0x05,
    },
    # Controller Destination Setting, Universal Real Time 09 <code> (CA-022).
    "controller_destination": {
        "channel_pressure": 0x01, "poly_pressure": 0x02, "control_change": 0x03,
    },
    # Control Change controller numbers.
    "channel_mode": {
        "all_sound_off": 120, "reset_all_controllers": 121,
        "local_control": 122, "all_notes_off": 123, "omni_off": 124,
        "omni_on": 125, "mono_on": 126, "poly_on": 127,
    },
    # MIDI Tuning 08 <code> (MIDI Tuning Updated Specification).
    "midi_tuning": {
        "bulk_dump_request": 0x00, "bulk_dump_reply": 0x01,
        "note_change": 0x02, "bulk_dump_request_bank": 0x03,
        "key_based_dump": 0x04, "scale_octave_dump_1byte": 0x05,
        "scale_octave_dump_2byte": 0x06, "note_change_bank": 0x07,
        "scale_octave_1byte": 0x08, "scale_octave_2byte": 0x09,
    },
    # Notation 03 <code>.
    "notation": {
        "bar_marker": 0x01, "time_signature_immediate": 0x02,
        "time_signature_delayed": 0x42,
    },
    # Real Time MTC Cueing 05 <code>.
    "mtc_cueing": {
        "special_system_stop": 0x00, "punch_in": 0x01, "punch_out": 0x02,
        "event_start": 0x05, "event_stop": 0x06,
        "event_start_with_info": 0x07, "event_stop_with_info": 0x08,
        "cue_point": 0x0B, "cue_point_with_info": 0x0C, "event_name": 0x0E,
    },
    # Non-Real Time MTC Cueing 04 <code>; every special uses 00.
    "mtc_cueing_nrt": {
        **{name: 0x00 for name in _MTC_CUEING_NRT_SPECIAL_TYPES},
        "punch_in": 0x01, "punch_out": 0x02,
        "delete_punch_in": 0x03, "delete_punch_out": 0x04,
        "event_start": 0x05, "event_stop": 0x06,
        "event_start_with_info": 0x07, "event_stop_with_info": 0x08,
        "delete_event_start": 0x09, "delete_event_stop": 0x0A,
        "cue_point": 0x0B, "cue_point_with_info": 0x0C,
        "delete_cue_point": 0x0D, "event_name": 0x0E,
    },
    # Sample Dump Standard: the sub-ID#1 (01-03), or 05 plus sub-ID#2 for
    # the Sample Dump Extensions. Its handshakes are the file_dump ones.
    "sample_dump": {
        "header": (0x01,), "data_packet": (0x02,), "request": (0x03,),
        "loop_points": (0x05, 0x01), "loop_points_request": (0x05, 0x02),
    },
    # File Dump 07 <code>, plus the handshakes.
    "file_dump": {
        "header": 0x01, "data_packet": 0x02, "request": 0x03,
        **_FILE_DUMP_HANDSHAKE,
    },
}

# MSC command_format names (RP-002/014).
_MSC_FORMATS = {
    "lighting": 0x01, "sound": 0x10, "machinery": 0x20, "video": 0x30,
    "projection": 0x40, "process_control": 0x50, "pyro": 0x60,
    "all_types": 0x7F,
}


def _command_enum() -> list:
    """Every command name in _COMMANDS, each once, grouped by type."""
    names: list = []
    for table in _COMMANDS.values():
        names += [name for name in table if name not in names]
    return names


TOOLS: list[dict] = [
    {
        "name": "midi1",
        "description": (
            "MIDI 1.0: device ports, building and decoding every "
            "MIDI 1.0 message, and .mid/.syx files. Actions: "
            "'list_devices': input and output port names. "
            "'open': open 'port_name' as 'direction' input or output; returns a "
            "'handle' ('opened_at' too for an input). "
            "'close': close a 'handle'. "
            "'send': send 'message' on an output handle. A message is a dict with "
            "'type' and that type's fields; 'describe' gives them and an example. "
            "rpn, nrpn, mtc_quarter_frame_sequence and an mmc with 'segment' send "
            "several wire messages, and 'sent' is then a list. "
            "'poll': the messages an input handle received since the last poll "
            "(up to 10,000 kept). Each has 'received_at' (epoch seconds), 'message' "
            "(text), 'hex' and 'decoded' (the dict 'send' takes; SysEx with no "
            "decoder is type 'sysex', MMC replies 'mmc_response'); 'completes' when "
            "it finishes an RPN/NRPN change, a Quarter Frame time or MMC segments; "
            "'at_open' and 'overflow' are explained under 'action'. "
            "'timeout_seconds' (0-60, default 0) waits for the first message. "
            "'describe': field documentation; see 'action'. "
            "'read_midi_file': summary of the .mid/.midi or .syx at 'path', with "
            "decoded messages up to 'max_messages' per track (default 100; 0 for "
            "counts only). "
            "'write_midi_file': create a .mid/.midi from 'tracks' or a .syx from "
            "'messages' at 'path', then read it back. Messages are 'send' dicts plus "
            "'time' (delta ticks), and in .mid tracks also the meta types with "
            "mido's fields (track_name 'name', set_tempo 'tempo' or 'bpm', "
            "time_signature, key_signature 'key', ...). An existing file is replaced "
            "only with 'overwrite': true. "
            "'decode_mmc_response': decode an MMC response given as its full SysEx "
            "bytes in 'data' (poll already decodes received ones). "
            "'run_clock': send MIDI Clock (24 per quarter note) at 'bpm' for "
            "'duration_seconds' on an output handle, for devices that follow "
            "external clock; one 'send' per clock is too slow and uneven. "
            "Handles last only as long as this process."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_devices", "open", "close", "send", "poll",
                        "read_midi_file", "write_midi_file",
                        "decode_mmc_response", "run_clock", "describe",
                    ],
                    "description": (
                        "Which operation. 'describe' with no 'message' lists every "
                        "message type; with 'message': {'type': T} it returns T's "
                        "fields and an example; add 'command' for one command, or "
                        "for mmc 'field' for one Information Field. In 'poll' "
                        "results, 'at_open': true marks the burst that arrives as an "
                        "input opens: usually what the device stored while the port "
                        "was closed (old playing, or a reply to a request sent "
                        "before the open); a live message within 20 ms of the open "
                        "is marked too. 'overflow': true marks where more than 2000 "
                        "events queued at once and messages were lost."
                    ),
                },
                "port_name": {
                    "type": "string",
                    "description": "'open': a port name as 'list_devices' gives it.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["input", "output"],
                    "description": "'open': input or output.",
                },
                "active_sensing": {
                    "type": "boolean",
                    "description": (
                        "'open' of an input: true passes received Active Sensing "
                        "(FE, about every 300 ms from a device that sends it) to "
                        "'poll'. Default false drops it."
                    ),
                },
                "handle": {
                    "type": "string",
                    "description": "From 'open'; for close, send, poll and run_clock.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        "'poll': 0-60, default 0 (return at once). Above 0, wait "
                        "until a message arrives or this many seconds pass."
                    ),
                },
                "bpm": {
                    "type": "number",
                    "description": "'run_clock', required: 20-300.",
                },
                "duration_seconds": {
                    "type": "number",
                    "description": (
                        "'run_clock', required: above 0, at most 120; the call "
                        "takes this long. Call again for longer."
                    ),
                },
                "transport": {
                    "type": "string",
                    "enum": ["start", "continue", "none"],
                    "description": (
                        "'run_clock': sent once before the clock, default 'start'; "
                        "'none' sends clock only."
                    ),
                },
                "stop_at_end": {
                    "type": "boolean",
                    "description": "'run_clock': send Stop after the clock, default true.",
                },
                "path": {
                    "type": "string",
                    "description": "'read_midi_file'/'write_midi_file': a .mid, .midi or .syx path.",
                },
                "max_messages": {
                    "type": "integer",
                    "description": (
                        "'read_midi_file': decoded messages per track (.mid) or in "
                        "total (.syx), default 100; 0 returns counts only."
                    ),
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "'write_midi_file': replace an existing file; default false.",
                },
                "midi_file_type": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": "'write_midi_file' .mid: default 1; type 0 takes one track.",
                },
                "ticks_per_beat": {
                    "type": "integer",
                    "description": "'write_midi_file' .mid: default 480.",
                },
                "tracks": {
                    "type": "array",
                    "description": (
                        "'write_midi_file' .mid, required: a list of "
                        "{'messages': [...]}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "messages": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                },
                "messages": {
                    "type": "array",
                    "description": (
                        "'write_midi_file' .syx, required: a list of "
                        "{'type': 'sysex', 'data': [...]}."
                    ),
                    "items": {"type": "object"},
                },
                "message": {
                    "type": "object",
                    "description": (
                        "'send', required: 'type' plus that type's fields (see "
                        "'describe'). 'describe': {'type', 'command'} or, for mmc, "
                        "{'type': 'mmc', 'field'}."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                # Channel messages
                                "note_on", "note_off", "control_change",
                                "program_change", "pitchwheel", "aftertouch",
                                "polytouch", "channel_mode",
                                # System Common
                                "quarter_frame", "songpos", "song_select",
                                "tune_request",
                                # System Real-Time
                                "clock", "start", "stop", "continue",
                                "active_sensing", "reset",
                                # System Exclusive, raw and typed
                                "sysex",
                                "mtc_full", "mtc_nak", "mtc_user_bits", "mmc", "msc",
                                "gm_system", "device_inquiry",
                                "device_control", "controller_destination",
                                "key_based_instrument_control",
                                "midi_tuning", "notation",
                                "mtc_cueing", "mtc_cueing_nrt", "sample_dump",
                                "file_dump",
                                # Several wire messages each; 'sent' is a list
                                "rpn", "nrpn", "mtc_quarter_frame_sequence",
                            ],
                        },
                        "command": {
                            "type": "string",
                            "enum": _command_enum(),
                            "description": (
                                "The sub-command for types that have one; the "
                                "valid names depend on 'type' ('describe' lists "
                                "them)."
                            ),
                        },
                    },
                },
                "data": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "'decode_mmc_response', required: the whole SysEx, F0 to "
                        "F7 included (unlike a 'sysex' message's 'data')."
                    ),
                },
            },
            "required": ["action"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MESSAGE_TYPES = TOOLS[0]["input_schema"]["properties"]["message"]["properties"]["type"]["enum"]

# handle -> (direction, port): an input or output port object of the
# _PORT_BACKEND backend (_AlsaInput/_AlsaOutput or _RtMidiInput/_RtMidiOutput).
# Process memory only.
_OPEN_PORTS: dict = {}
_HANDLE_COUNTER = itertools.count(1)

# Per input handle: a deque of (received_at, mido.Message or None for an
# overflow, at_open), an Event that _poll waits on, and a _StreamDecoder that
# _poll feeds in arrival order. _AlsaInput's thread stamps each entry with
# time.time() on arrival, and the callback _open gives it appends the entry
# and sets the Event. The deque drops its oldest entries past
# _INPUT_BUFFER_MAXLEN.
_INPUT_BUFFER_MAXLEN = 10_000
_INPUT_BUFFERS: dict = {}
_INPUT_EVENTS: dict = {}
_STREAM_DECODERS: dict = {}


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


def _err(message: str) -> str:
    return json.dumps({"error": message})


# execute() runs every action in a worker thread under asyncio.wait_for, so
# a hung driver or device can't block the caller forever. A timeout only ends
# the wait: Python can't kill the thread, so a stuck call holds its slot in
# the shared thread pool until the process exits. That's why poll's wait and
# run_clock's duration have caps.
_DEFAULT_TIMEOUT = 10.0

# Largest 'timeout_seconds' poll accepts.
_MAX_POLL_TIMEOUT = 60.0

# Extra time execute() allows past poll's own wait, so an empty wait returns
# normally instead of being reported as a hung driver.
_POLL_TIMEOUT_MARGIN = 2.0

# Largest 'duration_seconds' run_clock accepts. Call again for a longer run.
_MAX_CLOCK_DURATION = 120.0

# Extra time execute() allows past run_clock's duration, for the loop to
# finish and send Stop.
_CLOCK_TIMEOUT_MARGIN = 5.0


async def execute(name: str, tool_input: dict) -> str:
    if name != "midi1":
        return json.dumps({"error": f"unknown midi1 tool {name!r}"})

    effective_timeout = _DEFAULT_TIMEOUT
    action = tool_input.get("action")
    if action == "poll":
        requested = tool_input.get("timeout_seconds")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool):
            effective_timeout = max(
                _DEFAULT_TIMEOUT, requested + _POLL_TIMEOUT_MARGIN
            )
    elif action == "run_clock":
        requested = tool_input.get("duration_seconds")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool):
            effective_timeout = max(
                _DEFAULT_TIMEOUT, requested + _CLOCK_TIMEOUT_MARGIN
            )

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run, tool_input), timeout=effective_timeout
        )
    except TimeoutError:
        return _err(
            f"midi1 action {tool_input.get('action')!r} timed out after "
            f"{effective_timeout}s — a MIDI driver or device may be hung "
            "(the underlying blocking call could not be cancelled and may "
            "still be running in the background; see the note above "
            "_DEFAULT_TIMEOUT in core/midi1.py)"
        )


def _run(tool_input: dict) -> str:
    if mido is None:
        return _err(
            "mido is not installed — `pip install mido[ports-rtmidi]` "
            f"to use the midi1 tool ({_MIDO_IMPORT_ERROR})"
        )
    action = tool_input.get("action")
    if action == "list_devices":
        return _list_devices()
    if action == "open":
        return _open(tool_input)
    if action == "close":
        return _close(tool_input)
    if action == "send":
        return _send(tool_input)
    if action == "poll":
        return _poll(tool_input)
    if action == "read_midi_file":
        return _read_midi_file(tool_input)
    if action == "write_midi_file":
        return _write_midi_file(tool_input)
    if action == "decode_mmc_response":
        return _decode_mmc_response(tool_input)
    if action == "run_clock":
        return _run_clock(tool_input)
    if action == "describe":
        return _describe(tool_input)
    return _err(
        f"unknown action {action!r} — expected one of "
        "list_devices, open, close, send, poll, read_midi_file, "
        "write_midi_file, decode_mmc_response, run_clock, describe"
    )


def _list_devices() -> str:
    try:
        inputs = mido.get_input_names()
        outputs = mido.get_output_names()
    except Exception as e:
        return _err(f"device enumeration failed: {type(e).__name__}: {e}")
    return json.dumps({"status": "ok", "inputs": inputs, "outputs": outputs})


# The largest input pool the kernel gives a sequencer client
# (SNDRV_SEQ_MAX_CLIENT_EVENTS in include/sound/seq_kernel.h). A larger request
# is ignored without an error, so _AlsaInput reads the size back.
_ALSA_INPUT_POOL = 2000


def _alsa_port_address(port_name: str, direction: str = "input") -> tuple:
    """(full port name, client, port) for a port name as list_devices gives
    it, or a shorter form mido accepts ("client:port name")."""
    from mido.backends.rtmidi_utils import expand_alsa_port_name

    names = mido.get_input_names() if direction == "input" else mido.get_output_names()
    port_name = expand_alsa_port_name(names, port_name)
    found = re.search(r" (\d+):(\d+)$", port_name) if port_name in names else None
    if found is None:
        raise OSError(f"unknown port {port_name!r}")
    return port_name, int(found.group(1)), int(found.group(2))


# A device releases what it stored while its input was closed as one burst
# when the port opens. Measured 2026-10-02 (TR-8S, KeyStep 37, Hydrasynth DR):
# the first message 0.1-3.1 ms after the open, the rest about 0.01 ms apart,
# 655 messages within 4.2 ms. A message belongs to that burst if it arrives
# within _AT_OPEN_GAP of the open or of the burst's previous message. Timing
# can't tell a stored message from a live one arriving that soon, so a live
# message within the gap is marked too.
_AT_OPEN_GAP = 0.020


class _OpenBurst:
    """Decides, in arrival order, which messages belong to the burst at open.
    The first message more than _AT_OPEN_GAP after the open or after the
    previous burst message ends the burst."""

    def __init__(self, opened_at: float) -> None:
        self._last = opened_at
        self._running = True

    def member(self, received_at: float) -> bool:
        if self._running and received_at - self._last <= _AT_OPEN_GAP:
            self._last = received_at
            return True
        self._running = False
        return False


# POLLIN from <asm-generic/poll.h>; select.POLLIN doesn't exist on Windows,
# where this module also loads (it only uses ALSA on Linux).
_POLLIN = 0x0001


class _AlsaInput:
    """An input port as an ALSA sequencer client of its own, subscribed to
    the device's port, with an input pool of _ALSA_INPUT_POOL events. A
    thread waits on the client's poll descriptor, reads every queued event,
    turns it back into bytes (snd_midi_event_decode) and parses them with
    mido.Parser, which joins SysEx split across events. deliver gets
    (received_at, mido.Message, at_open) for each message, and
    (received_at, None, at_open) when the kernel reports the pool overflowed
    (it has then discarded everything queued). 'opened_at' is the time just
    before subscribing; _OpenBurst gives at_open."""

    def __init__(self, port_name: str, deliver, active_sensing: bool) -> None:
        if _ALSA is None:
            raise OSError(f"input ports need cffi and libasound.so.2 ({_ALSA_IMPORT_ERROR})")
        self.name, client, port = _alsa_port_address(port_name)
        ffi, lib = _ALSA_FFI, _ALSA
        handle = ffi.new("snd_seq_t **")
        # SND_SEQ_OPEN_INPUT = 2, SND_SEQ_NONBLOCK = 1
        rc = lib.snd_seq_open(handle, b"default", 2, 1)
        if rc < 0:
            raise OSError(f"snd_seq_open failed ({rc})")
        self._seq = handle[0]
        self._decoder = ffi.NULL
        try:
            lib.snd_seq_set_client_name(self._seq, b"midi1")
            pool = self._set_input_pool()
            if pool != _ALSA_INPUT_POOL:
                raise OSError(f"input pool is {pool} events, not {_ALSA_INPUT_POOL}")
            # caps WRITE | SUBS_WRITE; type MIDI_GENERIC | APPLICATION
            my_port = lib.snd_seq_create_simple_port(
                self._seq, b"input", (1 << 1) | (1 << 6), (1 << 1) | (1 << 20))
            if my_port < 0:
                raise OSError(f"snd_seq_create_simple_port failed ({my_port})")
            decoder = ffi.new("snd_midi_event_t **")
            if lib.snd_midi_event_new(0, decoder) < 0:
                raise OSError("snd_midi_event_new failed")
            self._decoder = decoder[0]
            lib.snd_midi_event_no_status(self._decoder, 1)  # full status bytes
            pfd = ffi.new("struct pollfd[1]")
            if lib.snd_seq_poll_descriptors(self._seq, pfd, 1, _POLLIN) != 1:
                raise OSError("snd_seq_poll_descriptors failed")
            self._fd = pfd[0].fd
            self._deliver = deliver
            self._active_sensing = active_sensing
            self.opened_at = time.time()
            self._burst = _OpenBurst(self.opened_at)
            self._stop = False
            self._thread = threading.Thread(target=self._read, daemon=True,
                                            name=f"midi1 input {self.name}")
            self._thread.start()
            # Subscribing opens the device's input; a backlog arrives now.
            self.opened_at = time.time()
            self._burst = _OpenBurst(self.opened_at)
            rc = lib.snd_seq_connect_from(self._seq, my_port, client, port)
            if rc < 0:
                raise OSError(f"can't subscribe to {client}:{port} ({rc})")
        except Exception:
            self.close()
            raise

    def _set_input_pool(self) -> int:
        lib, ffi = _ALSA, _ALSA_FFI
        lib.snd_seq_set_client_pool_input(self._seq, _ALSA_INPUT_POOL)
        info = ffi.new("snd_seq_client_pool_t **")
        if lib.snd_seq_client_pool_malloc(info) < 0:
            raise OSError("snd_seq_client_pool_malloc failed")
        try:
            if lib.snd_seq_get_client_pool(self._seq, info[0]) < 0:
                raise OSError("snd_seq_get_client_pool failed")
            return lib.snd_seq_client_pool_get_input_pool(info[0])
        finally:
            lib.snd_seq_client_pool_free(info[0])

    def _read(self) -> None:
        ffi, lib = _ALSA_FFI, _ALSA
        event = ffi.new("snd_seq_event_t **")
        size = 4096
        out = ffi.new("unsigned char[]", size)
        parser = mido.Parser()
        while not self._stop:
            select.select([self._fd], [], [], 0.1)  # also how often _stop is checked
            while not self._stop:
                rc = lib.snd_seq_event_input(self._seq, event)
                if rc == -errno.EAGAIN:
                    break
                if rc == -errno.ENOSPC:
                    parser = mido.Parser()  # a SysEx in progress is lost too
                    now = time.time()
                    self._deliver((now, None, self._burst.member(now)))
                    continue
                if rc < 0:
                    break
                n = lib.snd_midi_event_decode(self._decoder, out, size, event[0])
                while n == -errno.ENOMEM:  # a SysEx chunk larger than the buffer
                    size *= 4
                    out = ffi.new("unsigned char[]", size)
                    n = lib.snd_midi_event_decode(self._decoder, out, size, event[0])
                if n <= 0:
                    continue  # not a MIDI event (port subscribed, client start, ...)
                parser.feed(bytes(ffi.buffer(out, n)))
                for msg in parser:
                    if msg.type == "active_sensing" and not self._active_sensing:
                        continue
                    now = time.time()
                    self._deliver((now, msg, self._burst.member(now)))

    def close(self) -> None:
        self._stop = True
        thread = getattr(self, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if self._decoder != _ALSA_FFI.NULL:
            _ALSA.snd_midi_event_free(self._decoder)
            self._decoder = _ALSA_FFI.NULL
        if self._seq is not None:
            _ALSA.snd_seq_close(self._seq)
            self._seq = None


# SysEx goes out in events of at most this many bytes: far under alsa-lib's
# 16,384-byte output buffer (one event at least that size fails with -EINVAL)
# and under a device driver's rawmidi buffer, which refuses an event larger
# than its free space.
_ALSA_SYSEX_CHUNK = 256
# How long _AlsaOutput waits while a receiver's queue, or a device's output
# buffer, stays full.
_ALSA_SEND_PATIENCE = 5.0


def _rawmidi_output(card: int, port: int) -> "tuple[str, int] | None":
    """(/proc file, Output index) of the rawmidi output behind sequencer port
    `port` of sound card `card`, or None. snd-seq-midi numbers a card's
    ports across its rawmidi devices in device order, max(outputs, inputs)
    ports per device."""
    import glob

    paths = sorted(glob.glob(f"/proc/asound/card{card}/midi[0-9]*"),
                   key=lambda p: int(p.rsplit("midi", 1)[1]))
    first = 0
    for path in paths:
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            return None
        outputs = len(re.findall(r"^Output \d+$", text, re.MULTILINE))
        inputs = len(re.findall(r"^Input \d+$", text, re.MULTILINE))
        count = max(outputs, inputs)
        if port < first + count:
            index = port - first
            return (path, index) if index < outputs else None
        first += count
    return None


def _rawmidi_avail(path: str, index: int) -> "int | None":
    """Free bytes in an open rawmidi output's buffer ("Avail" in /proc), or
    None when not shown."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None
    block = re.search(rf"^Output {index}$((?:\n  .*)*)", text, re.MULTILINE)
    avail = re.search(r"Avail\s+:\s+(\d+)", block.group(1)) if block else None
    return int(avail.group(1)) if avail else None


class _AlsaOutput:
    """An output port as an ALSA sequencer client of its own, connected to
    the device's port. send() feeds a message's bytes to
    snd_midi_event_encode_byte, which emits an event per complete message
    and splits a long SysEx into _ALSA_SYSEX_CHUNK-byte SysEx events (ALSA's
    own transport for large SysEx; receivers join them up to F7). Events are
    sent direct, so a full receiving queue comes back as an error from the
    write; send() then waits and retries, for up to _ALSA_SEND_PATIENCE
    seconds without progress.

    Toward a hardware port the kernel's snd-seq-midi writes each event into
    the device's rawmidi buffer (4096 bytes by default) and drops one that
    doesn't fit, logging "seq_midi: MIDI output buffer overrun" but
    returning no error. So for a port backed by a sound card's rawmidi
    output, each event waits until that buffer's free space ("Avail" in
    /proc/asound) holds it."""

    def __init__(self, port_name: str) -> None:
        if _ALSA is None:
            raise OSError(f"output ports need cffi and libasound.so.2 ({_ALSA_IMPORT_ERROR})")
        self.name, client, port = _alsa_port_address(port_name, "output")
        ffi, lib = _ALSA_FFI, _ALSA
        handle = ffi.new("snd_seq_t **")
        rc = lib.snd_seq_open(handle, b"default", 1, 0)  # SND_SEQ_OPEN_OUTPUT, blocking
        if rc < 0:
            raise OSError(f"snd_seq_open failed ({rc})")
        self._seq = handle[0]
        self._encoder = ffi.NULL
        self._rawmidi: tuple[str, int] | None = None
        try:
            lib.snd_seq_set_client_name(self._seq, b"midi1")
            # caps READ | SUBS_READ; type MIDI_GENERIC | APPLICATION
            self._port = lib.snd_seq_create_simple_port(
                self._seq, b"output", (1 << 0) | (1 << 5), (1 << 1) | (1 << 20))
            if self._port < 0:
                raise OSError(f"snd_seq_create_simple_port failed ({self._port})")
            encoder = ffi.new("snd_midi_event_t **")
            if lib.snd_midi_event_new(_ALSA_SYSEX_CHUNK, encoder) < 0:
                raise OSError("snd_midi_event_new failed")
            self._encoder = encoder[0]
            rc = lib.snd_seq_connect_to(self._seq, self._port, client, port)
            if rc < 0:
                raise OSError(f"can't connect to {client}:{port} ({rc})")
            card = self._card_of(client)
            self._rawmidi = _rawmidi_output(card, port) if card >= 0 else None
        except Exception:
            self.close()
            raise

    def _card_of(self, client: int) -> int:
        """The sound card behind a sequencer client, or -1."""
        ffi, lib = _ALSA_FFI, _ALSA
        info = ffi.new("snd_seq_client_info_t **")
        if lib.snd_seq_client_info_malloc(info) < 0:
            return -1
        try:
            if lib.snd_seq_get_any_client_info(self._seq, client, info[0]) < 0:
                return -1
            return lib.snd_seq_client_info_get_card(info[0])
        finally:
            lib.snd_seq_client_info_free(info[0])

    def send(self, msg: "mido.Message") -> None:
        ffi, lib = _ALSA_FFI, _ALSA
        event = ffi.new("snd_seq_event_t *")
        pending = 0  # bytes fed into the event being built
        for byte in msg.bytes():
            pending += 1
            if lib.snd_midi_event_encode_byte(self._encoder, byte, event) == 1:
                self._deliver(event, pending)
                event = ffi.new("snd_seq_event_t *")
                pending = 0

    def _wait_for_room(self, size: int) -> None:
        """Wait until the device's rawmidi buffer has `size` bytes free."""
        if self._rawmidi is None:
            return
        last, since = None, time.monotonic()
        while True:
            avail = _rawmidi_avail(*self._rawmidi)
            if avail is None or avail >= size:
                return
            if avail != last:
                last, since = avail, time.monotonic()
            elif time.monotonic() - since > _ALSA_SEND_PATIENCE:
                raise OSError(
                    f"the device didn't take data: its output buffer stayed at "
                    f"{avail} free bytes for {_ALSA_SEND_PATIENCE:.0f} s"
                )
            time.sleep(0.001)

    def _deliver(self, event, size: int) -> None:
        """Send one encoded event (`size` MIDI bytes) to the subscribers,
        direct; retry while the receiver's queue is full (-ENOMEM, -EAGAIN)."""
        lib = _ALSA
        self._wait_for_room(size)
        event.source.port = self._port
        event.dest.client = 254  # SND_SEQ_ADDRESS_SUBSCRIBERS
        event.dest.port = 253  # SND_SEQ_ADDRESS_UNKNOWN
        event.queue = 253  # SND_SEQ_QUEUE_DIRECT
        rc = lib.snd_seq_event_output(self._seq, event)
        if rc < 0:
            raise OSError(f"snd_seq_event_output failed ({rc})")
        give_up = time.monotonic() + _ALSA_SEND_PATIENCE
        while True:
            rc = lib.snd_seq_drain_output(self._seq)
            if rc >= 0:
                return
            queue_full = rc in (-errno.ENOMEM, -errno.EAGAIN)
            if not queue_full or time.monotonic() > give_up:
                lib.snd_seq_drop_output(self._seq)
                reason = errno.errorcode.get(-rc, str(rc))
                if queue_full:
                    reason += f", still full after {_ALSA_SEND_PATIENCE:.0f} s"
                raise OSError(f"the receiver didn't accept the message ({reason})")
            time.sleep(0.002)

    def close(self) -> None:
        if self._encoder != _ALSA_FFI.NULL:
            _ALSA.snd_midi_event_free(self._encoder)
            self._encoder = _ALSA_FFI.NULL
        if self._seq is not None:
            _ALSA.snd_seq_close(self._seq)
            self._seq = None


class _RtMidiInput:
    """An input port opened with rtmidi.MidiIn directly (the "rtmidi"
    backend: CoreMIDI on macOS, WinMM on Windows). mido's rtmidi Input
    always drops Active Sensing; this does the same open and parse
    (mido.Message.from_bytes on each complete message rtmidi delivers) with
    the filter chosen here. deliver gets (received_at, mido.Message, at_open)
    as _AlsaInput's does; rtmidi reports no queue overflow, so no overflow
    entries come from this backend."""

    def __init__(self, port_name: str, deliver, active_sensing: bool) -> None:
        import rtmidi
        from mido.backends.rtmidi_utils import expand_alsa_port_name

        self._rt = rtmidi.MidiIn()
        try:
            names = self._rt.get_ports()
            if self._rt.get_current_api() == rtmidi.API_LINUX_ALSA:
                port_name = expand_alsa_port_name(names, port_name)
            if port_name not in names:
                raise OSError(f"unknown port {port_name!r}")
            self._rt.ignore_types(sysex=False, timing=False,
                                  active_sense=not active_sensing)
            self._deliver = deliver
            self.opened_at = time.time()
            self._burst = _OpenBurst(self.opened_at)
            # Before open_port, so nothing waits in rtmidi's own queue.
            self._rt.set_callback(self._callback)
            self._rt.open_port(names.index(port_name))
        except Exception:
            self._rt.delete()
            raise
        self.name = port_name

    def _callback(self, event, _data) -> None:
        try:
            msg = mido.Message.from_bytes(event[0])
        except ValueError:
            return  # not a complete MIDI message; mido drops these too
        now = time.time()
        self._deliver((now, msg, self._burst.member(now)))

    def close(self) -> None:
        self._rt.cancel_callback()
        self._rt.close_port()
        self._rt.delete()


class _RtMidiOutput:
    """An output port opened with rtmidi.MidiOut directly (the "rtmidi"
    backend). send() hands each message's bytes to send_message as they
    are; how a large SysEx and a full receiver are handled is up to the
    platform's MIDI system."""

    def __init__(self, port_name: str) -> None:
        import rtmidi
        from mido.backends.rtmidi_utils import expand_alsa_port_name

        self._rt = rtmidi.MidiOut()
        try:
            names = self._rt.get_ports()
            if self._rt.get_current_api() == rtmidi.API_LINUX_ALSA:
                port_name = expand_alsa_port_name(names, port_name)
            if port_name not in names:
                raise OSError(f"unknown port {port_name!r}")
            self._rt.open_port(names.index(port_name))
        except Exception:
            self._rt.delete()
            raise
        self.name = port_name

    def send(self, msg: "mido.Message") -> None:
        self._rt.send_message(msg.bytes())

    def close(self) -> None:
        self._rt.close_port()
        self._rt.delete()


def _open(tool_input: dict) -> str:
    port_name = tool_input.get("port_name")
    direction = tool_input.get("direction")
    if not port_name:
        return _err("'port_name' is required for 'open'")
    if direction not in ("input", "output"):
        return _err("'direction' must be 'input' or 'output' for 'open'")
    active_sensing = tool_input.get("active_sensing", False)
    if not isinstance(active_sensing, bool):
        return _err(f"'active_sensing' must be true or false, got {active_sensing!r}")
    if active_sensing and direction != "input":
        return _err("'active_sensing' applies only to an input port")

    handle = f"midi1-{next(_HANDLE_COUNTER)}"

    port: _AlsaInput | _AlsaOutput | _RtMidiInput | _RtMidiOutput
    input_class = _AlsaInput if _PORT_BACKEND == "alsa" else _RtMidiInput
    output_class = _AlsaOutput if _PORT_BACKEND == "alsa" else _RtMidiOutput
    try:
        if direction == "input":
            # Runs on _AlsaInput's thread. deque append/popleft and Event
            # set/wait/clear need no lock with one producer and one consumer.
            buf: deque = deque(maxlen=_INPUT_BUFFER_MAXLEN)
            event = threading.Event()

            def _deliver(entry: tuple, _buf: deque = buf, _event: threading.Event = event) -> None:
                _buf.append(entry)
                _event.set()

            port = input_class(port_name, _deliver, active_sensing)
            _INPUT_BUFFERS[handle] = buf
            _INPUT_EVENTS[handle] = event
            _STREAM_DECODERS[handle] = _StreamDecoder()
        else:
            port = output_class(port_name)
    except Exception as e:
        _INPUT_BUFFERS.pop(handle, None)
        _INPUT_EVENTS.pop(handle, None)
        _STREAM_DECODERS.pop(handle, None)
        return _err(
            f"failed to open {direction} port {port_name!r}: "
            f"{type(e).__name__}: {e}"
        )

    _OPEN_PORTS[handle] = (direction, port)
    result = {"status": "ok", "handle": handle, "direction": direction, "port_name": port_name}
    if isinstance(port, (_AlsaInput, _RtMidiInput)):
        result["opened_at"] = port.opened_at
    return json.dumps(result)


def _close(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.pop(handle, None) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    _, port = entry
    # Output handles have no buffer entries; pop(..., None) covers both.
    _INPUT_BUFFERS.pop(handle, None)
    _INPUT_EVENTS.pop(handle, None)
    _STREAM_DECODERS.pop(handle, None)
    try:
        port.close()
    except Exception as e:
        return _err(f"error closing handle {handle!r}: {type(e).__name__}: {e}")
    return json.dumps({"status": "ok", "handle": handle, "closed": True})


def close_all() -> None:
    """Close every open port; called by local_tools.shutdown(). Safe when
    nothing is open. A failure on one port doesn't stop the others.
    """
    for handle in list(_OPEN_PORTS):
        _, port = _OPEN_PORTS.pop(handle)
        _INPUT_BUFFERS.pop(handle, None)
        _INPUT_EVENTS.pop(handle, None)
        _STREAM_DECODERS.pop(handle, None)
        try:
            port.close()
        except Exception as e:
            print(f"[midi1] close_all: failed to close {handle!r} (ignored): {e}")


# Hour byte 0yyzzzzz (yy = frame rate, zzzzz = hours), shared by every
# SMPTE-style time field in this file: MTC Full Message, MMC Standard Time
# Code, MSC time, and MTC Cueing time.
_FRAME_RATE_BITS = {"24": 0b00, "25": 0b01, "30drop": 0b10, "30nondrop": 0b11}


def _encode_smpte_hour_byte(hours: int, frame_rate: str) -> int:
    _check_range("hours", hours, 0, 23)
    if not isinstance(frame_rate, str) or frame_rate not in _FRAME_RATE_BITS:
        raise ValueError(
            f"'frame_rate' must be one of {sorted(_FRAME_RATE_BITS)}, "
            f"got {frame_rate!r}"
        )
    return (_FRAME_RATE_BITS[frame_rate] << 5) | hours


def _encode_standard_speed(speed: float, reverse: bool) -> tuple:
    """MMC Standard Speed (RP-013 p.10): 3 bytes sh sm sl, used by
    VARIABLE PLAY, SEARCH and SHUTTLE. `speed` is the play-speed multiple
    (0 to ~1023.99); `reverse` sets the sign bit.

    sh = 0 g sss ppp (g = sign, sss = shift 0-7, ppp = top 3 bits of a
    17-bit magnitude); sm/sl = the middle and low 7 bits. The magnitude is
    round(speed * 2**(14 - sss)). The smallest sss that fits is used, for
    the most precision.
    """
    if _as_number("speed", speed) < 0:
        raise ValueError(
            f"'speed' must be >= 0 (use 'reverse' for direction), "
            f"got {speed!r}"
        )
    raw = None
    shift = None
    for candidate_shift in range(8):
        candidate_raw = round(speed * (2 ** (14 - candidate_shift)))
        if candidate_raw <= 0x1FFFF:
            raw = candidate_raw
            shift = candidate_shift
            break
    if raw is None or shift is None:
        raise ValueError(f"'speed' out of range (max ~1023.99), got {speed!r}")
    ppp = (raw >> 14) & 0x7
    sm = (raw >> 7) & 0x7F
    sl = raw & 0x7F
    sh = (0x40 if reverse else 0x00) | (shift << 3) | ppp
    return sh, sm, sl


# MMC Information Field names (RP-013 pp.14-16, every field in its index).
# The name byte sets the data length (RP-013 p.9): 01-1F 5 bytes (Standard
# Time Code), 20-3F 2 bytes (Short Time Code: the 'short_' form of 01-0F),
# 40-77 a <count> then that many bytes. Used by the mmc commands and by
# _mmc_response_fields.
_INFO_FIELD_NAMES = {
    "selected_time_code": 0x01,
    "selected_master_code": 0x02,
    "requested_offset": 0x03,
    "actual_offset": 0x04,
    "lock_deviation": 0x05,
    "generator_time_code": 0x06,
    "midi_time_code_input": 0x07,
    "gp0": 0x08,
    "gp1": 0x09,
    "gp2": 0x0A,
    "gp3": 0x0B,
    "gp4": 0x0C,
    "gp5": 0x0D,
    "gp6": 0x0E,
    "gp7": 0x0F,
    # Short Time Code forms of 01-0F (frames + subframes/status only).
    "short_selected_time_code": 0x21,
    "short_selected_master_code": 0x22,
    "short_requested_offset": 0x23,
    "short_actual_offset": 0x24,
    "short_lock_deviation": 0x25,
    "short_generator_time_code": 0x26,
    "short_midi_time_code_input": 0x27,
    "short_gp0": 0x28, "short_gp1": 0x29, "short_gp2": 0x2A, "short_gp3": 0x2B,
    "short_gp4": 0x2C, "short_gp5": 0x2D, "short_gp6": 0x2E, "short_gp7": 0x2F,
    # Count-prefixed fields.
    "signature": 0x40,
    "update_rate": 0x41,
    "command_error": 0x43,
    "command_error_level": 0x44,
    "time_standard": 0x45,
    "selected_time_code_source": 0x46,
    "selected_time_code_userbits": 0x47,
    "motion_control_tally": 0x48,
    "velocity_tally": 0x49,
    "stop_mode": 0x4A,
    "fast_mode": 0x4B,
    "record_mode": 0x4C,
    "record_status": 0x4D,
    "global_monitor": 0x50,
    "record_monitor": 0x51,
    "step_length": 0x54,
    "play_speed_reference": 0x55,
    "fixed_speed": 0x56,
    "lifter_defeat": 0x57,
    "control_disable": 0x58,
    "resolved_play_mode": 0x59,
    "chase_mode": 0x5A,
    "generator_command_tally": 0x5B,
    "generator_set_up": 0x5C,
    "generator_userbits": 0x5D,
    "midi_time_code_command_tally": 0x5E,
    "midi_time_code_set_up": 0x5F,
    "procedure_response": 0x60,
    "event_response": 0x61,
    "vitc_insert_enable": 0x63,
    "failure": 0x65,
    # Standard Track Bitmap fields (RP-013 p.17): <count> <bitmap bytes...>,
    # one bit per track. Reachable through read/update (whole field) and
    # masked_write. 'write' doesn't support them (see _WRITEABLE_INFO_FIELDS).
    "track_record_status": 0x4E,
    "track_record_ready": 0x4F,
    "track_sync_monitor": 0x52,
    "track_input_monitor": 0x53,
    "track_mute": 0x62,
}

# Response-only names (RP-013): RESPONSE ERROR lists fields the device can't
# supply; RESPONSE SEGMENT splits a long response. Neither can be READ.
_MMC_RESPONSE_ONLY_NAMES = {"response_error": 0x42, "response_segment": 0x64}

# Handshakes in a response string (RP-013): no data.
_MMC_RESPONSE_HANDSHAKES = {"wait": 0x7C, "resume": 0x7F}

# Read/Writeable Standard Time Code fields: valid destinations for write/
# move/add/subtract/drop_frame_adjust. Sources may be any registered name.
# Track Bitmap fields stay out even when writeable, because those commands
# encode the 5-byte time code format only.
_WRITEABLE_INFO_FIELDS = frozenset({
    "selected_time_code", "requested_offset", "generator_time_code",
    "gp0", "gp1", "gp2", "gp3", "gp4", "gp5", "gp6", "gp7",
})

# Fields in Standard Track Bitmap format; _decode_mmc_response branches on
# this.
_TRACK_BITMAP_INFO_FIELDS = frozenset({
    "track_record_status", "track_record_ready", "track_sync_monitor",
    "track_input_monitor", "track_mute",
})

# Valid masked_write targets: the writeable Track Bitmap fields (RP-013
# MASKED WRITE note 1). TRACK_RECORD_STATUS is read-only.
_MASK_WRITEABLE_INFO_FIELDS = frozenset({
    "track_record_ready", "track_sync_monitor", "track_input_monitor",
    "track_mute",
})


def _resolve_info_field_name(
    name: str,
    *,
    require_writeable: bool = False,
    require_mask_writeable: bool = False,
) -> int:
    """Return the name byte for an MMC Information Field name.

    require_writeable: the field must be in _WRITEABLE_INFO_FIELDS (a
    destination for write/move/add/subtract/drop_frame_adjust).
    require_mask_writeable: the field must be in
    _MASK_WRITEABLE_INFO_FIELDS (a masked_write target).
    """
    if _as_text('Information Field name', name) not in _INFO_FIELD_NAMES:
        raise ValueError(
            f"unknown Information Field name {name!r}; must be one of "
            f"{sorted(_INFO_FIELD_NAMES)}"
        )
    if require_writeable and name not in _WRITEABLE_INFO_FIELDS:
        raise ValueError(
            f"Information Field {name!r} is read-only; valid destinations "
            f"are {sorted(_WRITEABLE_INFO_FIELDS)}"
        )
    if require_mask_writeable and name not in _MASK_WRITEABLE_INFO_FIELDS:
        raise ValueError(
            f"Information Field {name!r} is not mask-writeable; valid "
            f"targets are {sorted(_MASK_WRITEABLE_INFO_FIELDS)}"
        )
    return _INFO_FIELD_NAMES[name]


def _encode_nested_mmc_command(
    nested: dict,
    *,
    forbid_assemble: bool = False,
    forbid_define: bool = False,
    forbid_execute_name: "int | None" = None,
) -> tuple:
    """Encode an mmc command dict for use inside PROCEDURE [ASSEMBLE] or
    EVENT [DEFINE] (RP-013 pp.34-37).

    Returns _mmc_command_bytes(nested): the command without the
    `7F <device_id> 06` prefix, which the enclosing message writes once.

    Nesting rules from the spec:
    - forbid_assemble: no nested PROCEDURE [ASSEMBLE] (both callers).
    - forbid_define: no nested EVENT [DEFINE] (EVENT [DEFINE] only).
    - forbid_execute_name: no nested PROCEDURE [EXECUTE] of the procedure
      being assembled (PROCEDURE [ASSEMBLE] only).
    """
    if _as_dict("nested command", nested).get("type") != "mmc":
        raise ValueError(
            f"nested commands must be type 'mmc', got "
            f"{nested.get('type')!r}"
        )
    nested_command = nested.get("command")
    if (
        forbid_assemble
        and nested_command == "procedure"
        and nested.get("action") == "assemble"
    ):
        raise ValueError(
            "a nested command cannot be another PROCEDURE [ASSEMBLE] "
            "(nested/recursive ASSEMBLE is not permitted)"
        )
    if (
        forbid_define
        and nested_command == "event"
        and nested.get("action") == "define"
    ):
        raise ValueError(
            "a nested command cannot be another EVENT [DEFINE] "
            "(nested/recursive DEFINE is not permitted)"
        )
    if (
        forbid_execute_name is not None
        and nested_command == "procedure"
        and nested.get("action") == "execute"
        and nested.get("procedure") == forbid_execute_name
    ):
        raise ValueError(
            f"a PROCEDURE [ASSEMBLE] cannot nest a PROCEDURE [EXECUTE] "
            f"naming the SAME procedure ({forbid_execute_name!r}) "
            f"currently being assembled (recursive EXECUTE)"
        )
    return _mmc_command_bytes(nested)


def _encode_standard_time_code(
    hours: int,
    minutes: int,
    seconds: int,
    frames: int,
    frame_rate: str,
    *,
    subframes: int = 0,
    color_frame: bool = False,
    blank: bool = False,
    negative: bool = False,
    use_status_byte: bool = False,
    estimated: bool = False,
    invalid: bool = False,
    video_field_1: bool = False,
    no_time_code: bool = False,
) -> tuple:
    """MMC Standard Time Code (RP-013 section 3), 5 bytes: the format of
    Information Fields 01h-0Fh. With the flags off it is also the MTC, MSC
    and MTC Cueing time layout (see _time_code_bytes).

    hr = 0 tt hhhhh  (_encode_smpte_hour_byte)
    mn = 0 c mmmmmm  (c = color frame)
    sc = 0 k ssssss  (k = blank: never loaded since power-up or MMC RESET)
    fr = 0 g i fffff (g = sign; i selects the 5th byte's format)
    5th byte: subframes 0-99 when i=0, or status flags 0 e v d n 000
    (estimated, invalid, video field 1, no time code) when i=1.
    All flags default to off.
    """
    _check_range("minutes", minutes, 0, 59)
    _check_range("seconds", seconds, 0, 59)
    _check_range("frames", frames, 0, 29)
    hr_byte = _encode_smpte_hour_byte(hours, frame_rate)
    mn_byte = (0x40 if color_frame else 0x00) | minutes
    sc_byte = (0x40 if blank else 0x00) | seconds
    fr_byte = (
        (0x40 if negative else 0x00)
        | (0x20 if use_status_byte else 0x00)
        | frames
    )
    if use_status_byte:
        fifth_byte = (
            (0x40 if estimated else 0x00)
            | (0x20 if invalid else 0x00)
            | (0x10 if video_field_1 else 0x00)
            | (0x08 if no_time_code else 0x00)
        )
    else:
        _check_range("subframes", subframes, 0, 99)
        fifth_byte = subframes
    return hr_byte, mn_byte, sc_byte, fr_byte, fifth_byte


def _decode_short_time_code(fr_byte: int, fifth_byte: int) -> dict:
    """MMC Short Time Code (RP-013 section 3): the fr and 5th bytes of
    Standard Time Code alone."""
    return {
        k: v for k, v in _decode_standard_time_code(0, 0, 0, fr_byte, fifth_byte).items()
        if k not in ("hours", "frame_rate", "color_frame", "minutes", "blank", "seconds")
    }


def _decode_standard_time_code(
    hr_byte: int, mn_byte: int, sc_byte: int, fr_byte: int, fifth_byte: int
) -> dict:
    """Decode MMC Standard Time Code; inverse of
    _encode_standard_time_code.
    """
    frame_rate_names = {v: k for k, v in _FRAME_RATE_BITS.items()}
    result: dict = {
        "hours": hr_byte & 0x1F,
        "frame_rate": frame_rate_names[(hr_byte >> 5) & 0x3],
        "color_frame": bool(mn_byte & 0x40),
        "minutes": mn_byte & 0x3F,
        "blank": bool(sc_byte & 0x40),
        "seconds": sc_byte & 0x3F,
        "negative": bool(fr_byte & 0x40),
        "frames": fr_byte & 0x1F,
    }
    use_status_byte = bool(fr_byte & 0x20)
    result["use_status_byte"] = use_status_byte
    if use_status_byte:
        result["estimated"] = bool(fifth_byte & 0x40)
        result["invalid"] = bool(fifth_byte & 0x20)
        result["video_field_1"] = bool(fifth_byte & 0x10)
        result["no_time_code"] = bool(fifth_byte & 0x08)
    else:
        result["subframes"] = fifth_byte
    return result


def _time_code_bytes(
    message: dict, subframe_field: "str | None" = None,
    context: "str | None" = None,
) -> tuple:
    """Read the time fields from `message` and encode them as Standard Time
    Code (_encode_standard_time_code, flags off): hr mn sc fr, plus the 5th
    byte when `subframe_field` names the field that holds it. MTC, MSC and
    MTC Cueing times all use this layout.
    """
    extra = (subframe_field,) if subframe_field else ()
    hours, minutes, seconds, frames, frame_rate, *rest = _time_code_fields(
        message, *extra, context=context,
    )
    encoded = _encode_standard_time_code(
        hours, minutes, seconds, frames, frame_rate,
        subframes=rest[0] if rest else 0,
    )
    return encoded if subframe_field else encoded[:4]


def _additional_info_bytes(message: dict, command: str) -> list:
    """MTC Cueing additional info, before nibblizing: the bytes of
    'additional_info_message' (built with _build_message) or the raw
    'additional_info_bytes'. Exactly one of the two."""
    info_message = message.get("additional_info_message")
    raw_bytes = message.get("additional_info_bytes")
    if info_message is not None and raw_bytes is not None:
        raise ValueError(
            "specify only ONE of 'additional_info_message' or "
            "'additional_info_bytes', not both"
        )
    if raw_bytes is not None:
        return _as_list("additional_info_bytes", raw_bytes)
    if info_message is None:
        raise KeyError(
            f"'additional_info_message' (or 'additional_info_bytes') — "
            f"required for {command!r}"
        )
    return _build_message(_as_dict("additional_info_message", info_message)).bytes()


def _cueing_event(message: dict, command: str) -> tuple:
    """sl sm <additional info> for both MTC Cueing types: the 14-bit event
    number, then nibblized ASCII for event_name or a nibblized MIDI message
    for the *_with_info commands."""
    sl, sm = _split14("event_number", _required(message, "event_number"))
    if command == "event_name":
        name = _required(message, "event_name", command)
        info = _nibblize(_ascii("event_name", name))
    elif command.endswith("_with_info"):
        info = _nibblize(_additional_info_bytes(message, command))
    else:
        info = ()
    return (sl, sm, *info)


def _decode_track_bitmap(bitmap) -> dict:
    """Standard Track Bitmap (RP-013 section 3). Byte 0 is 0 g f e d c b a:
    a = video, b reserved, c = time code track, d = aux track A, e = aux
    track B, f = track 1, g = track 2. Byte n (n >= 1) holds tracks
    7n-4 .. 7n+2 in bits 0-6."""
    first = bitmap[0] if bitmap else 0
    tracks = [t for t, bit in ((1, 5), (2, 6)) if first & (1 << bit)]
    tracks += [
        3 + 7 * (index - 1) + bit
        for index, byte in enumerate(bitmap) if index >= 1
        for bit in range(7) if byte & (1 << bit)
    ]
    return {
        "video": bool(first & 0x01), "time_code_track": bool(first & 0x04),
        "aux_track_a": bool(first & 0x08), "aux_track_b": bool(first & 0x10),
        "active_tracks": tracks,
    }


# --- MMC count-prefixed Information Field data (RP-013 section 6) --------------
# Single-byte fields: name -> {value name: code}, or None for a plain number.
# "local" (7F) is "as selected/defined locally", which RP-013 allows only in a
# WRITE.
_MMC_BYTE_FIELDS = {
    "update_rate": None,  # minimum frames between UPDATE cycles (default 1)
    "command_error_level": {"all_disabled": 0x00, "all_enabled": 0x7F},
    "selected_time_code_source": {
        "ltc": 0x00, "vitc": 0x01, "tape_counter": 0x02, "auto_vitc_ltc": 0x04,
        "local": 0x7F,
    },
    "stop_mode": {"disable_monitoring": 0x00, "enable_monitoring": 0x01, "local": 0x7F},
    "fast_mode": {"no_monitoring": 0x00, "with_monitoring": 0x01, "local": 0x7F},
    "record_mode": {
        "disabled": 0x00, "record_insert": 0x01, "record_assemble": 0x02,
        "rehearse": 0x04, "record_crash": 0x05, "local": 0x7F,
    },
    "global_monitor": {
        "playback_sync": 0x00, "input": 0x01, "playback_repro": 0x02, "local": 0x7F,
    },
    "record_monitor": {
        "record_only": 0x00, "record_or_non_play": 0x01,
        "record_or_record_ready": 0x02, "local": 0x7F,
    },
    "step_length": None,  # in 1/100 frame (default 32h, half a frame)
    "play_speed_reference": {"internal": 0x00, "external": 0x01, "local": 0x7F},
    "fixed_speed": {"lower": 0x3F, "standard": 0x40, "higher": 0x41, "local": 0x7F},
    "lifter_defeat": {"no_defeat": 0x00, "defeat": 0x01, "local": 0x7F},
    "control_disable": {"enable": 0x00, "disable": 0x01, "local": 0x7F},
    "resolved_play_mode": {"normal": 0x00, "free_resolve": 0x01, "local": 0x7F},
    "chase_mode": {"absolute_standard": 0x00, "absolute_resolve": 0x01, "local": 0x7F},
}

# RECORD STATUS activity nibble (0 d c b aaaa).
_MMC_RECORD_ACTIVITY = {
    "none": 0x0, "record_insert": 0x1, "record_assemble": 0x2, "rehearsing": 0x4,
    "record_crash": 0x5, "record_pause": 0x6,
}
_MMC_VITC_CONTROL = {"disable": 0x00, "enable": 0x01, "local": 0x7F}


def _named_value(entry: dict, field: str, names: "dict | None") -> int:
    """A byte given as a name from `names` or a 0-127 number."""
    value = _required(entry, field, entry.get("name"))
    if isinstance(value, str) and names:
        return names[_choice(entry, field, names, entry.get("name", field))]
    return _check_range(field, value, 0, 127)


def _value_name(names: "dict | None", code: int):
    return _name_for(names, code) if names and code in names.values() else code


def _byte_field_codec(names: "dict | None"):
    def encode(entry: dict) -> tuple:
        return (_named_value(entry, "value", names),)

    def decode(data) -> dict:
        (code,) = data
        return {"value": _value_name(names, code)}
    return encode, decode


def _encode_track_bitmap(entry: dict) -> tuple:
    """Standard Track Bitmap for a WRITE: 'bitmap_bytes' as is, or built from
    'active_tracks' and the video/time_code_track/aux_track_a/aux_track_b
    flags (layout in _decode_track_bitmap). Trailing zero bytes are left
    out; tracks not sent are reset (RP-013 section 3)."""
    if entry.get("bitmap_bytes") is not None:
        return tuple(_check_range("bitmap_bytes entry", b, 0, 127)
                     for b in entry["bitmap_bytes"])
    out = [0]
    for flag, bit in (("video", 0), ("time_code_track", 2), ("aux_track_a", 3),
                      ("aux_track_b", 4)):
        if entry.get(flag):
            out[0] |= 1 << bit
    for track in _as_list("active_tracks", entry.get("active_tracks", [])):
        _check_range("active_tracks entry", track, 1, 317)
        if track <= 2:
            out[0] |= 1 << (track + 4)
            continue
        index, bit = 1 + (track - 3) // 7, (track - 3) % 7
        out += [0] * (index + 1 - len(out))
        out[index] |= 1 << bit
    while out and out[-1] == 0:
        out.pop()
    return tuple(out)


def _decode_record_status(data) -> dict:
    (status,) = data
    return {
        "activity": _value_name(_MMC_RECORD_ACTIVITY, status & 0x0F),
        "local_record_inhibit": bool(status & 0x10),
        "local_rehearse_inhibit": bool(status & 0x20),
        "no_tracks_active": bool(status & 0x40),
    }


def _encode_time_standard(entry: dict) -> tuple:
    """TIME STANDARD. RP-013 p.55 defines the byte as 0 tt 00000 (tt = the
    frame-rate code of _FRAME_RATE_BITS). Its appendix example (p.82) sends
    03 for "30 frame", the code unshifted; 'encoding': "unshifted" sends
    that form."""
    code = _FRAME_RATE_BITS[_choice(entry, "frame_rate", _FRAME_RATE_BITS, "time_standard")]
    encoding = entry.get("encoding", "field_definition")
    if encoding not in ("field_definition", "unshifted"):
        raise ValueError(f"'encoding' must be 'field_definition' or 'unshifted', got {encoding!r}")
    return (code,) if encoding == "unshifted" else (code << 5,)


def _decode_time_standard(data) -> dict:
    (value,) = data
    if not value & 0x1F:
        return {"frame_rate": _name_for(_FRAME_RATE_BITS, (value >> 5) & 0x3)}
    if value <= 0x03:
        return {"frame_rate": _name_for(_FRAME_RATE_BITS, value), "encoding": "unshifted"}
    raise ValueError(f"TIME STANDARD byte {value:#04x} is neither 0 tt 00000 nor 00-03")


def _encode_vitc_insert(entry: dict) -> tuple:
    def line(field: str) -> int:
        value = _required(entry, field, "vitc_insert_enable")
        return 0x7F if value == "local" else _check_range(field, value, 0, 127)
    return (_named_value(entry, "control", _MMC_VITC_CONTROL),
            line("first_line"), line("second_line"))


def _decode_vitc_insert(data) -> dict:
    control, first, second = data
    return {
        "control": _value_name(_MMC_VITC_CONTROL, control),
        "first_line": "local" if first == 0x7F else first,
        "second_line": "local" if second == 0x7F else second,
    }


# field name -> (encode(entry) -> data bytes or None if read only,
#                decode(data) -> fields). Data excludes the name and count.
_MMC_FIELD_CODECS = {
    **{name: _byte_field_codec(names) for name, names in _MMC_BYTE_FIELDS.items()},
    "time_standard": (_encode_time_standard, _decode_time_standard),
    "record_status": (None, _decode_record_status),
    "vitc_insert_enable": (_encode_vitc_insert, _decode_vitc_insert),
    **{name: (_encode_track_bitmap if name in _MASK_WRITEABLE_INFO_FIELDS else None,
              lambda data: {"byte_count": len(data), "bitmap_bytes": list(data),
                            **_decode_track_bitmap(data)})
       for name in _TRACK_BITMAP_INFO_FIELDS},
}


# MMC structured, mostly read-only fields (RP-013 section 6).

def _mmc_code_names(codes, names: dict) -> list:
    """Code numbers as names from a name -> code table, else "0xNN"."""
    by_code = {v: k for k, v in names.items()}
    return [by_code.get(code, f"0x{code:02X}") for code in codes]


def _signature_bitmap_codes(bitmaps) -> tuple:
    """Codes set in a SIGNATURE bitmap array (RP-013 p.48): each run of 20
    bitmaps covers codes 00-7F in four 32-code blocks of 7, 7, 7, 7 and 4
    bits; bitmaps 0-19 are the basic set, 20-39 the 00 xx extension set,
    40-59 the 00 00 xx set. Returns (basic codes, extended codes as
    "00 xx" / "00 00 xx" strings)."""
    basic, extended = [], []
    for index, byte in enumerate(bitmaps):
        level, position = divmod(index, 20)
        block, slot = divmod(position, 5)
        base = 32 * block + 7 * slot
        for bit in range(4 if slot == 4 else 7):
            if byte >> bit & 1:
                code = base + bit
                if level == 0:
                    basic.append(code)
                else:
                    extended.append(" ".join(["00"] * level + [f"{code:02X}"]))
    return basic, extended


def _decode_signature(data) -> dict:
    vi, vf, va, vb, count_1 = data[0], data[1], data[2], data[3], data[4]
    commands = data[5:5 + count_1]
    count_2 = data[5 + count_1]
    responses = data[6 + count_1:6 + count_1 + count_2]
    if len(commands) != count_1 or len(responses) != count_2 or \
            len(data) != 6 + count_1 + count_2:
        raise ValueError("SIGNATURE counts don't match its data")
    command_codes, extended_commands = _signature_bitmap_codes(commands)
    field_codes, extended_fields = _signature_bitmap_codes(responses)
    field_names = {**_INFO_FIELD_NAMES, **_MMC_RESPONSE_ONLY_NAMES,
                   **_MMC_RESPONSE_HANDSHAKES, "extension": 0x00}
    return {
        "version": f"{vi}.{vf:02d}", "version_extension": [va, vb],
        "commands": _mmc_code_names(command_codes, {**_COMMANDS["mmc"], "extension": 0x00}),
        "fields": _mmc_code_names(field_codes, field_names),
        "extended_commands": extended_commands, "extended_fields": extended_fields,
        "command_bitmaps": list(commands), "field_bitmaps": list(responses),
    }


# COMMAND ERROR codes (RP-013 pp.52-53).
_MMC_ERROR_CODES = {
    "receive_buffer_overflow": 0x01, "sysex_length_error": 0x02,
    "command_count_error": 0x03, "write_field_count_error": 0x04,
    "illegal_group_name": 0x05, "illegal_procedure_name": 0x06,
    "illegal_event_name": 0x07, "illegal_name_extension": 0x08,
    "segmentation_error": 0x09,
    "update_list_overflow": 0x20, "group_buffer_overflow": 0x21,
    "undefined_procedure": 0x22, "procedure_buffer_overflow": 0x23,
    "undefined_event": 0x24, "event_buffer_overflow": 0x25,
    "blank_time_code": 0x26,
    "unsupported_command": 0x40, "unrecognized_sub_command": 0x41,
    "unrecognized_command_data": 0x42, "unsupported_field_in_command": 0x43,
    "unsupported_field_in_procedure_read": 0x44,
    "event_trigger_source_unavailable": 0x45, "nested_procedure_assemble": 0x46,
    "recursive_procedure_execute": 0x47, "nested_event_define": 0x48,
    "procedure_assemble_in_event_define": 0x49,
    "write_to_unsupported_field": 0x60, "write_to_read_only_field": 0x61,
    "unrecognized_write_data": 0x62, "unsupported_field_in_write_data": 0x63,
    "no_errors": 0x7F,
}


def _decode_command_error(data) -> dict:
    """COMMAND ERROR (RP-013 p.51): flags, level, error, then <count_1>
    <offset> <command string> (the command that caused the error)."""
    flags, level, error, count_1 = data[0], data[1], data[2], data[3]
    rest = data[4:]
    if len(rest) != count_1:
        raise ValueError("COMMAND ERROR count_1 doesn't match its data")
    out = {
        "error_halt": bool(flags & 0x01), "procedure_assemble_error": bool(flags & 0x02),
        "event_define_error": bool(flags & 0x04), "unsolicited": bool(flags & 0x10),
        "previously_transmitted": bool(flags & 0x20), "level": level,
        "error": _value_name(_MMC_ERROR_CODES, error),
    }
    if count_1:
        out["offset"] = "unavailable" if rest[0] == 0x7F else rest[0]
        out["command_bytes"] = list(rest[1:])
    return out


# MOTION CONTROL TALLY (RP-013 pp.56-57): the most recent Motion Control State
# command and Motion Control Process, and a success level for each.
_MMC_MOTION_STATES = ("stop", "play", "fast_forward", "rewind", "pause", "eject",
                      "variable_play", "search", "shuttle", "step")
_MMC_MCS_SUCCESS = {
    "stop": {0: "in_transition", 1: "completely_stopped", 2: "failure", 3: "deduced_motion"},
    "play": {0: "in_transition", 1: "requested_motion_achieved", 2: "failure",
             3: "deduced_motion", 5: "playing_not_resolved"},
    "fast_forward": {0: "in_transition", 1: "requested_motion_achieved", 2: "failure",
                     3: "deduced_motion"},
    "rewind": {0: "in_transition", 1: "requested_motion_achieved", 2: "failure",
               3: "deduced_motion"},
    "pause": {0: "in_transition", 1: "completely_stopped", 2: "failure"},
    "eject": {0: "in_transition", 1: "media_ejected", 2: "failure"},
    "variable_play": {0: "in_transition", 1: "requested_motion_achieved", 2: "failure"},
    "search": {0: "in_transition", 1: "requested_motion_achieved", 2: "failure"},
    "shuttle": {0: "in_transition", 1: "requested_motion_achieved", 2: "failure"},
    "step": {0: "in_transition", 1: "step_completed", 2: "failure", 4: "step_in_progress"},
}
_MMC_MCP_SUCCESS = {
    "locate": {0: "locating", 1: "locate_complete", 2: "failure",
               4: "locating_deferred_play_pending",
               6: "locating_deferred_variable_play_pending"},
    "chase": {0: "synchronizing", 1: "synchronized", 2: "failure",
              4: "chasing_not_in_play", 6: "parked"},
}


def _decode_motion_control_tally(data) -> dict:
    state_code, process_code, levels = data[0], data[1], data[2]
    state = _value_name(_COMMANDS["mmc"], state_code)
    process = "none" if process_code == 0x7F else _value_name(_COMMANDS["mmc"], process_code)
    state_level, process_level = levels & 0x07, (levels >> 4) & 0x07
    out = {
        "motion_state": state,
        "motion_state_success": _MMC_MCS_SUCCESS.get(state, {}).get(state_level, state_level),
        "motion_process": process,
        "motion_process_success": _MMC_MCP_SUCCESS.get(process, {}).get(process_level,
                                                                         process_level),
    }
    if len(data) > 3:
        out["extension"] = list(data[3:])
    return out


def _decode_user_bits_field(data) -> dict:
    if len(data) != 9:
        raise ValueError("userbits fields have 9 bytes")
    return {"binary_groups": list(data[:8]), "flags": data[8]}


def _encode_user_bits_field(entry: dict) -> tuple:
    """Standard Userbits (RP-013 section 3, the MTC User Bits u1-u9 layout):
    'binary_groups' or 'characters', plus 'flags' (_user_bit_groups)."""
    return (*_user_bit_groups(entry), _check_range("flags", entry.get("flags", 0), 0, 3))


_MMC_FIELD_CODECS.update({
    "signature": (None, _decode_signature),
    "command_error": (None, _decode_command_error),
    "motion_control_tally": (None, _decode_motion_control_tally),
    "velocity_tally": (None, lambda data: _decode_speed(data)),  # defined further down
    "selected_time_code_userbits": (None, _decode_user_bits_field),
    "generator_userbits": (_encode_user_bits_field, _decode_user_bits_field),
})


# Generator and MIDI Time Code tallies and set-ups (RP-013 pp.68-70).
_MMC_COMMAND_SUCCESS = {0: "in_transition", 1: "successful", 2: "failure"}
_MMC_GENERATOR_RUN_REFERENCE = {
    "internal_standard": 0, "external": 1, "internal_drop_a": 2, "internal_drop_b": 3,
    "local": 7,
}
_MMC_GENERATOR_JAM_REFERENCE = {"source_frame_edges": 0, "external": 1, "local": 7}
_MMC_GENERATOR_JAM_SOURCE = {
    "selected_time_code": 0x01, "selected_master_code": 0x02, "local": 0x7F,
}
_MMC_GENERATOR_JAM_MODE = {"stop_with_source": 0x00, "continue": 0x01}
_MMC_MTC_SOURCE = {
    "selected_time_code": 0x01, "selected_master_code": 0x02,
    "generator_time_code": 0x06, "midi_time_code_input": 0x07, "local": 0x7F,
}
_MMC_MTC_FLAGS = (
    "transmit_while_stopped", "stopped_full_messages", "transmit_while_fast",
    "fast_full_messages", "transmit_userbits", "mute_on_response_cable",
)


def _decode_generator_command_tally(data) -> dict:
    command, status = data
    return {
        "command": _value_name(_MMC_GENERATOR_ACTIONS, command),
        "success": _MMC_COMMAND_SUCCESS.get(status & 0x07, status & 0x07),
        "source_data_lost": bool(status & 0x10),
        "frame_sync_reference_lost": bool(status & 0x20),
    }


def _encode_generator_set_up(entry: dict) -> tuple:
    """GENERATOR SET UP: <reference> = 0 yyy 0 nnn (nnn run mode, yyy
    copy/jam), <source>, <copy/jam mode>."""
    run = _named_value(entry, "run_reference", _MMC_GENERATOR_RUN_REFERENCE)
    jam = _named_value(entry, "copy_jam_reference", _MMC_GENERATOR_JAM_REFERENCE)
    if run > 7 or jam > 7:
        raise ValueError("'run_reference' and 'copy_jam_reference' are 3-bit (0-7)")
    return (
        (jam << 4) | run,
        _named_value(entry, "copy_jam_source", _MMC_GENERATOR_JAM_SOURCE),
        _named_value(entry, "copy_jam_mode", _MMC_GENERATOR_JAM_MODE),
    )


def _decode_generator_set_up(data) -> dict:
    reference, source, mode = data
    if reference & 0x88:
        raise ValueError("GENERATOR SET UP reference bits 3 and 7 must be 0")
    return {
        "run_reference": _value_name(_MMC_GENERATOR_RUN_REFERENCE, reference & 0x07),
        "copy_jam_reference": _value_name(_MMC_GENERATOR_JAM_REFERENCE, reference >> 4),
        "copy_jam_source": _value_name(_MMC_GENERATOR_JAM_SOURCE, source),
        "copy_jam_mode": _value_name(_MMC_GENERATOR_JAM_MODE, mode),
    }


def _decode_mtc_command_tally(data) -> dict:
    command, status = data
    return {
        "command": _value_name(_MMC_MTC_COMMAND_ACTIONS, command),
        "success": _MMC_COMMAND_SUCCESS.get(status & 0x07, status & 0x07),
    }


def _encode_mtc_set_up(entry: dict) -> tuple:
    """MIDI TIME CODE SET UP: <flags> (bits a-f in _MMC_MTC_FLAGS order),
    <source>."""
    flags = sum(1 << bit for bit, name in enumerate(_MMC_MTC_FLAGS) if entry.get(name))
    return flags, _named_value(entry, "source", _MMC_MTC_SOURCE)


def _decode_mtc_set_up(data) -> dict:
    flags, source = data
    if flags & 0x40:
        raise ValueError("MIDI TIME CODE SET UP flag bit 6 must be 0")
    return {**{name: bool(flags >> bit & 1) for bit, name in enumerate(_MMC_MTC_FLAGS)},
            "source": _value_name(_MMC_MTC_SOURCE, source)}


def _decode_procedure_response(data) -> dict:
    """PROCEDURE RESPONSE (RP-013 p.71): the procedure and its commands;
    procedure 7F means none set or defined."""
    if data[0] == 0x7F:
        return {"procedure": "invalid"}
    return {"procedure": data[0], "commands": _mmc_parse_commands(data[1:])}


def _decode_event_response(data) -> dict:
    """EVENT RESPONSE (RP-013 p.71): event, flags (as EVENT [DEFINE]),
    trigger source, event time, one command; event 7F means none set or
    defined."""
    if data[0] == 0x7F:
        return {"event": "invalid"}
    event, flags, source = data[0], data[1], data[2]
    commands = _mmc_parse_commands(data[8:])
    if len(commands) != 1:
        raise ValueError("an EVENT RESPONSE carries exactly one command")
    return {
        "event": event,
        "direction": _name_for(_MMC_EVENT_DIRECTIONS, flags & 0x03),
        "all_speeds": bool(flags & 0x10), "non_delete": bool(flags & 0x40),
        "trigger_source": _info_field_name(source),
        "event_time": _decode_standard_time_code(*data[3:8]),
        "trigger_command": commands[0],
    }


_MMC_FIELD_CODECS.update({
    "generator_command_tally": (None, _decode_generator_command_tally),
    "generator_set_up": (_encode_generator_set_up, _decode_generator_set_up),
    "midi_time_code_command_tally": (None, _decode_mtc_command_tally),
    "midi_time_code_set_up": (_encode_mtc_set_up, _decode_mtc_set_up),
    # The response decoders call functions defined further down.
    "procedure_response": (None, lambda data: _decode_procedure_response(data)),
    "event_response": (None, lambda data: _decode_event_response(data)),
    "failure": (None, lambda data: {"text": bytes(data).decode("ascii")}),
})


def _mmc_response_field(name_byte: int, payload: list) -> dict:
    """One field of an MMC response, given its name byte and data (the
    count byte already removed)."""
    if name_byte == _MMC_RESPONSE_ONLY_NAMES["response_segment"]:
        # RESPONSE SEGMENT (RP-013 p.73): <id> = 0 f ssssss, then a piece of
        # a long response string; _StreamDecoder reassembles them.
        if not payload:
            raise ValueError("RESPONSE SEGMENT has no segment id")
        return {"type": "response_segment", "first": bool(payload[0] & 0x40),
                "remaining": payload[0] & 0x3F, "data": list(payload[1:])}
    if name_byte == _MMC_RESPONSE_ONLY_NAMES["response_error"]:
        names = {v: k for k, v in _INFO_FIELD_NAMES.items()}
        return {
            "type": "response_error",
            "unsupported_fields": [names.get(b, f"0x{b:02X}") for b in payload],
        }
    name = {v: k for k, v in _INFO_FIELD_NAMES.items()}.get(name_byte)
    if name is None:
        return {
            "type": "unknown", "raw_name_byte": name_byte, "data": list(payload),
            "note": "not a registered Information Field",
        }
    if name_byte < 0x20:
        return {"type": "field_value", "name": name,
                **_decode_standard_time_code(*payload)}
    if name_byte < 0x40:
        return {"type": "field_value", "name": name,
                **_decode_short_time_code(*payload)}
    codec = _MMC_FIELD_CODECS.get(name)
    if codec is not None:
        try:
            return {"type": "field_value", "name": name, **codec[1](payload)}
        except (ValueError, KeyError, IndexError, TypeError):
            pass  # wrong length or an undefined code: report the raw data
    return {"type": "field_value", "name": name, "data": list(payload)}


def _mmc_response_string(body) -> list:
    """Split an MMC response string into fields by the name-byte ranges of
    RP-013 p.9: 01-1F 5 data bytes, 20-3F 2, 40-77 <count> + data, 78-7F
    none (handshakes). Extension sets (00 prefix) aren't decoded."""
    fields, at = [], 0
    while at < len(body):
        name_byte = body[at]
        if name_byte == 0x00:
            raise ValueError("MMC extension sets (00 prefix) aren't decoded")
        if name_byte >= 0x78:
            handshake = {v: k for k, v in _MMC_RESPONSE_HANDSHAKES.items()}.get(name_byte)
            fields.append({"type": "handshake",
                           "name": handshake or f"0x{name_byte:02X}"})
            at += 1
            continue
        if name_byte < 0x40:
            size = 5 if name_byte < 0x20 else 2
            payload, at = body[at + 1:at + 1 + size], at + 1 + size
        else:
            if at + 1 >= len(body):
                raise ValueError(f"field 0x{name_byte:02X} has no <count> byte")
            size = body[at + 1]
            payload, at = body[at + 2:at + 2 + size], at + 2 + size
        if len(payload) != size:
            name = {v: k for k, v in _INFO_FIELD_NAMES.items()}.get(name_byte)
            label = f"0x{name_byte:02X} ({name})" if name else f"0x{name_byte:02X}"
            raise ValueError(
                f"field {label} needs {size} data byte(s), got {len(payload)}"
            )
        fields.append(_mmc_response_field(name_byte, list(payload)))
    return fields


def _mmc_response_fields(data: list) -> dict:
    """Decode an MMC Response (F0 7F <device_id> 07 <fields> F7; 07 is mcr,
    device to controller), given the whole SysEx including F0 and F7.
    Raises ValueError for a malformed one. One field comes back as that
    field's dict; several as type "fields" with a 'fields' list."""
    if len(data) < 5 or data[0] != 0xF0 or data[-1] != 0xF7:
        raise ValueError(
            "'data' must be a complete sysex, including the leading "
            "0xF0 and trailing 0xF7"
        )
    if data[1] != 0x7F or data[3] != 0x07:
        raise ValueError(
            "'data' is not an MMC Response sysex — expected "
            "F0 7F <device_id> 07 ... F7"
        )
    fields = _mmc_response_string(data[4:-1])
    if not fields:
        raise ValueError("the MMC Response has no fields")
    if len(fields) == 1:
        return {"device_id": data[2], **fields[0]}
    return {"device_id": data[2], "type": "fields", "fields": fields}


def _decode_mmc_response(tool_input: dict) -> str:
    """The decode_mmc_response action: _mmc_response_fields on 'data'."""
    data = tool_input.get("data")
    if not data:
        return _err(
            "'data' is required: a non-empty list of ints, the full "
            "sysex including the leading 0xF0 and trailing 0xF7"
        )
    try:
        fields = _mmc_response_fields(list(data))
    except (ValueError, TypeError) as e:
        return _err(str(e))
    return json.dumps({"status": "ok", **fields})


def _encode_msc_ascii_field(name: str, value: str) -> tuple:
    """Encode an MSC Q_number/Q_list/Q_path: ASCII digits with '.' as
    the decimal point (MSC 1.0 example: cue "235.6" -> 32 33 35 2E 36).
    Only the character set is checked; the spec's rules for stray dots
    apply to receivers.
    """
    if not _as_text(name, value) or any(c not in "0123456789." for c in value):
        raise ValueError(
            f"{name!r} must be a non-empty string of digits and '.' only, "
            f"got {value!r}"
        )
    return tuple(ord(c) for c in value)


def _encode_msc_cue_data(q_number, q_list, q_path) -> tuple:
    """Cue data for MSC GO, STOP, RESUME, TIMED_GO and GO_OFF: the
    fields present, separated by 00. Q_list needs Q_number; Q_path needs
    Q_list. Absent trailing fields are left off.
    """
    if q_number is None:
        if q_list is not None or q_path is not None:
            raise ValueError("'q_list'/'q_path' require 'q_number' too")
        return ()
    out = list(_encode_msc_ascii_field("q_number", q_number))
    if q_list is None:
        if q_path is not None:
            raise ValueError("'q_path' requires 'q_list' too")
        return tuple(out)
    out += [0x00] + list(_encode_msc_ascii_field("q_list", q_list))
    if q_path is None:
        return tuple(out)
    out += [0x00] + list(_encode_msc_ascii_field("q_path", q_path))
    return tuple(out)


def _encode_tuning_frequency(entry: dict) -> tuple:
    """Encode one MIDI Tuning frequency (Frequency Data Format), used by
    Bulk Tuning Dump and Single Note Tuning Change: the equal-tempered
    semitone at or below the frequency (0-127), then a 14-bit fraction of
    100 cents above it, MSB byte first.

    Takes {"semitone": 0-127, "cents": 0 <= cents < 100}, or
    {"no_change": true} for the 7F 7F 7F "leave this key unchanged" value.
    """
    if entry.get("no_change"):
        return (0x7F, 0x7F, 0x7F)
    semitone = entry.get("semitone")
    cents = entry.get("cents")
    if semitone is None:
        raise KeyError("'semitone' (or 'no_change': true)")
    if cents is None:
        raise KeyError("'cents' (or 'no_change': true)")
    _check_range("semitone", semitone, 0, 127)
    if not (0 <= _as_number("cents", cents) < 100):
        raise ValueError(f"'cents' must be 0 <= cents < 100, got {cents!r}")
    frac14 = round(cents / 100.0 * 16384)
    frac14 = min(frac14, 16383)  # cents just under 100 can round to 16384
    return (semitone, (frac14 >> 7) & 0x7F, frac14 & 0x7F)


def _encode_time_signature_pair(numerator: int, denominator: int) -> tuple:
    """Encode one Notation Time Signature pair as nn dd. `denominator` is
    the note value (2, 4, 8, ...), as in mido's time_signature meta event;
    the wire byte is its power of 2.
    """
    _check_range("numerator", numerator, 0, 127)
    if isinstance(denominator, bool) or not isinstance(denominator, int):
        raise TypeError(f"'denominator' must be an integer, got {denominator!r}")
    if denominator < 1 or (denominator & (denominator - 1)) != 0:
        raise ValueError(
            f"'denominator' must be a positive power of 2 (1, 2, 4, 8, "
            f"...), got {denominator!r}"
        )
    exponent = denominator.bit_length() - 1
    if not (0 <= exponent <= 127):
        raise ValueError(
            f"'denominator' {denominator!r} is out of representable range"
        )
    return (numerator, exponent)


def _nibblize(raw_bytes) -> tuple:
    """Split 8-bit bytes into nibbles, low nibble first, for MTC Cueing
    additional info (MTC spec, "Additional Information"). Example:
    91 46 7F -> 01 09 06 04 0F 07.
    """
    out: list = []
    for b in _as_list("additional_info_bytes", raw_bytes):
        if isinstance(b, bool) or not isinstance(b, int) or not (0 <= b <= 255):
            raise ValueError(
                f"additional-info bytes must each be 0-255 (a full "
                f"8-bit MIDI byte, pre-nibblization), got {b!r}"
            )
        out.append(b & 0x0F)
        out.append((b >> 4) & 0x0F)
    return tuple(out)


def _encode_file_dump_data(stored_bytes) -> tuple:
    """Encode 1-112 file bytes for a File Dump Data Packet. Each group of
    up to 7 bytes becomes a sign byte (bit 7 of each byte, first byte in
    bit 6, left-justified for a short group) followed by each byte's low 7
    bits.
    """
    if not (1 <= len(stored_bytes) <= 112):
        raise ValueError(
            f"a single Data Packet holds 1-112 stored bytes (encodes to "
            f"a max 128-byte payload), got {len(stored_bytes)}"
        )
    out: list = []
    for group_start in range(0, len(stored_bytes), 7):
        group = stored_bytes[group_start:group_start + 7]
        sign_byte = 0
        for i, b in enumerate(group):
            if isinstance(b, bool) or not isinstance(b, int) or not (0 <= b <= 255):
                raise ValueError(
                    f"stored bytes must each be 0-255, got {b!r}"
                )
            sign_byte |= ((b >> 7) & 1) << (6 - i)
        out.append(sign_byte)
        out.extend(b & 0x7F for b in group)
    return tuple(out)


# --- Shared message-building helpers ---------------------------------------
# Missing required field -> KeyError; bad value -> ValueError. _send and
# _write_midi_file report both.


def _required(message: dict, field: str, context: "str | None" = None):
    value = message.get(field)
    if value is None:
        where = f" (required for {context!r})" if context else ""
        raise KeyError(f"'{field}'{where}")
    return value


def _check_range(field: str, value, low: int, high: int):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"'{field}' must be an integer, got {value!r}")
    if not (low <= value <= high):
        raise ValueError(f"'{field}' must be {low}-{high}, got {value!r}")
    return value


def _as_list(field: str, value) -> list:
    """A list field's value; anything else is a TypeError."""
    if isinstance(value, (list, tuple)):
        return list(value)
    raise TypeError(f"'{field}' must be a list, got {value!r}")


def _as_dict(field: str, value) -> dict:
    """An object (dict) field's value; anything else is a TypeError."""
    if isinstance(value, dict):
        return value
    raise TypeError(f"'{field}' must be an object, got {value!r}")


def _as_text(field: str, value) -> str:
    if isinstance(value, str):
        return value
    raise TypeError(f"'{field}' must be text, got {value!r}")


def _as_number(field: str, value) -> "int | float":
    """An int or float (not a bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"'{field}' must be a number, got {value!r}")
    return value


def _choice(message: dict, field: str, table, context: str):
    """Read a required field whose value must be a key of `table`."""
    value = _as_text(field, _required(message, field))
    if value not in table:
        raise ValueError(
            f"'{field}' must be one of {sorted(table)} for {context!r}, "
            f"got {value!r}"
        )
    return value


def _device_id(message: dict, default: "int | None" = 0x7F) -> int:
    """SysEx device ID, 0-127. default=None makes it required."""
    device_id = message.get("device_id", default)
    if device_id is None:
        raise KeyError("'device_id'")
    return _check_range("device_id", device_id, 0, 127)


def _sysex(*data: int, time: int = 0) -> "mido.Message":
    """A SysEx message; `data` excludes F0/F7."""
    return mido.Message("sysex", data=data, time=time)


def _split14(field: str, value: int) -> tuple:
    """A 0-16383 value as (LSB, MSB) 7-bit bytes."""
    _check_range(field, value, 0, 0x3FFF)
    return value & 0x7F, (value >> 7) & 0x7F


def _xor_checksum(data) -> int:
    checksum = 0
    for b in data:
        checksum ^= b
    return checksum


def _ascii(field: str, text: str, *, printable: bool = False) -> tuple:
    """Encode text as 7-bit ASCII bytes; printable=True allows 20h-7Eh only."""
    low, high = (0x20, 0x7E) if printable else (0x00, 0x7F)
    for c in _as_text(field, text):
        if not (low <= ord(c) <= high):
            kind = "printable ASCII (0x20-0x7E)" if printable else "7-bit ASCII"
            raise ValueError(f"'{field}' must be {kind}, got {c!r}")
    return tuple(ord(c) for c in text)


def _time_code_fields(message: dict, *extra: str, context: "str | None" = None) -> tuple:
    """The required hours, minutes, seconds, frames, frame_rate fields, plus
    any `extra` ones, in that order."""
    names = ("hours", "minutes", "seconds", "frames", "frame_rate", *extra)
    return tuple(_required(message, name, context) for name in names)


def _required_list(message: dict, field: str, context: str) -> list:
    """A required field that must be a non-empty list."""
    value = message.get(field)
    if value is None or value == [] or value == ():
        raise KeyError(f"'{field}' (required non-empty list for {context!r})")
    return _as_list(field, value)


def _user_bit_groups(message: dict) -> tuple:
    """The 8 SMPTE binary groups (u1-u8), from 'binary_groups' (8 nibbles,
    group 1 first) or 'characters' (4 characters of 8 bits). Characters go
    into the groups in RP-004/008's order hhhhgggg ffffeeee ddddcccc bbbbaaaa:
    the first character is groups 8 and 7, the last is groups 2 and 1."""
    groups = message.get("binary_groups")
    characters = message.get("characters")
    if groups is not None and characters is not None:
        raise ValueError("specify only ONE of 'binary_groups' or 'characters', not both")
    if characters is not None:
        if len(_as_text("characters", characters)) != 4:
            raise ValueError(f"'characters' must be 4 characters, got {characters!r}")
        nibbles = [0] * 8
        for index, char in enumerate(characters):
            code = _check_range("characters entry", ord(char), 0, 255)
            nibbles[7 - 2 * index] = code >> 4
            nibbles[6 - 2 * index] = code & 0x0F
        return tuple(nibbles)
    groups = _as_list("binary_groups", _required(message, "binary_groups"))
    if len(groups) != 8:
        raise ValueError(f"'binary_groups' must have 8 entries, got {len(groups)}")
    return tuple(_check_range("binary_groups entry", g, 0, 15) for g in groups)


# --- GM2 and CA Universal SysEx ---------------------------------------------------

# Global Parameter Control slot paths and GM2's parameter numbers (GM2 4.4-4.5).
_GPC_EFFECTS = {"reverb": (0x01, 0x01), "chorus": (0x01, 0x02)}
_GPC_PARAMETERS = {
    "reverb": {"type": 0, "time": 1},
    "chorus": {"type": 0, "mod_rate": 1, "mod_depth": 2, "feedback": 3,
               "send_to_reverb": 4},
}

# Controller Destination controlled parameters (CA-022).
_CONTROLLER_DESTINATIONS = {
    "pitch": 0x00, "filter_cutoff": 0x01, "amplitude": 0x02,
    "lfo_pitch_depth": 0x03, "lfo_filter_depth": 0x04,
    "lfo_amplitude_depth": 0x05,
}

# Controllers Key-Based Instrument Control can't use (CA-023): Bank Select,
# Data Entry, the RPN/NRPN and increment/decrement controllers, and the mode
# messages 7A-7F. 78 and 79 are allowed; there they mean Fine/Coarse Tuning.
_KEY_BASED_EXCLUDED_CONTROLS = frozenset(
    {0x00, 0x20, 0x06, 0x26, *range(0x60, 0x66), *range(0x7A, 0x80)}
)


def _named_or_number(entry: dict, field: str, names: dict, context: str) -> int:
    """A field given either as a name from `names` or as a 0-127 number."""
    value = _required(entry, field, context)
    if isinstance(value, str):
        return names[_choice(entry, field, names, context)]
    return _check_range(field, value, 0, 127)


def _gpc_field(entry: dict, field: str, width: int, names: dict) -> list:
    """One Global Parameter Control parameter or value: an int (or, for a
    parameter, a name) when the width is 1, else a list of `width` raw bytes
    in wire order."""
    value = _required(entry, field, "parameters")
    if isinstance(value, list):
        if len(value) != width:
            raise ValueError(f"'{field}' must have {width} bytes, got {value!r}")
        return [_check_range(f"{field} byte", b, 0, 127) for b in value]
    if width != 1:
        raise ValueError(f"'{field}' must be a list of {width} bytes, got {value!r}")
    return [_named_or_number(entry, field, names, "parameters")
            if field == "parameter" else _check_range(field, value, 0, 127)]


def _gpc_data(message: dict) -> tuple:
    """Global Parameter Control (GM2 4.4-4.5): sw pw vw <slot path>
    [<parameter> <value>]... 'effect' ("reverb" or "chorus") sets the slot
    path and allows GM2 parameter names; otherwise 'slot_path' is a list of
    [msb, lsb] pairs. Widths above 1 take raw byte lists."""
    if message.get("effect") is not None and message.get("slot_path") is not None:
        raise ValueError("specify only ONE of 'effect' or 'slot_path', not both")
    effect = None
    if message.get("effect") is not None:
        effect = _choice(message, "effect", _GPC_EFFECTS, "global_parameter_control")
        slot_path = [_GPC_EFFECTS[effect]]
    else:
        slot_path = _required_list(message, "slot_path", "global_parameter_control")
    parameter_width = _check_range(
        "parameter_width", message.get("parameter_width", 1), 1, 127,
    )
    value_width = _check_range("value_width", message.get("value_width", 1), 1, 127)
    data = [len(slot_path), parameter_width, value_width]
    for slot in slot_path:
        if not isinstance(slot, (list, tuple)) or len(slot) != 2:
            raise ValueError(f"each 'slot_path' entry must be [msb, lsb], got {slot!r}")
        data += [_check_range("slot_path byte", b, 0, 127) for b in slot]
    names = _GPC_PARAMETERS.get(effect, {}) if effect is not None else {}
    data += _each("parameters", _required_list(message, "parameters", "global_parameter_control"),
                  lambda entry: (
                      *_gpc_field(entry, "parameter", parameter_width, names),
                      *_gpc_field(entry, "value", value_width, names),
                  ))
    return tuple(data)


def _key_based_pair(entry: dict) -> tuple:
    control = _check_range("control", _required(entry, "control"), 0, 127)
    if control in _KEY_BASED_EXCLUDED_CONTROLS:
        raise ValueError(
            f"'control' {control:#04x} can't be used in Key-Based Instrument "
            f"Control (CA-023)"
        )
    return control, _check_range("value", _required(entry, "value"), 0, 127)


# --- MIDI Tuning (MIDI Tuning Updated Specification) ---------------------------
# F0 7E|7F <device_id> 08 <code> <payload> F7. note_change is real time;
# note_change_bank and the scale/octave changes take 'real_time' (default
# true); everything else is non-real time. The dumps end with a checksum: the
# XOR of every byte between F0 and the checksum.

_TUNING_REAL_TIME_ONLY = frozenset({"note_change"})
_TUNING_EITHER_TIME = frozenset(
    {"note_change_bank", "scale_octave_1byte", "scale_octave_2byte"}
)
_TUNING_DUMPS = frozenset({
    "bulk_dump_reply", "key_based_dump", "scale_octave_dump_1byte",
    "scale_octave_dump_2byte",
})


def _tuning_real_time(message: dict, command: str) -> bool:
    if command in _TUNING_REAL_TIME_ONLY:
        return True
    if command not in _TUNING_EITHER_TIME:
        return False
    real_time = message.get("real_time", True)
    if not isinstance(real_time, bool):
        raise TypeError(f"'real_time' must be true or false, got {real_time!r}")
    return real_time


def _tuning_bank(message: dict) -> int:
    return _check_range("bank", _required(message, "bank"), 0, 127)


def _tuning_program(message: dict) -> int:
    return _check_range("tuning_program", _required(message, "tuning_program"), 0, 127)


def _tuning_name(message: dict) -> tuple:
    """The 16-character tuning name, space padded."""
    name = _as_text("tuning_name", message.get("tuning_name", ""))
    if len(name) > 16:
        raise ValueError(
            f"'tuning_name' must be at most 16 characters, got {len(name)} ({name!r})"
        )
    return _ascii("tuning_name", name.ljust(16))


def _tuning_notes(message: dict, command: str) -> tuple:
    """128 x [xx yy zz], note 0 first."""
    notes = _as_list("notes", _required(message, "notes", command))
    if len(notes) != 128:
        raise ValueError(
            f"'notes' must have exactly 128 entries (one per MIDI key number), "
            f"got {len(notes)}"
        )
    return _each("notes", notes, _encode_tuning_frequency)


def _tuning_changes(message: dict, command: str) -> tuple:
    """ll then [kk xx yy zz] per entry."""
    changes = _required_list(message, "changes", command)
    if len(changes) > 127:
        raise ValueError(f"'changes' must have 1-127 entries, got {len(changes)}")
    return (len(changes), *_each("changes", changes, lambda entry: (
        _check_range("key", _required(entry, "key"), 0, 127),
        *_encode_tuning_frequency(entry),
    )))


def _tuning_offsets(message: dict, command: str, two_byte: bool) -> tuple:
    """Scale/octave offsets for C through B. 1-byte: 0-127, 64 = 0 cents,
    1 cent per step. 2-byte: 0-16383, 8192 = 0 cents, 200/16384 cents per
    step, sent MSB first."""
    offsets = _as_list("offsets", _required(message, "offsets", command))
    if len(offsets) != 12:
        raise ValueError(f"'offsets' must have 12 entries (C to B), got {len(offsets)}")
    if not two_byte:
        return tuple(_check_range("offsets entry", v, 0, 127) for v in offsets)
    out: list = []
    for v in offsets:
        lsb, msb = _split14("offsets entry", v)
        out += [msb, lsb]
    return tuple(out)


def _channel_bitmap(message: dict, command: str) -> tuple:
    """ff gg hh: ff bits 0-1 = channels 14-15, gg = channels 7-13, hh =
    channels 0-6 (0-based); ff bits 2-6 are reserved and stay 0."""
    ff = gg = hh = 0
    for channel in _required_list(message, "channels", command):
        _check_range("channels entry", channel, 0, 15)
        if channel >= 14:
            ff |= 1 << (channel - 14)
        elif channel >= 7:
            gg |= 1 << (channel - 7)
        else:
            hh |= 1 << channel
    return ff, gg, hh


def _decode_channel_bitmap(ff: int, gg: int, hh: int) -> list:
    if ff & ~0x03:
        raise ValueError("reserved channel bits are set")
    return (
        [c for c in range(7) if hh >> c & 1]
        + [c + 7 for c in range(7) if gg >> c & 1]
        + [c + 14 for c in range(2) if ff >> c & 1]
    )


# command -> payload after <code> (the checksum is added for dumps).
_TUNING_PAYLOADS = {
    "bulk_dump_request": lambda m, c: (_tuning_program(m),),
    "bulk_dump_request_bank": lambda m, c: (_tuning_bank(m), _tuning_program(m)),
    "bulk_dump_reply": lambda m, c: (
        _tuning_program(m), *_tuning_name(m), *_tuning_notes(m, c),
    ),
    "key_based_dump": lambda m, c: (
        _tuning_bank(m), _tuning_program(m), *_tuning_name(m), *_tuning_notes(m, c),
    ),
    "scale_octave_dump_1byte": lambda m, c: (
        _tuning_bank(m), _tuning_program(m), *_tuning_name(m),
        *_tuning_offsets(m, c, two_byte=False),
    ),
    "scale_octave_dump_2byte": lambda m, c: (
        _tuning_bank(m), _tuning_program(m), *_tuning_name(m),
        *_tuning_offsets(m, c, two_byte=True),
    ),
    "note_change": lambda m, c: (_tuning_program(m), *_tuning_changes(m, c)),
    "note_change_bank": lambda m, c: (
        _tuning_bank(m), _tuning_program(m), *_tuning_changes(m, c),
    ),
    "scale_octave_1byte": lambda m, c: (
        *_channel_bitmap(m, c), *_tuning_offsets(m, c, two_byte=False),
    ),
    "scale_octave_2byte": lambda m, c: (
        *_channel_bitmap(m, c), *_tuning_offsets(m, c, two_byte=True),
    ),
}


# --- Sample Dump Standard (MIDI 1.0 Detailed Spec pp.35-39) --------------------
# Numbers are LSB first in 7-bit bytes (14 or 21 bits). Data packets carry
# 120 bytes of sample words, MSB first and left-justified: 2 bytes per word
# for 8-14 bit formats, 3 for 15-21, 4 for 22-28 (spec example: the 12-bit
# word FFFH is 7F 7C). Handshakes (ACK, NAK, WAIT, CANCEL, EOF) are the
# generic ones built by file_dump.

_SAMPLE_LOOP_TYPES = {"forward": 0x00, "bidirectional": 0x01, "off": 0x7F}
_SAMPLE_PACKET_BYTES = 120


def _split21(field: str, value: int) -> tuple:
    """A 0-2097151 value as three 7-bit bytes, LSB first."""
    _check_range(field, value, 0, 0x1FFFFF)
    return value & 0x7F, (value >> 7) & 0x7F, (value >> 14) & 0x7F


def _join21(b0: int, b1: int, b2: int) -> int:
    return b0 | (b1 << 7) | (b2 << 14)


def _sample_bytes_per_word(sample_format: int) -> int:
    _check_range("sample_format", sample_format, 8, 28)
    return 2 if sample_format <= 14 else 3 if sample_format <= 21 else 4


def _pack_sample_words(words, sample_format: int) -> list:
    """Sample words (0 = full negative, all ones = full positive) as packet
    bytes: MSB first, left-justified, unused low bits 0."""
    per_word = _sample_bytes_per_word(sample_format)
    shift = 7 * per_word - sample_format
    out: list = []
    for word in _as_list("words", words):
        value = _check_range("words entry", word, 0, (1 << sample_format) - 1) << shift
        out += [(value >> (7 * (per_word - 1 - i))) & 0x7F for i in range(per_word)]
    if len(out) > _SAMPLE_PACKET_BYTES:
        raise ValueError(
            f"at most {_SAMPLE_PACKET_BYTES // per_word} words fit in a packet for "
            f"a {sample_format}-bit format, got {len(words)}"
        )
    return out


def _sample_packet_data(message: dict) -> list:
    """The 120 data bytes: 'data' (raw bytes) or 'words' with
    'sample_format', zero-padded as the spec requires for the last packet."""
    data, words = message.get("data"), message.get("words")
    if data is not None and words is not None:
        raise ValueError("specify only ONE of 'data' or 'words', not both")
    if words is not None:
        out = _pack_sample_words(words, _required(message, "sample_format", "words"))
    else:
        out = [_check_range("data entry", b, 0, 127)
               for b in _required_list(message, "data", "data_packet")]
        if len(out) > _SAMPLE_PACKET_BYTES:
            raise ValueError(f"'data' holds at most 120 bytes, got {len(out)}")
    return out + [0] * (_SAMPLE_PACKET_BYTES - len(out))


def _sample_loop_number(message: dict) -> tuple:
    """A loop number, or "all" (7F 7F: delete all / request all loops)."""
    loop = _required(message, "loop_number")
    return (0x7F, 0x7F) if loop == "all" else _split14("loop_number", loop)


def _sample_dump_payload(message: dict, command: str) -> tuple:
    sample = _split14("sample_number", _required(message, "sample_number", command))
    if command == "request":
        return sample
    if command == "loop_points_request":
        return (*sample, *_sample_loop_number(message))
    if command == "loop_points":
        return (
            *sample, *_sample_loop_number(message),
            _SAMPLE_LOOP_TYPES[_choice(message, "loop_type", _SAMPLE_LOOP_TYPES, command)],
            *_split21("loop_start", _required(message, "loop_start", command)),
            *_split21("loop_end", _required(message, "loop_end", command)),
        )
    # header
    return (
        *sample,
        _check_range("sample_format", _required(message, "sample_format", command), 8, 28),
        *_split21("sample_period", _required(message, "sample_period", command)),
        *_split21("sample_length", _required(message, "sample_length", command)),
        *_split21("sustain_loop_start", _required(message, "sustain_loop_start", command)),
        *_split21("sustain_loop_end", _required(message, "sustain_loop_end", command)),
        _SAMPLE_LOOP_TYPES[_choice(message, "loop_type", _SAMPLE_LOOP_TYPES, command)],
    )


# --- MIDI Machine Control (RP-013) -------------------------------------------
# Every command is F0 7F <device_id> 06 <opcode> [<count> <data...>] F7.
# _mmc_command_bytes builds <opcode> [<count> <data...>]. Commands that carry
# data have a builder in _MMC_DATA_BUILDERS that returns <data>; the 13
# transport commands in _MMC_NO_DATA_COMMANDS, WAIT and RESUME have none.

_MMC_GENERATOR_ACTIONS = {"stop": 0x00, "run": 0x01, "copy_jam": 0x02}
_MMC_MTC_COMMAND_ACTIONS = {"off": 0x00, "follow": 0x02}
_MMC_GROUP_ACTIONS = {"assign": 0x00, "dis_assign": 0x01}
_MMC_PROCEDURE_ACTIONS = {
    "assemble": 0x00, "delete": 0x01, "set": 0x02, "execute": 0x03,
}
_MMC_EVENT_ACTIONS = {"define": 0x00, "delete": 0x01, "set": 0x02, "test": 0x03}
_MMC_EVENT_DIRECTIONS = {"forward": 0b00, "reverse": 0b01, "both": 0b10}
_MMC_EVENT_TRIGGER_SOURCES = (
    "selected_time_code", "selected_master_code", "generator_time_code",
    "midi_time_code_input",
)
_MMC_GP_REGISTERS = ("gp0", "gp1", "gp2", "gp3", "gp4", "gp5", "gp6", "gp7")
_MMC_UPDATE_ACTIONS = {"begin": 0x00, "end": 0x01}

# Always sent to the all-call device ID 7F, whatever 'device_id' says
# (RP-013 pp.30-31 and p.42).
_MMC_ALL_CALL = frozenset({"assign_system_master", "wait", "resume"})

# The per-bit flags of Standard Time Code (_encode_standard_time_code).
_TIME_CODE_FLAGS = (
    "color_frame", "blank", "negative", "use_status_byte", "estimated",
    "invalid", "video_field_1", "no_time_code",
)


def _each(field: str, entries, encode) -> tuple:
    """encode(entry) for every entry of a list field, concatenated. An error
    is re-raised with the entry's index in front."""
    out: list = []
    for index, entry in enumerate(_as_list(field, entries)):
        if not isinstance(entry, dict):
            raise TypeError(f"'{field}'[{index}] must be an object, got {entry!r}")
        try:
            out += encode(entry)
        except KeyError as e:
            raise KeyError(f"'{field}'[{index}]: {e}") from e
        except ValueError as e:
            raise ValueError(f"'{field}'[{index}]: {e}") from e
        except TypeError as e:
            raise TypeError(f"'{field}'[{index}]: {e}") from e
    return tuple(out)


def _mmc_locate(message: dict, command: str) -> tuple:
    # LOCATE (44h, RP-013 p.28):
    #   [I/F]    00 <name>: locate to the time held in register GP0-GP7
    #            ('name').
    #   [TARGET] 01 then Standard Time Code with subframes (the time fields).
    time_fields = ("hours", "minutes", "seconds", "frames", "subframes", "frame_rate")
    if message.get("name") is not None:
        if any(message.get(field) is not None for field in time_fields):
            raise ValueError(
                "give 'name' (LOCATE [I/F]) or the time fields (LOCATE [TARGET]), "
                "not both"
            )
        name = _choice(message, "name", _MMC_GP_REGISTERS, command)
        return (0x00, _INFO_FIELD_NAMES[name])
    return (0x01, *_time_code_bytes(message, "subframes"))


def _mmc_step(message: dict, command: str) -> tuple:
    # STEP (48h, RP-013 p.30): one byte 0 g ssssss (g = reverse,
    # s = quantity 0-63).
    quantity = _check_range(
        "quantity", _required(message, "quantity", command), 0, 63,
    )
    return ((0x40 if message.get("reverse", False) else 0x00) | quantity,)


def _mmc_assign_system_master(message: dict, command: str) -> tuple:
    # ASSIGN SYSTEM MASTER (49h, RP-013 pp.30-31). target_device_id 7F
    # dis-assigns. Sent to all-call (_MMC_ALL_CALL).
    target = _required(message, "target_device_id", command)
    return (_check_range("target_device_id", target, 0, 127),)


def _mmc_generator_command(message: dict, command: str) -> tuple:
    # GENERATOR COMMAND (4Ah, RP-013 p.31): stop, run, or copy/jam the time
    # code generator, as set by the GENERATOR SET UP Information Field.
    action = _choice(message, "action", _MMC_GENERATOR_ACTIONS, command)
    return (_MMC_GENERATOR_ACTIONS[action],)


def _mmc_midi_time_code_command(message: dict, command: str) -> tuple:
    # MIDI TIME CODE COMMAND (4Bh, RP-013 p.31). The spec defines only 00 and
    # 02; MIDI TIME CODE SET UP sets what is sent.
    action = _choice(message, "action", _MMC_MTC_COMMAND_ACTIONS, command)
    return (_MMC_MTC_COMMAND_ACTIONS[action],)


def _mmc_speed(message: dict, command: str) -> tuple:
    # VARIABLE PLAY 45h, SEARCH 46h, SHUTTLE 47h (RP-013 pp.29-30), DEFERRED
    # VARIABLE PLAY 54h (p.37), RECORD STROBE VARIABLE 55h (p.41): sh sm sl
    # (_encode_standard_speed).
    speed = _required(message, "speed", command)
    return _encode_standard_speed(speed, message.get("reverse", False))


def _mmc_drop_frame_adjust(message: dict, command: str) -> tuple:
    # DROP FRAME ADJUST (4Fh, RP-013 p.33): converts a writeable field to
    # drop-frame in place (the device ignores it unless the field is 30fps
    # non-drop).
    name = _required(message, "name", command)
    return (_resolve_info_field_name(name, require_writeable=True),)


def _mmc_move(message: dict, command: str) -> tuple:
    # MOVE (4Ch, RP-013 p.32): destination = source. The destination must be
    # writeable; the source can be any field.
    destination = _required(message, "destination", command)
    source = _required(message, "source", command)
    return (
        _resolve_info_field_name(destination, require_writeable=True),
        _resolve_info_field_name(source),
    )


def _mmc_math(message: dict, command: str) -> tuple:
    # ADD (4Dh) / SUBTRACT (4Eh), RP-013 pp.32-33:
    # destination = source_1 +/- source_2. Same field rules as MOVE. The
    # destination may also be a source.
    destination, source_1, source_2 = (
        _required(message, field, command)
        for field in ("destination", "source_1", "source_2")
    )
    return (
        _resolve_info_field_name(destination, require_writeable=True),
        _resolve_info_field_name(source_1),
        _resolve_info_field_name(source_2),
    )


def _mmc_group(message: dict, command: str) -> tuple:
    # GROUP (52h, RP-013 p.39):
    #   assign (00): device_ids join group; group can't be 7F.
    #   dis_assign (01): device_ids leave group; group 7F means all groups.
    #     7F in device_ids means all devices.
    # Spec example (all devices, all groups): F0 7F 7F 06 52 03 01 7F 7F F7.
    action = _choice(message, "action", _MMC_GROUP_ACTIONS, command)
    group = _check_range("group", _required(message, "group", command), 0, 127)
    if action == "assign" and group == 0x7F:
        raise ValueError(
            "'group' must not be 0x7F (127) for action='assign' -- 0x7F is "
            "the all-call address, not a group number"
        )
    device_ids = _required_list(message, "device_ids", command)
    for device_id in device_ids:
        _check_range("device_ids entry", device_id, 0, 127)
    return (_MMC_GROUP_ACTIONS[action], group, *device_ids)


def _mmc_slot(message: dict, field: str, command: str, all_allowed: bool) -> int:
    """A procedure or event number. 7F means "all" where `all_allowed`
    (delete, set) and is reserved otherwise."""
    value = _required(message, field, command)
    return _check_range(field, value, 0, 0x7F if all_allowed else 0x7E)


def _mmc_procedure(message: dict, command: str) -> tuple:
    # PROCEDURE (50h, RP-013 pp.34-35): stored command lists.
    #   assemble (00): procedure + nested 'commands'.
    #   delete (01), set (02): procedure 7F means all procedures.
    #   execute (03).
    action = _choice(message, "action", _MMC_PROCEDURE_ACTIONS, command)
    procedure = _mmc_slot(
        message, "procedure", command, all_allowed=action in ("delete", "set"),
    )
    if action != "assemble":
        return (_MMC_PROCEDURE_ACTIONS[action], procedure)
    nested: list = []
    for entry in _required_list(message, "commands", "assemble"):
        nested += _encode_nested_mmc_command(
            entry, forbid_assemble=True, forbid_execute_name=procedure,
        )
    return (0x00, procedure, *nested)


def _mmc_event(message: dict, command: str) -> tuple:
    # EVENT (51h, RP-013 pp.35-38): commands triggered at a time.
    # define (00), delete (01), set (02), test (03); event 7F means all events
    # for delete/set and is reserved for define/test.
    # define payload: event, flags, trigger_source, name, trigger_command.
    #   flags = 0 k 0 a 00 dd: k = non_delete (stays armed after firing),
    #     a = all_speeds, dd = direction (00 forward, 01 reverse, 10 both).
    #   trigger_source: a time code field.
    #   name: the GP0-GP7 register holding the trigger time.
    #   trigger_command: one nested mmc command; it can't be an EVENT
    #     [DEFINE] or PROCEDURE [ASSEMBLE].
    action = _choice(message, "action", _MMC_EVENT_ACTIONS, command)
    event = _mmc_slot(
        message, "event", command, all_allowed=action in ("delete", "set"),
    )
    if action != "define":
        return (_MMC_EVENT_ACTIONS[action], event)
    direction = _choice(message, "direction", _MMC_EVENT_DIRECTIONS, "define")
    flags = (
        (0x40 if message.get("non_delete", False) else 0x00)
        | (0x10 if message.get("all_speeds", False) else 0x00)
        | _MMC_EVENT_DIRECTIONS[direction]
    )
    trigger_source = _choice(
        message, "trigger_source", _MMC_EVENT_TRIGGER_SOURCES, "define",
    )
    name = _choice(message, "name", _MMC_GP_REGISTERS, "define")
    # Not 'command': that key already holds "event".
    trigger_command = _required(message, "trigger_command", "define")
    nested = _encode_nested_mmc_command(
        trigger_command, forbid_assemble=True, forbid_define=True,
    )
    return (
        0x00, event, flags, _INFO_FIELD_NAMES[trigger_source],
        _INFO_FIELD_NAMES[name], *nested,
    )


def _mmc_read(message: dict, command: str) -> tuple:
    # READ (42h, RP-013 p.26): ask for the current value of any registered
    # fields, read-only ones included. The device answers with an MMC
    # Response (decode with decode_mmc_response), or RESPONSE ERROR for
    # fields it doesn't support.
    names = _required_list(message, "names", command)
    return tuple(_resolve_info_field_name(name) for name in names)


def _mmc_write(message: dict, command: str) -> tuple:
    # WRITE (40h, RP-013 p.25): <name> <data> for each entry in 'fields'.
    # Writeable Standard Time Code fields send 5 bytes; count-prefixed
    # fields send <count> <data> (_MMC_FIELD_CODECS with an encoder), as in
    # RP-013's appendix example <TIME STANDARD> <count=01> 03.
    data: list = []
    for field in _required_list(message, "fields", command):
        field = _as_dict("fields entry", field)
        name = _as_text("name", _required(field, "name", "each 'fields' entry"))
        codec = _MMC_FIELD_CODECS.get(name)
        if codec is not None and codec[0] is not None:
            payload = codec[0](field)
            data += [_INFO_FIELD_NAMES[name], len(payload), *payload]
            continue
        if name in _INFO_FIELD_NAMES and name not in _WRITEABLE_INFO_FIELDS:
            writeable = sorted(_WRITEABLE_INFO_FIELDS | {
                n for n, c in _MMC_FIELD_CODECS.items() if c[0] is not None
            })
            raise ValueError(
                f"Information Field {name!r} can't be written; writeable fields "
                f"are {writeable}"
            )
        data.append(_resolve_info_field_name(name, require_writeable=True))
        data += _encode_standard_time_code(
            *_time_code_fields(field, context=name),
            subframes=field.get("subframes", 0),
            **{flag: field.get(flag, False) for flag in _TIME_CODE_FLAGS},
        )
    return tuple(data)


def _mmc_masked_write(message: dict, command: str) -> tuple:
    # MASKED WRITE (41h, RP-013 pp.25-26): change selected bits of a Track
    # Bitmap field. Each entry in 'fields' becomes <name> <byte#> <mask>
    # <data>. byte# 0 is the first bitmap byte after the field's own count
    # byte. mask and data are 7-bit, so 7F means all ones.
    data: list = []
    for field in _required_list(message, "fields", command):
        field = _as_dict("fields entry", field)
        name = _required(field, "name", "each 'fields' entry")
        data.append(_resolve_info_field_name(name, require_mask_writeable=True))
        for key in ("byte_number", "mask", "data"):
            data.append(_check_range(key, _required(field, key, name), 0, 127))
    return tuple(data)


def _mmc_update(message: dict, command: str) -> tuple:
    # UPDATE (43h, RP-013 pp.26-27):
    #   begin (00): send the named fields now, then again whenever they
    #     change (limited by the UPDATE RATE field).
    #   end (01): stop updating the named fields. The name "all" (sent as 7F)
    #     stops every update; it is valid only here.
    action = _choice(message, "action", _MMC_UPDATE_ACTIONS, command)
    names = _required_list(message, "names", command)
    return (_MMC_UPDATE_ACTIONS[action], *(
        0x7F if action == "end" and name == "all"
        else _resolve_info_field_name(name)
        for name in names
    ))


def _mmc_command_segment(message: dict, command: str) -> tuple:
    # COMMAND SEGMENT (53h, RP-013 p.39): one piece of a long command
    # string. <id> = 0 f ssssss: f = first segment, ssssss = segments still
    # to come (0 on the last). Usually built by 'segment': true on an mmc
    # message (_build_mmc_segments).
    first = message.get("first", False)
    if not isinstance(first, bool):
        raise TypeError(f"'first' must be true or false, got {first!r}")
    remaining = _check_range("remaining", _required(message, "remaining", command), 0, 63)
    data = [_check_range("data entry", b, 0, 127)
            for b in _as_list("data", _required(message, "data", command))]
    return ((0x40 if first else 0x00) | remaining, *data)


_MMC_DATA_BUILDERS = {
    "locate": _mmc_locate,
    "step": _mmc_step,
    "assign_system_master": _mmc_assign_system_master,
    "generator_command": _mmc_generator_command,
    "midi_time_code_command": _mmc_midi_time_code_command,
    "variable_play": _mmc_speed,
    "search": _mmc_speed,
    "shuttle": _mmc_speed,
    "deferred_variable_play": _mmc_speed,
    "record_strobe_variable": _mmc_speed,
    "drop_frame_adjust": _mmc_drop_frame_adjust,
    "move": _mmc_move,
    "add": _mmc_math,
    "subtract": _mmc_math,
    "group": _mmc_group,
    "procedure": _mmc_procedure,
    "event": _mmc_event,
    "read": _mmc_read,
    "write": _mmc_write,
    "masked_write": _mmc_masked_write,
    "update": _mmc_update,
    "command_segment": _mmc_command_segment,
}


def _mmc_command_bytes(message: dict) -> tuple:
    """<opcode> [<count> <data...>] for one mmc command dict; the count is
    the length of the data."""
    command = _choice(message, "command", _COMMANDS["mmc"], "mmc")
    opcode = _COMMANDS["mmc"][command]
    builder = _MMC_DATA_BUILDERS.get(command)
    if builder is None:
        return (opcode,)
    data = builder(message, command)
    return (opcode, len(data), *data)


# --- MIDI Show Control (RP-002/014) ------------------------------------------
# F0 7F <device_id> 02 <command_format> <command> <data> F7. Commands that
# carry data have a builder in _MSC_DATA_BUILDERS returning <data>; the ones
# in _MSC_NO_DATA_COMMANDS have none.

_MSC_NO_DATA_COMMANDS = frozenset({"all_off", "restore", "reset"})

# The time fields of MSC TIMED_GO and SET.
_MSC_TIME_FIELDS = (
    "hours", "minutes", "seconds", "frames", "fractional_frames", "frame_rate",
)


def _msc_command_format(message: dict) -> int:
    """The command_format byte: a name from _MSC_FORMATS, or
    'command_format_raw' (0-127) for a narrower sub-category. Exactly one."""
    name = message.get("command_format")
    raw = message.get("command_format_raw")
    if name is not None and raw is not None:
        raise ValueError(
            "specify only ONE of 'command_format' or 'command_format_raw', "
            "not both"
        )
    if raw is not None:
        return _check_range("command_format_raw", raw, 0, 127)
    if name is None:
        raise KeyError("'command_format' (or 'command_format_raw')")
    return _MSC_FORMATS[_choice(message, "command_format", _MSC_FORMATS, "msc")]


def _msc_cue(message: dict, command: str) -> tuple:
    # GO, STOP, RESUME, GO_OFF: optional cue data. LOAD: q_number required.
    if command == "load":
        q_number = _required(message, "q_number", command)
    else:
        q_number = message.get("q_number")
    return _encode_msc_cue_data(
        q_number, message.get("q_list"), message.get("q_path"),
    )


def _msc_timed_go(message: dict, command: str) -> tuple:
    # TIMED_GO: MSC time (hr mn sc fr ff), then the same cue data as GO.
    return (
        *_time_code_bytes(message, "fractional_frames", command),
        *_msc_cue(message, command),
    )


def _msc_set(message: dict, command: str) -> tuple:
    # SET: control number and value, 14 bits each, LSB first, then the MSC
    # time when all six time fields are given (none or all).
    control_number = _required(message, "control_number", command)
    control_value = _required(message, "control_value", command)
    data = (
        *_split14("control_number", control_number),
        *_split14("control_value", control_value),
    )
    given = [field for field in _MSC_TIME_FIELDS if message.get(field) is not None]
    if not given:
        return data
    if len(given) != len(_MSC_TIME_FIELDS):
        raise ValueError(
            f"SET's time fields ({'/'.join(_MSC_TIME_FIELDS)}) must be given "
            f"all together or not at all; got {given}"
        )
    return (*data, *_time_code_bytes(message, "fractional_frames", command))


def _msc_fire(message: dict, command: str) -> tuple:
    # FIRE: one macro number.
    macro = _required(message, "macro_number", command)
    return (_check_range("macro_number", macro, 0, 127),)


def _msc_single_cue_field(field: str, required: bool):
    """Builder for Sound Commands whose data is one cue field alone (Q_list
    or Q_path, not preceded by Q_number)."""
    def build(message: dict, command: str) -> tuple:
        value = _required(message, field, command) if required else message.get(field)
        return () if value is None else _encode_msc_ascii_field(field, value)
    return build


def _msc_set_clock(message: dict, command: str) -> tuple:
    # SET_CLOCK: MSC time, then Q_list if given.
    return (
        *_time_code_bytes(message, "fractional_frames", command),
        *_msc_single_cue_field("q_list", required=False)(message, command),
    )


# Two-Phase Commit (MSC 1.1.1 section 5 and 6). Status codes are 16-bit
# values with the low 2 bits 0, sent as s1 = (code/4) & 7F, s2 = (code/512)
# & 7F (section 6.7). The two tables differ: 80 28 is "manual override in
# progress" for CANCELLED but "manual override initiated" for ABORT.
_MSC_CANCELLED_STATUS = {
    "completing": 0x8004, "paused": 0x8008, "terminated": 0x800C,
    "reversed": 0x8010, "not_standing_by": 0x8024,
    "manual_override_in_progress": 0x8028,
}
_MSC_ABORT_STATUS = {
    "unknown_error": 0x0000, "checksum_error": 0x8000, "timeout": 0x8020,
    "not_standing_by": 0x8024, "manual_override_initiated": 0x8028,
    "manual_override_in_progress": 0x8030,
    "deadman_interlock_not_established": 0x8040,
    "safety_interlock_not_established": 0x8044,
    "unknown_q_number": 0x8050, "unknown_q_list": 0x8054,
    "unknown_q_path": 0x8058, "too_many_cues_active": 0x805C,
    "cue_out_of_sequence": 0x8060, "invalid_d1": 0x8064, "invalid_d2": 0x8068,
    "invalid_d3": 0x806C, "invalid_d4": 0x8070,
    "manual_cueing_required": 0x8090, "power_failure": 0x80A0,
    "reading_new_show_cues": 0x80B0,
}


# MSC 6.5 checksum. command_format, command and data (with the checksum's own
# two bytes zeroed, and a zero byte added if the count is odd) are summed as
# 2-byte values, overflow ignored; the device ID is added; the sum is ANDed
# with 7F7F and sent LSB first. 6.5 doesn't say which byte of each pair is the
# low one, so both pairings are offered by name.
_MSC_2PC_COMMANDS = frozenset(
    {"standby", "standing_by", "go_2pc", "complete", "cancel", "cancelled", "abort"}
)
_MSC_CHECKSUM_ORDERS = ("lsb_first", "msb_first")


def _msc_2pc_checksum(device_id: int, command_format: int, code: int,
                      data, order: str) -> tuple:
    """(cc LSB, cc MSB) for a 2PC message whose <data> is `data` (its first
    two bytes, the checksum's place, are taken as zero)."""
    body = [command_format, code, 0, 0, *data[2:]]
    if len(body) % 2:
        body.append(0)
    pairs = zip(body[0::2], body[1::2])
    total = sum(a | (b << 8) if order == "lsb_first" else (a << 8) | b for a, b in pairs)
    total = (total + device_id) & 0x7F7F
    return total & 0x7F, (total >> 8) & 0x7F


def _msc_checksum_bytes(message: dict, command: str) -> tuple:
    """cc cc as given: 0-16383 sent LSB first, or a name from
    _MSC_CHECKSUM_ORDERS, for which the msc branch of _build_message
    computes the bytes once the whole message is known (placeholder 00 00)."""
    checksum = _required(message, "checksum", command)
    if isinstance(checksum, str):
        _choice(message, "checksum", _MSC_CHECKSUM_ORDERS, command)
        return (0, 0)
    return _split14("checksum", checksum)


def _msc_2pc_prefix(message: dict, command: str) -> tuple:
    """cc cc nn nn: the checksum and sequence number, LSB first."""
    return (
        *_msc_checksum_bytes(message, command),
        *_split14("sequence_number", _required(message, "sequence_number", command)),
    )


def _msc_cue_data(message: dict) -> tuple:
    """d1 d2 d3 d4 (section 6.8); zeros when unknown, as the spec asks."""
    values = _as_list("cue_data", message.get("cue_data", [0, 0, 0, 0]))
    if len(values) != 4:
        raise ValueError(f"'cue_data' must have 4 values, got {values!r}")
    return tuple(_check_range("cue_data entry", v, 0, 127) for v in values)


def _msc_2pc_cue(message: dict, command: str, q_number_required: bool) -> tuple:
    """<Q_number> [00 <Q_list> [00 <Q_path>]], with Q_number required or not."""
    if q_number_required:
        q_number = _required(message, "q_number", command)
    else:
        q_number = message.get("q_number")
    return _encode_msc_cue_data(q_number, message.get("q_list"), message.get("q_path"))


def _msc_2pc(message: dict, command: str) -> tuple:
    # standby / go_2pc: cc cc nn nn d1-d4 <Q_number> [00 <Q_list> [00 <Q_path>]]
    # standing_by: cc cc nn nn hr mn sc fr ff [cue], the most time the cue
    #   can take; complete: cc cc nn nn [cue]; cancel: cc cc nn nn <Q_number>...
    prefix = _msc_2pc_prefix(message, command)
    if command in ("standby", "go_2pc"):
        return (*prefix, *_msc_cue_data(message), *_msc_2pc_cue(message, command, True))
    if command == "cancel":
        return (*prefix, *_msc_2pc_cue(message, command, True))
    if command == "standing_by":
        return (*prefix, *_time_code_bytes(message, "fractional_frames", command),
                *_msc_2pc_cue(message, command, False))
    return (*prefix, *_msc_2pc_cue(message, command, False))  # complete


def _msc_2pc_status(message: dict, command: str) -> tuple:
    # cancelled / abort: cc cc s1 s2 nn nn.
    names = _MSC_CANCELLED_STATUS if command == "cancelled" else _MSC_ABORT_STATUS
    status = _required(message, "status", command)
    code = names[_choice(message, "status", names, command)] if isinstance(status, str) \
        else _check_range("status", status, 0, 0xFFFC)
    if code % 4:
        raise ValueError(f"'status' must have its low 2 bits 0, got {code:#06x}")
    checksum = _msc_checksum_bytes(message, command)
    sequence = _split14("sequence_number", _required(message, "sequence_number", command))
    return (*checksum, (code // 4) & 0x7F, (code // 512) & 0x7F, *sequence)


_MSC_DATA_BUILDERS = {
    "go": _msc_cue,
    "stop": _msc_cue,
    "resume": _msc_cue,
    "go_off": _msc_cue,
    "load": _msc_cue,
    "timed_go": _msc_timed_go,
    "set": _msc_set,
    "fire": _msc_fire,
    # Sound Commands: one optional Q_list...
    **{name: _msc_single_cue_field("q_list", required=False) for name in (
        "standby_plus", "standby_minus", "sequence_plus", "sequence_minus",
        "start_clock", "stop_clock", "zero_clock", "mtc_chase_on",
        "mtc_chase_off",
    )},
    "set_clock": _msc_set_clock,
    # ...or a required Q_list / Q_path.
    "open_cue_list": _msc_single_cue_field("q_list", required=True),
    "close_cue_list": _msc_single_cue_field("q_list", required=True),
    "open_cue_path": _msc_single_cue_field("q_path", required=True),
    "close_cue_path": _msc_single_cue_field("q_path", required=True),
    # Two-Phase Commit.
    **{name: _msc_2pc for name in ("standby", "standing_by", "go_2pc", "complete", "cancel")},
    "cancelled": _msc_2pc_status,
    "abort": _msc_2pc_status,
}


# --- Channel and system messages ---------------------------------------------
# The types mido builds directly: type -> {field: default}, where _REQUIRED
# marks a field with no default. Types in _CHANNEL_TYPES also take 'channel'
# (default 0). mido checks the values' ranges.

_REQUIRED = object()

_MIDO_FIELDS: dict[str, dict] = {
    "note_on": {"note": _REQUIRED, "velocity": 64},
    "note_off": {"note": _REQUIRED, "velocity": 0},
    "control_change": {"control": _REQUIRED, "value": 0},
    "program_change": {"program": _REQUIRED},
    "pitchwheel": {"pitch": 0},
    "aftertouch": {"value": 0},  # Channel Pressure: one value per channel
    "polytouch": {"note": _REQUIRED, "value": 0},  # Polyphonic Key Pressure
    "quarter_frame": {"frame_type": _REQUIRED, "frame_value": _REQUIRED},
    "songpos": {"pos": 0},  # 0-16383
    "song_select": {"song": _REQUIRED},
    "tune_request": {},
    # System Real-Time: status byte only.
    "clock": {}, "start": {}, "stop": {}, "continue": {},
    "active_sensing": {}, "reset": {},
}
_CHANNEL_TYPES = frozenset({
    "note_on", "note_off", "control_change", "program_change", "pitchwheel",
    "aftertouch", "polytouch",
})


def _build_message(message: dict) -> "mido.Message":
    """Build one mido.Message (channel, system or SysEx) from a typed dict.

    Used by send (through _build_message_sequence) and write_midi_file.
    Raises KeyError for a missing required field and ValueError for a bad
    value; callers catch both. 'time' is the delta in ticks, used only in
    files.
    """
    msg_type = message.get("type")
    channel = message.get("channel", 0)
    time = message.get("time", 0)
    if msg_type in _MIDO_FIELDS:
        fields = {
            name: message[name] if default is _REQUIRED else message.get(name, default)
            for name, default in _MIDO_FIELDS[msg_type].items()
        }
        if msg_type in _CHANNEL_TYPES:
            fields["channel"] = channel
        return mido.Message(msg_type, time=time, **fields)
    if msg_type == "sysex":
        # Payload without F0/F7 (mido adds them). mido raises ValueError for
        # a byte outside 0-127.
        raw_data = message.get("data")
        if raw_data is None:
            raise KeyError("'data'")
        return mido.Message("sysex", data=tuple(_as_list("data", raw_data)), time=time)
    if msg_type == "mtc_full":
        # MTC Full Message (RP-004/008): jumps to a position in one
        # message instead of eight Quarter Frames.
        #   F0 7F <device_id> 01 01 hr mn sc fr F7
        # hr: _encode_smpte_hour_byte. frame_rate has no default because it
        # changes what the position means. device_id defaults to 7F (all
        # devices), the spec's default.
        device_id = _device_id(message)
        return _sysex(
            0x7F, device_id, 0x01, 0x01, *_time_code_bytes(message), time=time,
        )
    if msg_type == "mtc_nak":
        # MTC sync-dropped NAK (RP-004/008 p.4): the receiver treats it as
        # "tape stopped". Same bytes as file_dump's 'nak', without its
        # required packet_number:
        #   F0 7E <device_id> 7E <packet_number, default 0> F7
        # device_id defaults to 7F (all devices).
        device_id = _device_id(message)
        packet_number = _check_range(
            "packet_number", message.get("packet_number", 0), 0, 127,
        )
        return _sysex(0x7E, device_id, 0x7E, packet_number, time=time)
    if msg_type == "mtc_user_bits":
        # MTC User Bits (RP-004/008 p.5): the 32 SMPTE user bits, e.g. a reel
        # number or date.
        #   F0 7F <device_id> 01 02 u1 u2 u3 u4 u5 u6 u7 u8 u9 F7
        # u1-u8 = binary groups 1-8 (_user_bit_groups); u9 = 000000ji,
        # i = SMPTE bit 43, j = SMPTE bit 59 ('flags' 0-3). The spec's
        # device ID is 7F (whole system).
        device_id = _device_id(message)
        groups = _user_bit_groups(message)
        flags = _check_range("flags", message.get("flags", 0), 0, 3)
        return _sysex(0x7F, device_id, 0x01, 0x02, *groups, flags, time=time)
    if msg_type == "mmc":
        # MIDI Machine Control (RP-013); see _mmc_command_bytes. 'command'
        # sends one command; 'batch' (a list of command dicts) sends
        # several in one SysEx (RP-013 p.7). ('commands' is PROCEDURE's own
        # nested list.) The command string can't exceed
        # 48 bytes, and WAIT/RESUME must be alone in their message. Device
        # replies are decoded by the decode_mmc_response action.
        device_id, command_string = _mmc_command_string(message)
        if len(command_string) > 48:
            raise ValueError(
                f"the MMC command string is {len(command_string)} bytes; RP-013 "
                f"allows at most 48 per message (use 'segment': true)"
            )
        return _sysex(0x7F, device_id, 0x06, *command_string, time=time)
    if msg_type == "msc":
        # MIDI Show Control (RP-002/014); see _MSC_DATA_BUILDERS: the 11
        # General Category commands, the 15 Sound Commands and the 7
        # Two-Phase Commit commands. A 2PC 'checksum' given as a pairing
        # name is computed here (_msc_2pc_checksum).
        command_format = _msc_command_format(message)
        command = _choice(message, "command", _COMMANDS["msc"], "msc")
        device_id = _device_id(message)
        builder = _MSC_DATA_BUILDERS.get(command)
        data = builder(message, command) if builder else ()
        if command in _MSC_2PC_COMMANDS and isinstance(message.get("checksum"), str):
            data = (*_msc_2pc_checksum(device_id, command_format, _COMMANDS["msc"][command],
                                       data, message["checksum"]), *data[2:])
        return _sysex(
            0x7F, device_id, 0x02, command_format, _COMMANDS["msc"][command],
            *data, time=time,
        )
    if msg_type == "gm_system":
        # General MIDI System On/Off (Universal Non-Real Time, MIDI 1.0
        # Detailed Spec Table VIIa):
        #   F0 7E <device_id> 09 01 F7   on
        #   F0 7E <device_id> 09 02 F7   off
        # device_id defaults to 7F (all devices), as the spec suggests.
        command = _choice(
            message, "command", _COMMANDS["gm_system"], "gm_system",
        )
        device_id = _device_id(message)
        return _sysex(
            0x7E, device_id, 0x09, _COMMANDS["gm_system"][command], time=time,
        )
    if msg_type == "device_inquiry":
        # Device Inquiry (Universal Non-Real Time, MIDI 1.0 Detailed Spec):
        #   request: F0 7E <device_id> 06 01 F7
        #   reply:   F0 7E <device_id> 06 02 mm ff ff dd dd ss ss ss ss F7
        # mm: manufacturer ID, one byte 1-127 or three bytes 00 xx yy.
        # ff ff / dd dd: device family / member code, 14 bits, LSB first.
        # ss x4: software revision, device-specific format.
        command = _choice(
            message, "command", _COMMANDS["device_inquiry"], "device_inquiry",
        )
        device_id = _device_id(message)
        if command == "request":
            return _sysex(0x7E, device_id, 0x06, 0x01, time=time)

        manufacturer_id = _required(message, "manufacturer_id", "reply")
        family = _required(message, "device_family_code", "reply")
        member = _required(message, "device_family_member_code", "reply")
        software_revision = _required(message, "software_revision", "reply")

        if isinstance(manufacturer_id, int) and not isinstance(manufacturer_id, bool):
            if not (1 <= manufacturer_id <= 127):
                raise ValueError(
                    "'manufacturer_id' as a single int must be 1-127 (use "
                    "a 3-element list/tuple starting with 0 for the "
                    f"extended form), got {manufacturer_id!r}"
                )
            mfr_bytes: tuple = (manufacturer_id,)
        else:
            mfr_bytes = tuple(_as_list("manufacturer_id", manufacturer_id))
            if len(mfr_bytes) != 3 or mfr_bytes[0] != 0:
                raise ValueError(
                    "'manufacturer_id' as a list/tuple must have exactly "
                    f"3 bytes, the first being 0, got {manufacturer_id!r}"
                )
            for b in mfr_bytes:
                _check_range("manufacturer_id byte", b, 0, 127)

        revision_bytes = tuple(_as_list("software_revision", software_revision))
        if len(revision_bytes) != 4:
            raise ValueError(
                f"'software_revision' must be exactly 4 bytes, got "
                f"{software_revision!r}"
            )
        for b in revision_bytes:
            _check_range("software_revision byte", b, 0, 127)

        return _sysex(
            0x7E, device_id, 0x06, 0x02, *mfr_bytes,
            *_split14("device_family_code", family),
            *_split14("device_family_member_code", member),
            *revision_bytes,
            time=time,
        )
    if msg_type == "device_control":
        # Device Control (Universal Real Time, MIDI 1.0 Detailed Spec),
        # whole-device rather than per channel:
        #   master_volume:  F0 7F <device_id> 04 01 vv vv F7 (0 = off)
        #   master_balance: F0 7F <device_id> 04 02 bb bb F7 (0 = left,
        #                   16383 = right)
        #   master_fine_tuning:   F0 7F <device_id> 04 03 lsb msb F7
        #                         (8192 = A440, +/-100 cents; CA-025)
        #   master_coarse_tuning: F0 7F <device_id> 04 04 00 msb F7
        #                         (value = msb, 64 = A440, semitones; CA-025)
        #   global_parameter_control: F0 7F <device_id> 04 05 ... F7
        #                         (reverb/chorus; see _gpc_data)
        # value is 14 bits, LSB first, except for master_coarse_tuning.
        command = _choice(
            message, "command", _COMMANDS["device_control"], "device_control",
        )
        device_id = _device_id(message)
        if command == "global_parameter_control":
            data = _gpc_data(message)
        elif command == "master_coarse_tuning":
            data = (0x00, _check_range("value", _required(message, "value"), 0, 127))
        else:
            data = _split14("value", _required(message, "value"))
        return _sysex(
            0x7F, device_id, 0x04, _COMMANDS["device_control"][command], *data,
            time=time,
        )
    if msg_type == "controller_destination":
        # Controller Destination Setting (CA-022; GM2 4.6), Universal Real
        # Time. Assigns a controller to sound parameters, with a range each:
        #   F0 7F <device_id> 09 01|02 0n [pp rr]... F7  channel/poly pressure
        #   F0 7F <device_id> 09 03 0n cc [pp rr]... F7  control change cc
        # cc must be 01-1F or 40-5F. pp: a _CONTROLLER_DESTINATIONS name or
        # a number; rr's meaning comes from the recommended practice (GM2).
        command = _choice(
            message, "command", _COMMANDS["controller_destination"],
            "controller_destination",
        )
        device_id = _device_id(message)
        data = [_check_range("channel", channel, 0, 15)]
        if command == "control_change":
            control = _check_range("control", _required(message, "control", command), 0, 127)
            if not (0x01 <= control <= 0x1F or 0x40 <= control <= 0x5F):
                raise ValueError(f"'control' must be 01-1F or 40-5F, got {control!r}")
            data.append(control)
        data += _each(
            "destinations", _required_list(message, "destinations", command),
            lambda entry: (
                _named_or_number(entry, "parameter", _CONTROLLER_DESTINATIONS,
                                 "destinations"),
                _check_range("range", _required(entry, "range"), 0, 127),
            ),
        )
        return _sysex(
            0x7F, device_id, 0x09, _COMMANDS["controller_destination"][command],
            *data, time=time,
        )
    if msg_type == "key_based_instrument_control":
        # Key-Based Instrument Control (CA-023; GM2 4.8), Universal Real
        # Time: per-key controller values, e.g. for one drum in a kit.
        #   F0 7F <device_id> 0A 01 0n kk [nn vv]... F7
        # Values are relative, 40 = the preset, except absolute ones such as
        # Pan, Reverb Send and Chorus Send. 78/79 mean Fine/Coarse Tuning here.
        device_id = _device_id(message)
        key = _check_range("key", _required(message, "key"), 0, 127)
        pairs = _each(
            "controllers",
            _required_list(message, "controllers", "key_based_instrument_control"),
            _key_based_pair,
        )
        return _sysex(
            0x7F, device_id, 0x0A, 0x01, _check_range("channel", channel, 0, 15),
            key, *pairs, time=time,
        )
    if msg_type == "channel_mode":
        # Channel Mode messages: Control Change 120-127 (MIDI 1.0
        # Detailed Spec, Table IV), by name instead of controller number.
        #   120 All Sound Off            value=0 always
        #   121 Reset All Controllers    value=0 always
        #   122 Local Control            value: 0=Off, 127=On
        #   123 All Notes Off            value=0 always
        #   124 Omni Mode Off            value=0 always
        #   125 Omni Mode On             value=0 always
        #   126 Mono Mode On (Poly Off)  value=M, number of channels
        #                                (0-16; 0 is the spec's own
        #                                special case meaning "voices
        #                                equal the receiver's own channel
        #                                count")
        #   127 Poly Mode On (Mono Off)  value=0 always
        # 124-127 also act as All Notes Off on the receiver (Detailed
        # Spec, "Mode Messages as All Notes Off Messages").
        command = _choice(
            message, "command", _COMMANDS["channel_mode"], "channel_mode",
        )
        if command == "local_control":
            mode_value = 127 if _required(message, "on", command) else 0
        elif command == "mono_on":
            mode_value = _check_range(
                "channel_count", _required(message, "channel_count", command),
                0, 16,
            )
        else:
            mode_value = 0
        return mido.Message(
            "control_change", channel=channel,
            control=_COMMANDS["channel_mode"][command], value=mode_value,
            time=time,
        )
    if msg_type == "midi_tuning":
        # MIDI Tuning (MIDI Tuning Updated Specification); payloads in
        # _TUNING_PAYLOADS:
        #   bulk_dump_request        7E 08 00 tt
        #   bulk_dump_reply          7E 08 01 tt <name> [xx yy zz]x128 chksum
        #   note_change              7F 08 02 tt ll [kk xx yy zz]...
        #   bulk_dump_request_bank   7E 08 03 bb tt
        #   key_based_dump           7E 08 04 bb tt <name> [xx yy zz]x128 chksum
        #   scale_octave_dump_1byte  7E 08 05 bb tt <name> [xx]x12 chksum
        #   scale_octave_dump_2byte  7E 08 06 bb tt <name> [xx yy]x12 chksum
        #   note_change_bank         7F|7E 08 07 bb tt ll [kk xx yy zz]...
        #   scale_octave_1byte       7F|7E 08 08 ff gg hh [ss]x12
        #   scale_octave_2byte       7F|7E 08 09 ff gg hh [ss tt]x12
        # The updated spec says the original bulk dump's checksum text is
        # ambiguous and receivers may ignore it.
        command = _choice(
            message, "command", _COMMANDS["midi_tuning"], "midi_tuning",
        )
        device_id = _device_id(message)
        header = 0x7F if _tuning_real_time(message, command) else 0x7E
        data = (
            header, device_id, 0x08, _COMMANDS["midi_tuning"][command],
            *_TUNING_PAYLOADS[command](message, command),
        )
        if command in _TUNING_DUMPS:
            data = (*data, _xor_checksum(data))
        return _sysex(*data, time=time)
    if msg_type == "notation":
        # Notation Information (Universal Real Time, MIDI 1.0 Detailed
        # Spec):
        #   bar_marker: F0 7F <device_id> 03 01 aa aa F7
        #     signed 14-bit bar number, LSB first, two's complement:
        #     -8192 not running, 0 count-in, 1-8190 bar, 8191 unknown.
        #   time_signature_immediate (02) / _delayed (42):
        #     F0 7F <device_id> 03 <02|42> ln nn dd cc bb [nn dd ...] F7
        #     ln = number of bytes that follow. The spec's one-line summary
        #     shows an extra 'bb'; this follows its field list (ln nn dd cc
        #     bb), which matches mido's time_signature meta event.
        command = _choice(
            message, "command", _COMMANDS["notation"], "notation",
        )
        code = _COMMANDS["notation"][command]
        device_id = _device_id(message)

        if command == "bar_marker":
            bar = _check_range(
                "bar_number", _required(message, "bar_number", command),
                -8192, 8191,
            )
            return _sysex(
                0x7F, device_id, 0x03, code,
                *_split14("bar_number", bar & 0x3FFF),
                time=time,
            )

        # time_signature_immediate / time_signature_delayed
        numerator, denominator, clocks, n32nds = (
            _required(message, field) for field in (
                "numerator", "denominator", "clocks_per_click",
                "notated_32nd_notes_per_beat",
            )
        )
        data = (
            *_encode_time_signature_pair(numerator, denominator),
            _check_range("clocks_per_click", clocks, 0, 127),
            _check_range("notated_32nd_notes_per_beat", n32nds, 0, 127),
            *_each("compound", message.get("compound", []), lambda pair: (
                _encode_time_signature_pair(
                    _required(pair, "numerator"), _required(pair, "denominator"),
                )
            )),
        )
        return _sysex(0x7F, device_id, 0x03, code, len(data), *data, time=time)
    if msg_type == "mtc_cueing":
        # MTC Real Time Cueing (RP-004/008):
        #   F0 7F <device_id> 05 <sub-id#2> sl sm <additional info> F7
        # sl sm: 14-bit event number, LSB first. Additional info is a
        # nibblized MIDI message (_nibblize), or nibblized ASCII for
        # event_name. The Non-Real-Time set-up messages are 'mtc_cueing_nrt'.
        command = _choice(
            message, "command", _COMMANDS["mtc_cueing"], "mtc_cueing",
        )
        device_id = _device_id(message)
        sub_id2 = _COMMANDS["mtc_cueing"][command]
        if command == "special_system_stop":
            # Special type 04 00 in place of the event number; the spec
            # reserves the other special types here.
            return _sysex(0x7F, device_id, 0x05, sub_id2, 0x04, 0x00, time=time)
        return _sysex(
            0x7F, device_id, 0x05, sub_id2, *_cueing_event(message, command),
            time=time,
        )
    if msg_type == "mtc_cueing_nrt":
        # MTC Non-Real Time Cueing set-up (RP-004/008):
        #   F0 7E <device_id> 04 <sub-id#2> hr mn sc fr ff sl sm
        #   <additional info> F7
        # Like 'mtc_cueing', plus a time field, delete commands, and six
        # special types. Specials (sub-id#2 00) put the special type
        # (00 00-05 00) where the event number goes. Receivers ignore the
        # time field for types 01-04, so it's sent as zeros; 00 (time
        # code offset) and 05 (event list request) take a time.
        command = _choice(
            message, "command", _COMMANDS["mtc_cueing_nrt"], "mtc_cueing_nrt",
        )
        device_id = _device_id(message)
        sub_id2 = _COMMANDS["mtc_cueing_nrt"][command]
        if command in _MTC_CUEING_NRT_SPECIAL_TYPES:
            if command in ("special_time_code_offset", "special_event_list_request"):
                time_bytes = _time_code_bytes(message, "fractional_frames", command)
            else:
                # Types 01-04: receivers ignore the time field.
                time_bytes = (0, 0, 0, 0, 0)
            return _sysex(
                0x7E, device_id, 0x04, sub_id2, *time_bytes,
                _MTC_CUEING_NRT_SPECIAL_TYPES[command], 0x00,
                time=time,
            )
        return _sysex(
            0x7E, device_id, 0x04, sub_id2,
            *_time_code_bytes(message, "fractional_frames"),
            *_cueing_event(message, command),
            time=time,
        )
    if msg_type == "sample_dump":
        # Sample Dump Standard (MIDI 1.0 Detailed Spec pp.35-39), Universal
        # Non-Real Time:
        #   header:      F0 7E <id> 01 ss ss ee ff ff ff gg gg gg hh hh hh
        #                ii ii ii jj F7  (sample number, format 8-28 bits,
        #                period ns, length words, sustain loop start/end,
        #                loop type)
        #   data_packet: F0 7E <id> 02 kk <120 bytes> ll F7  (ll = XOR of
        #                every byte from 7E through the data)
        #   request:     F0 7E <id> 03 ss ss F7
        #   loop_points: F0 7E <id> 05 01 ss ss bb bb cc dd dd dd ee ee ee F7
        #   loop_points_request: F0 7E <id> 05 02 ss ss bb bb F7
        # loop_number "all" is 7F 7F (delete all / request all).
        command = _choice(message, "command", _COMMANDS["sample_dump"], "sample_dump")
        device_id = _device_id(message)
        if command == "data_packet":
            packet = _check_range(
                "packet_number", _required(message, "packet_number"), 0, 127,
            )
            data = (0x7E, device_id, 0x02, packet, *_sample_packet_data(message))
            return _sysex(*data, _xor_checksum(data), time=time)
        return _sysex(
            0x7E, device_id, *_COMMANDS["sample_dump"][command],
            *_sample_dump_payload(message, command), time=time,
        )
    if msg_type == "file_dump":
        # File Dump (Universal Non-Real Time, MIDI 1.0 Detailed Spec):
        #   request:     F0 7E <device_id> 07 03 ss <type> <name> F7
        #   header:      F0 7E <device_id> 07 01 ss <type> <len> <name> F7
        #   data_packet: F0 7E <device_id> 07 02 <pkt#> <count> <data>
        #                <chksm> F7
        #   eof/wait/cancel/nak/ack: F0 7E <device_id> <7B-7F> pp F7
        # device_id has no default: File Dump is point to point, and the
        # source ID (ss) can't be 7F. <type> is 4 printable ASCII
        # characters ("MIDI", "BIN "); <name> is printable ASCII. <len> is
        # 28 bits in four 7-bit bytes, LSB first, 0 = unknown. Checksum:
        # XOR of every byte after F0 up to the checksum. Sample Dump uses
        # these same handshakes.
        command = _choice(
            message, "command", _COMMANDS["file_dump"], "file_dump",
        )
        code = _COMMANDS["file_dump"][command]
        device_id = _device_id(message, default=None)

        if command in _FILE_DUMP_HANDSHAKE:
            # ack/nak name a packet; receivers ignore it for eof/wait/cancel.
            if command in ("ack", "nak"):
                packet = _required(message, "packet_number", command)
            else:
                packet = message.get("packet_number", 0)
            return _sysex(
                0x7E, device_id, code,
                _check_range("packet_number", packet, 0, 127),
                time=time,
            )

        if command == "data_packet":
            packet = _check_range(
                "packet_number", _required(message, "packet_number"), 0, 127,
            )
            encoded = _encode_file_dump_data(
                _required_list(message, "stored_bytes", command),
            )
            # <count> is the number of encoded data bytes minus 1.
            data = (0x7E, device_id, 0x07, code, packet, len(encoded) - 1, *encoded)
            return _sysex(*data, _xor_checksum(data), time=time)

        # request / header
        source = _check_range(
            "source_device_id", _required(message, "source_device_id"), 0, 126,
        )
        file_type = _as_text("file_type", _required(message, "file_type"))
        if len(file_type) != 4:
            raise ValueError(
                f"'file_type' must be exactly 4 characters (e.g. 'MIDI', "
                f"'TEXT', 'BIN '), got {len(file_type)} ({file_type!r})"
            )
        type_bytes = _ascii("file_type", file_type, printable=True)
        name_bytes = _ascii("filename", message.get("filename", ""), printable=True)
        if command == "request":
            return _sysex(
                0x7E, device_id, 0x07, code, source, *type_bytes, *name_bytes,
                time=time,
            )
        length = _check_range(
            "length", _required(message, "length", command), 0, 0xFFFFFFF,
        )
        return _sysex(
            0x7E, device_id, 0x07, code, source, *type_bytes,
            *((length >> shift) & 0x7F for shift in (0, 7, 14, 21)),
            *name_bytes,
            time=time,
        )
    raise ValueError(
        f"unknown message type {msg_type!r} — expected one of "
        f"{', '.join(_MESSAGE_TYPES)}"
    )


# Named RPNs: 0-4 from the MIDI 1.0 Detailed Spec Table IIIa, 5 from
# CA-026, 6 from MPE, and RPN Null (7F 7F; GM2 3.4.5), which deselects so
# that later Data Entry is ignored. NRPNs have no standard names.
_RPN_NAMED_PARAMETERS = {
    "pitch_bend_sensitivity": 0x0000,
    "fine_tuning": 0x0001,
    "coarse_tuning": 0x0002,
    "tuning_program_select": 0x0003,
    "tuning_bank_select": 0x0004,
    "modulation_depth_range": 0x0005,
    # MPE Configuration Message (M1-100-UM section 2.2.1): send on the
    # zone's Manager Channel (0 = Lower Zone, 15 = Upper Zone) with value =
    # number of Member Channels (0 turns the zone off) and msb_only true.
    "mpe_configuration": 0x0006,
    "null": 0x3FFF,
}


def _build_rpn_or_nrpn_sequence(message: dict, *, registered: bool) -> list:
    """Build the Control Change sequence that selects and sets an RPN
    (registered=True) or NRPN (registered=False):
      1. parameter LSB: CC100 (RPN) or CC98 (NRPN)
      2. parameter MSB: CC101 (RPN) or CC99 (NRPN)
      3. Data Entry MSB: CC6
      4. Data Entry LSB: CC38, left out when msb_only is true
    The order matches the MIDI Tuning spec's example
    (Bn 64 03 65 00 06 tt). Data Increment/Decrement (CC96/97) aren't
    implemented because the spec doesn't define their value byte; send
    them with 'control_change'.
    """
    channel = message.get("channel", 0)
    time = message.get("time", 0)

    if registered:
        name = message.get("parameter")
        number = message.get("parameter_number")
        if name is not None and number is not None:
            raise ValueError(
                "specify only ONE of 'parameter' or 'parameter_number', not both"
            )
        if name is not None:
            number = _RPN_NAMED_PARAMETERS[
                _choice(message, "parameter", _RPN_NAMED_PARAMETERS, "rpn")
            ]
        elif number is None:
            raise KeyError("'parameter' (or 'parameter_number')")
        select_lsb_cc, select_msb_cc = 100, 101
        if name == "null":
            return [
                mido.Message("control_change", channel=channel, control=100,
                             value=0x7F, time=time),
                mido.Message("control_change", channel=channel, control=101,
                             value=0x7F, time=0),
            ]
    else:
        number = _required(message, "parameter_number")
        select_lsb_cc, select_msb_cc = 98, 99
    number_lsb, number_msb = _split14("parameter_number", number)

    value = _required(message, "value")
    if message.get("msb_only", False):
        data_entry = [(6, _check_range("value", value, 0, 127))]
    else:
        value_lsb, value_msb = _split14("value", value)
        data_entry = [(6, value_msb), (38, value_lsb)]

    controls = [(select_lsb_cc, number_lsb), (select_msb_cc, number_msb), *data_entry]
    return [
        mido.Message(
            "control_change", channel=channel, control=control, value=cc_value,
            time=time if position == 0 else 0,
        )
        for position, (control, cc_value) in enumerate(controls)
    ]


def _build_quarter_frame_sequence(message: dict) -> list:
    """Build the 8 Quarter Frame messages for one SMPTE time (RP-004/008
    pp.1-4); the same position as 'mtc_full', for receivers that only
    read Quarter Frames. Spec example: 01:37:52:16 at 30fps non-drop ->
    F1 00, F1 11, F1 24, F1 33, F1 45, F1 52, F1 61, F1 76.

    Types 0-7 carry the low/high nibbles of frames, seconds, minutes and
    the hour byte (_encode_smpte_hour_byte, so the frame rate rides in
    type 7). direction "reverse" sends types 7 to 0, as tape running
    backwards does. This is a single snapshot, not a running MTC clock.
    """
    direction = message.get("direction", "forward")
    hr, mn, sc, fr = _time_code_bytes(message)
    if direction not in ("forward", "reverse"):
        raise ValueError(
            f"'direction' must be 'forward' or 'reverse', got {direction!r}"
        )
    time = message.get("time", 0)
    # Indexed by quarter frame type; direction only changes send order.
    values_by_type = [
        fr & 0xF, fr >> 4, sc & 0xF, sc >> 4, mn & 0xF, mn >> 4, hr & 0xF, hr >> 4,
    ]
    type_order = range(8) if direction == "forward" else range(7, -1, -1)
    return [
        mido.Message(
            "quarter_frame", frame_type=frame_type,
            frame_value=values_by_type[frame_type],
            time=(time if position == 0 else 0),
        )
        for position, frame_type in enumerate(type_order)
    ]


def _mmc_command_string(message: dict) -> tuple:
    """(device_id, command string) for an mmc message: 'command' or 'batch'.
    WAIT, RESUME and COMMAND SEGMENT must be alone in their message; an
    all-call command sends the message to 7F."""
    if message.get("batch") is not None:
        if message.get("command") is not None:
            raise ValueError("specify only ONE of 'command' or 'batch', not both")
        entries = _required_list(message, "batch", "mmc")
        for entry in entries:
            if _as_dict("batch entry", entry).get("type", "mmc") != "mmc":
                raise ValueError(f"'batch' entries must be mmc commands, got {entry!r}")
        names = [_choice(entry, "command", _COMMANDS["mmc"], "mmc") for entry in entries]
        alone = sorted({"wait", "resume", "command_segment"} & set(names))
        if alone and len(entries) > 1:
            raise ValueError(f"{alone} must be the only command in its message (RP-013)")
        command_string = [b for entry in entries for b in _mmc_command_bytes(entry)]
    else:
        names = [_choice(message, "command", _COMMANDS["mmc"], "mmc")]
        command_string = list(_mmc_command_bytes(message))
    device_id = _device_id(message)
    if set(names) & _MMC_ALL_CALL:
        device_id = 0x7F
    return device_id, command_string


def _build_mmc_segments(message: dict) -> list:
    """An mmc message sent as COMMAND SEGMENTs (RP-013 p.8): the command
    string split into pieces of 'segment_size' bytes (default 45, the most
    that fits in a 48-byte command field), first flagged 40h, counting down
    to 00 on the last. Splits can fall inside a command."""
    size = _check_range("segment_size", message.get("segment_size", 45), 1, 45)
    plain = {k: v for k, v in message.items() if k not in ("segment", "segment_size")}
    device_id, command_string = _mmc_command_string(plain)
    pieces = [command_string[i:i + size] for i in range(0, len(command_string), size)]
    if len(pieces) > 64:
        raise ValueError(f"{len(pieces)} segments; COMMAND SEGMENT allows at most 64")
    time = message.get("time", 0)
    return [
        _sysex(0x7F, device_id, 0x06, 0x53, len(piece) + 1,
               (0x40 if index == 0 else 0x00) | (len(pieces) - 1 - index), *piece,
               time=time if index == 0 else 0)
        for index, piece in enumerate(pieces)
    ]


def _build_message_sequence(message: dict) -> list:
    """Build the list of mido.Messages for a typed dict. rpn, nrpn and
    mtc_quarter_frame_sequence produce several; every other type is
    _build_message's single message in a one-item list.
    """
    msg_type = message.get("type")
    if msg_type == "rpn":
        return _build_rpn_or_nrpn_sequence(message, registered=True)
    if msg_type == "nrpn":
        return _build_rpn_or_nrpn_sequence(message, registered=False)
    if msg_type == "mtc_quarter_frame_sequence":
        return _build_quarter_frame_sequence(message)
    segment = message.get("segment", False) if msg_type == "mmc" else False
    if not isinstance(segment, bool):
        raise TypeError(f"'segment' must be true or false, got {segment!r}")
    if segment:
        return _build_mmc_segments(message)
    return [_build_message(message)]


# --- describe: field documentation per message type ------------------------
# _DOCS[type]: "summary", "fields" (name -> text) and an "example" message;
# a type with a 'command' has "commands" instead of "example", each command
# with its own "summary", "fields" and "example". describe merges the type's
# fields with the command's. Names and ranges come from the tables the
# builders use; test_midi1.py builds and decodes every example and checks
# that every field an example uses is documented.

_CHANNEL_DOC = "0-15 (MIDI channels 1-16), default 0"
_DEVICE_ID_DOC = "0-127, default 127 (all devices)"
_TIME_CODE_DOCS = {
    "hours": "0-23, required",
    "minutes": "0-59, required",
    "seconds": "0-59, required",
    "frames": "0-29, required",
    "frame_rate": f"one of {', '.join(_FRAME_RATE_BITS)}; required, no default",
}
_TC_EXAMPLE = {"hours": 1, "minutes": 37, "seconds": 52, "frames": 16}


def _no_fields(summary: str, example: dict) -> dict:
    return {"summary": summary, "fields": {}, "example": example}


# Shared field docs for MIDI Tuning, MTC Cueing and the dumps.
_TUNING_FREQUENCY_DOC = (
    "{'semitone': 0-127, 'cents': 0 <= cents < 100} (the equal-tempered "
    "semitone at or below the pitch, plus cents above it), or "
    "{'no_change': true}")
_TUNING_DOCS = {
    "bank": "0-127, required",
    "tuning_program": "0-127, required",
    "tuning_name": "up to 16 ASCII characters, default '' (space padded)",
    "notes": f"128 entries, key 0 first, required; each {_TUNING_FREQUENCY_DOC}",
    "changes": f"1-127 entries, required; each {{'key': 0-127}} plus {_TUNING_FREQUENCY_DOC}",
    "channels": "list of channels 0-15, required",
    "real_time": "true sends Real Time (F0 7F, applies now), false Non-Real Time; default true",
    "offsets_1byte": "12 values C to B, required; 0-127, 64 = 0 cents, 1 cent per step",
    "offsets_2byte": ("12 values C to B, required; 0-16383, 8192 = 0 cents, "
                      "200/16384 cent per step"),
}


def _tuning_doc(summary: str, fields: tuple, example: dict) -> dict:
    """A midi_tuning command entry; 'offsets' picks its 1- or 2-byte text
    from the command name."""
    two_byte = example["command"].endswith("2byte")
    docs = {name: _TUNING_DOCS[("offsets_2byte" if two_byte else "offsets_1byte")
                               if name == "offsets" else name] for name in fields}
    return {"summary": summary, "fields": docs, "example": {"type": "midi_tuning", **example}}


_CUEING_EVENT_DOC = "0-16383, required"
_CUEING_INFO_DOCS = {
    "additional_info_message": ("a message dict as 'send' takes (e.g. a note_on), "
                                "sent nibblized; or give 'additional_info_bytes'"),
    "additional_info_bytes": "list of 0-255, in place of 'additional_info_message'",
}


# MSC (RP-002/014) field docs.
_MSC_Q_DOCS = {
    "q_number": "cue number: ASCII digits and '.', e.g. '235.6'",
    "q_list": "cue list, same format; needs 'q_number' when sent with it",
    "q_path": "cue path, same format; needs 'q_list' when sent with it",
}
_MSC_TIME_DOCS = {**_TIME_CODE_DOCS, "fractional_frames": "0-99, required"}
_MSC_2PC_DOCS = {
    "checksum": ("required: 'lsb_first' or 'msb_first' computes it per MSC 6.5 "
                 "with that pairing of message bytes into 2-byte values (6.5 "
                 "doesn't say which is right), or give 0-16383 to send as is, "
                 "LSB first. A received 2PC message reports 'checksum_matches': "
                 "the pairings that reproduce its checksum"),
    "sequence_number": "0-16383, required",
}


def _msc_doc(command: str, summary: str, fields: dict, example: dict) -> dict:
    return {"summary": f"{summary} ({_COMMANDS['msc'][command]:02X}).", "fields": fields,
            "example": {"type": "msc", "command_format": "lighting", "command": command,
                        **example}}


def _msc_docs() -> dict:
    optional_cue = {name: f"{text}; optional" for name, text in _MSC_Q_DOCS.items()}
    q_list_only = {"q_list": "cue list (ASCII digits and '.'); optional"}
    out = {
        **{name: _msc_doc(name, summary, optional_cue, {"q_number": "1"})
           for name, summary in (
               ("go", "Start a cue; with no cue, the next one"),
               ("stop", "Stop a running cue; with no cue, all of them"),
               ("resume", "Resume a stopped cue; with no cue, all of them"),
               ("go_off", "Send a cue to its off state; with no cue, the current one"))},
        "load": _msc_doc("load", "Load a cue into standby, ready for GO",
                         {**optional_cue, "q_number": _MSC_Q_DOCS["q_number"] + "; required"},
                         {"q_number": "12.5"}),
        "timed_go": _msc_doc("timed_go", "GO at a stated time",
                             {**_MSC_TIME_DOCS, **optional_cue},
                             {**_TC_EXAMPLE, "frame_rate": "30nondrop",
                              "fractional_frames": 0, "q_number": "3"}),
        "set": _msc_doc("set", "Set a generic control to a value, optionally over a time",
                        {"control_number": "0-16383, required",
                         "control_value": "0-16383, required",
                         **{name: f"{text.replace(', required', '')}; the 6 time fields "
                                  "are given all together or not at all"
                            for name, text in _MSC_TIME_DOCS.items()}},
                        {"control_number": 1, "control_value": 8000}),
        "fire": _msc_doc("fire", "Trigger a macro", {"macro_number": "0-127, required"},
                         {"macro_number": 5}),
        "all_off": _msc_doc("all_off", "Turn all outputs off; RESTORE brings them back", {}, {}),
        "restore": _msc_doc("restore", "Restore what ALL_OFF turned off", {}, {}),
        "reset": _msc_doc("reset", "Stop all cues and load the top of the show", {}, {}),
        **{name: _msc_doc(name, summary, q_list_only, {})
           for name, summary in (
               ("standby_plus", "Sound: next cue to standby"),
               ("standby_minus", "Sound: previous cue to standby"),
               ("sequence_plus", "Sound: next parent cue to standby"),
               ("sequence_minus", "Sound: previous parent cue to standby"),
               ("start_clock", "Sound: start the auto-follow clock"),
               ("stop_clock", "Sound: stop the auto-follow clock"),
               ("zero_clock", "Sound: set the auto-follow clock to zero"),
               ("mtc_chase_on", "Sound: make the auto-follow clock follow incoming MTC"),
               ("mtc_chase_off", "Sound: stop following MTC"))},
        "set_clock": _msc_doc("set_clock", "Sound: set the auto-follow clock",
                              {**_MSC_TIME_DOCS, **q_list_only},
                              {**_TC_EXAMPLE, "frame_rate": "25", "fractional_frames": 0}),
        **{name: _msc_doc(name, summary, {field: f"{label} (ASCII digits and '.'); required"},
                          {field: "2"})
           for name, summary, field, label in (
               ("open_cue_list", "Sound: make a cue list active", "q_list", "cue list"),
               ("close_cue_list", "Sound: make a cue list inactive", "q_list", "cue list"),
               ("open_cue_path", "Sound: make a cue path active", "q_path", "cue path"),
               ("close_cue_path", "Sound: make a cue path inactive", "q_path", "cue path"))},
    }
    required_cue = {**optional_cue, "q_number": _MSC_Q_DOCS["q_number"] + "; required"}
    cue_data = {"cue_data": "4 values 0-127 (d1-d4, meaning per device), default all 0"}
    two_pc = {"checksum": "lsb_first", "sequence_number": 1}
    out.update({
        "standby": _msc_doc("standby", "Two-Phase Commit: controller asks a device to "
                            "prepare a cue", {**_MSC_2PC_DOCS, **cue_data, **required_cue},
                            {**two_pc, "q_number": "1"}),
        "standing_by": _msc_doc("standing_by", "Two-Phase Commit: device is ready; the "
                                "time is the most the cue can take",
                                {**_MSC_2PC_DOCS, **_MSC_TIME_DOCS, **optional_cue},
                                {**two_pc, **_TC_EXAMPLE, "frame_rate": "30nondrop",
                                 "fractional_frames": 0, "q_number": "1"}),
        "go_2pc": _msc_doc("go_2pc", "Two-Phase Commit: controller runs the cue",
                           {**_MSC_2PC_DOCS, **cue_data, **required_cue},
                           {**two_pc, "q_number": "1"}),
        "complete": _msc_doc("complete", "Two-Phase Commit: device finished the cue",
                             {**_MSC_2PC_DOCS, **optional_cue}, {**two_pc, "q_number": "1"}),
        "cancel": _msc_doc("cancel", "Two-Phase Commit: controller cancels a cue",
                           {**_MSC_2PC_DOCS, **required_cue}, {**two_pc, "q_number": "1"}),
        "cancelled": _msc_doc("cancelled", "Two-Phase Commit: device reports a cue cancelled",
                              {**_MSC_2PC_DOCS, "status": (
                                  f"one of {', '.join(_MSC_CANCELLED_STATUS)}, or a "
                                  "16-bit code with the low 2 bits 0; required")},
                              {**two_pc, "status": "completing"}),
        "abort": _msc_doc("abort", "Two-Phase Commit: device can't run the cue",
                          {**_MSC_2PC_DOCS, "status": (
                              f"one of {', '.join(_MSC_ABORT_STATUS)}, or a 16-bit "
                              "code with the low 2 bits 0; required")},
                          {**two_pc, "status": "timeout"}),
    })
    return out


# MMC (RP-013) field docs.
_MMC_SPEED_DOCS = {
    "speed": "0 to about 1023.99, required: a multiple of play speed (1 = play speed)",
    "reverse": "true for reverse, default false",
}
_MMC_TIME_FIELD_LIST = "time code fields: " + ", ".join(sorted(_WRITEABLE_INFO_FIELDS))


def _mmc_doc(command: str, summary: str, fields: dict, example: dict) -> dict:
    return {"summary": f"{summary} ({_COMMANDS['mmc'][command]:02X}).", "fields": fields,
            "example": {"type": "mmc", "command": command, **example}}


def _mmc_docs() -> dict:
    names = ", ".join(sorted(_INFO_FIELD_NAMES))
    writeable_codecs = sorted(n for n, c in _MMC_FIELD_CODECS.items() if c[0] is not None)
    nested = ("mmc command dicts, each with 'type': 'mmc' and its own fields; no "
              "PROCEDURE assemble inside")
    out = {
        **{name: _mmc_doc(name, summary, {}, {}) for name, summary in (
            ("stop", "Stop"), ("play", "Play"),
            ("deferred_play", "Play once a LOCATE in progress finishes; at once if none is"),
            ("fast_forward", "Fast forward"), ("rewind", "Rewind"),
            ("record_strobe", ("Punch in on the record-ready tracks; from a full stop "
                               "it starts playing, then records")),
            ("record_exit", "Punch out of record or rehearse"),
            ("record_pause", ("From PAUSE, enter record-pause: nothing recorded yet, "
                              "ready to punch in smoothly")),
            ("pause", "Pause"), ("eject", "Eject the media"),
            ("chase", "Follow and lock to the SELECTED MASTER CODE"),
            ("command_error_reset", ("Clear COMMAND ERROR's error-halt flag so "
                                     "commands are processed again")),
            ("mmc_reset", ("Reset MMC to power-up: empties the update list, deletes "
                           "procedures, events and groups, clears errors")))},
        **{name: _mmc_doc(name, summary, {}, {}) for name, summary in (
            ("wait", ("Handshake: the receiver's buffer is full, stop sending; "
                      "always to 7F and alone in its message")),
            ("resume", "Handshake: ready again after WAIT; always to 7F and alone"))},
        "locate": _mmc_doc(
            "locate", "Move to a time: TARGET with the time fields, or I/F with a GP register",
            {**_TIME_CODE_DOCS, "subframes": "0-99, required with the time fields",
             "name": "one of gp0-gp7, in place of the time fields"},
            {**_TC_EXAMPLE, "subframes": 0, "frame_rate": "25"}),
        **{name: _mmc_doc(name, summary, _MMC_SPEED_DOCS, {"speed": 1.5})
           for name, summary in (
               ("variable_play", "Play at a variable speed"),
               ("search", "Move at a speed with monitoring"),
               ("shuttle", "Move at a speed, monitoring not required"),
               ("deferred_variable_play", "VARIABLE PLAY once a LOCATE in progress finishes"),
               ("record_strobe_variable", "RECORD STROBE at a variable speed"))},
        "step": _mmc_doc("step", "Move a number of STEP LENGTH units (default half a frame)",
                         {"quantity": "0-63, required", "reverse": "true steps back, default false"},
                         {"quantity": 2}),
        "assign_system_master": _mmc_doc(
            "assign_system_master", "Make a device the system master; sent to 7F",
            {"target_device_id": "0-127, required; 127 dis-assigns"}, {"target_device_id": 2}),
        "generator_command": _mmc_doc(
            "generator_command", "Time code generator: stop, run or copy/jam",
            {"action": f"one of {', '.join(_MMC_GENERATOR_ACTIONS)}, required"},
            {"action": "run"}),
        "midi_time_code_command": _mmc_doc(
            "midi_time_code_command", "MIDI Time Code output: off or follow its source",
            {"action": f"one of {', '.join(_MMC_MTC_COMMAND_ACTIONS)}, required"},
            {"action": "follow"}),
        "drop_frame_adjust": _mmc_doc(
            "drop_frame_adjust", "Convert a 30 fps time code field to drop-frame in place",
            {"name": f"a writeable {_MMC_TIME_FIELD_LIST}; required"}, {"name": "gp0"}),
        "move": _mmc_doc(
            "move", "Copy one Information Field into another",
            {"destination": f"a writeable {_MMC_TIME_FIELD_LIST}; required",
             "source": "any Information Field name, required"},
            {"destination": "gp0", "source": "selected_time_code"}),
        **{name: _mmc_doc(
            name, f"destination = source_1 {sign} source_2 (time code fields)",
            {"destination": f"a writeable {_MMC_TIME_FIELD_LIST}; required",
             "source_1": "any Information Field name, required",
             "source_2": "any Information Field name, required"},
            {"destination": "gp1", "source_1": "gp1", "source_2": "gp2"})
           for name, sign in (("add", "+"), ("subtract", "-"))},
        "group": _mmc_doc(
            "group", "Assign devices to a group, or remove them",
            {"action": f"one of {', '.join(_MMC_GROUP_ACTIONS)}, required",
             "group": "0-127, required; 127 (dis_assign only) means every group",
             "device_ids": "list of 0-127, required; 127 means every device"},
            {"action": "assign", "group": 3, "device_ids": [1, 2]}),
        "procedure": _mmc_doc(
            "procedure", "Stored command lists: assemble, delete, set (select) or execute",
            {"action": f"one of {', '.join(_MMC_PROCEDURE_ACTIONS)}, required",
             "procedure": "0-126, required; 127 means all for delete and set",
             "commands": f"assemble only, required: a list of {nested}, and no "
                         "EXECUTE of the procedure being assembled"},
            {"action": "assemble", "procedure": 1,
             "commands": [{"type": "mmc", "command": "stop"},
                          {"type": "mmc", "command": "locate", "name": "gp0"}]}),
        "event": _mmc_doc(
            "event", "Commands run when a time code reaches a time: define, delete, set or test",
            {"action": f"one of {', '.join(_MMC_EVENT_ACTIONS)}, required",
             "event": "0-126, required; 127 means all for delete and set",
             "trigger_source": (f"define: one of {', '.join(_MMC_EVENT_TRIGGER_SOURCES)}, "
                                "required"),
             "name": "define: the GP register (gp0-gp7) holding the trigger time, required",
             "trigger_command": ("define: one mmc command dict ('type': 'mmc'), required; "
                                 "no EVENT define or PROCEDURE assemble"),
             "direction": f"define: one of {', '.join(_MMC_EVENT_DIRECTIONS)}, required",
             "all_speeds": "define: fire at any speed, not only play speed; default false",
             "non_delete": "define: stay armed after firing; default false"},
            {"action": "define", "event": 1, "trigger_source": "selected_time_code",
             "name": "gp1", "direction": "forward",
             "trigger_command": {"type": "mmc", "command": "play"}}),
        "read": _mmc_doc(
            "read", "Ask for Information Field values; the device answers with an MMC "
            "response (poll decodes it as mmc_response)",
            {"names": f"list of Information Field names, required: {names}"},
            {"names": ["selected_time_code", "motion_control_tally"]}),
        "update": _mmc_doc(
            "update", "Send fields now and again whenever they change (begin), or stop (end)",
            {"action": f"one of {', '.join(_MMC_UPDATE_ACTIONS)}, required",
             "names": "list of Information Field names (as read), required; 'all' with end"},
            {"action": "begin", "names": ["selected_time_code"]}),
        "write": _mmc_doc(
            "write", "Set Information Field values",
            {"fields": (
                "list of {'name', ...data}, required. A writeable "
                f"{_MMC_TIME_FIELD_LIST} takes hours, minutes, seconds, frames, "
                "frame_rate, subframes (0-99, default 0) and the flags "
                f"{', '.join(_TIME_CODE_FLAGS)} (default false). Count-prefixed "
                f"writeable fields: {', '.join(writeable_codecs)}; describe with "
                "'field' gives each one's data")},
            {"fields": [{"name": "gp0", **_TC_EXAMPLE, "frame_rate": "25"},
                        {"name": "stop_mode", "value": "enable_monitoring"}]}),
        "masked_write": _mmc_doc(
            "masked_write", "Change some bits of a track bitmap field",
            {"fields": (
                "list of {'name', 'byte_number', 'mask', 'data'}, required. name: "
                f"one of {', '.join(sorted(_MASK_WRITEABLE_INFO_FIELDS))}; byte_number "
                "0-127 (0 = the first bitmap byte); mask and data 0-127, only the "
                "mask's 1 bits change")},
            {"fields": [{"name": "track_mute", "byte_number": 0, "mask": 0x20, "data": 0x20}]}),
        "command_segment": _mmc_doc(
            "command_segment", "One piece of a command string too long for one message; "
            "'segment': true on any mmc message builds these",
            {"first": "true on the first segment, default false",
             "remaining": "0-63, required: segments still to come (0 on the last)",
             "data": "list of 0-127, required: this piece of the command string"},
            {"first": True, "remaining": 1, "data": [0x44, 0x06, 0x01, 0x21]}),
    }
    return out


# MMC Information Fields (RP-013 section 6), for describe with 'field'. Each
# entry: "summary", "data" (the keys a WRITE entry takes or a response
# carries) and, for a field WRITE accepts, a "write_example" entry.
_MMC_TC_DATA = (
    "Standard Time Code: hours, minutes, seconds, frames, frame_rate; then "
    "subframes 0-99 (default 0), or with use_status_byte true the status flags "
    "estimated, invalid, video_field_1, no_time_code; plus the flags "
    "color_frame, blank (never loaded), negative. All flags default false. A "
    "response carries the same keys.")
_MMC_SHORT_DATA = (
    "Response only (UPDATE sends it): frames, negative, use_status_byte, then "
    "subframes or the status flags.")
_MMC_BITMAP_DATA = (
    "Track bitmap. WRITE: 'bitmap_bytes' (list of 0-127) or 'active_tracks' "
    "(1-317) with the flags video, time_code_track, aux_track_a, aux_track_b; "
    "tracks left out are switched off. Response: byte_count, bitmap_bytes and "
    "the same tracks and flags.")
_MMC_FIELD_SUMMARIES = {
    "selected_time_code": "The device's current position (its own, or 'slave', time code).",
    "selected_master_code": "The master time code that CHASE synchronizes to.",
    "requested_offset": "Wanted offset: SELECTED TIME CODE - SELECTED MASTER CODE, for CHASE.",
    "actual_offset": "Measured SELECTED TIME CODE - SELECTED MASTER CODE.",
    "lock_deviation": "How far the position is from master + REQUESTED OFFSET.",
    "generator_time_code": "The time code generator's current value.",
    "midi_time_code_input": "The most recent incoming MIDI Time Code.",
    **{f"gp{n}": (f"General purpose time register {n} (LOCATE I/F, EVENT, MOVE, "
                  "ADD, SUBTRACT).") for n in range(8)},
    "signature": "The commands and fields the device supports, as bitmaps.",
    "update_rate": "Minimum frames between UPDATE transmissions (default 1).",
    "command_error": "The last command error: flags, level, error code, offending command.",
    "command_error_level": "Errors with a code below this level are reported.",
    "time_standard": "The device's frame rate.",
    "selected_time_code_source": "Where SELECTED TIME CODE comes from.",
    "selected_time_code_userbits": "Userbits most recently read from SELECTED TIME CODE.",
    "motion_control_tally": "The current motion state and process, with success levels.",
    "velocity_tally": "Actual transport speed, whatever the motion state.",
    "stop_mode": "Whether recorded material is monitored while stopped.",
    "fast_mode": "Whether recorded material is monitored in fast forward and rewind.",
    "record_mode": "What RECORD STROBE does: insert, assemble, rehearse or crash.",
    "record_status": "Actual record and rehearse activity.",
    "track_record_status": "Tracks currently recording or rehearsing.",
    "track_record_ready": "Tracks in record ready (the next RECORD STROBE records them).",
    "global_monitor": "Playback or input monitoring for all tracks.",
    "record_monitor": "When record tracks monitor their inputs.",
    "track_sync_monitor": "Tracks with synchronous playback on their outputs.",
    "track_input_monitor": "Tracks whose outputs monitor their inputs.",
    "step_length": "The STEP unit, in 1/100 frame (default 50, half a frame).",
    "play_speed_reference": "Play speed from the device itself or an external reference.",
    "fixed_speed": "Nominal play speed on a multi-speed device.",
    "lifter_defeat": "Defeat a reel-to-reel's tape lifters so tape touches the heads.",
    "control_disable": "Ignore transport and sync commands from every source.",
    "resolved_play_mode": "How PLAY establishes its speed.",
    "chase_mode": "How CHASE synchronizes.",
    "generator_command_tally": "The last GENERATOR COMMAND and how it went.",
    "generator_set_up": "Generator run and copy/jam references, source and mode.",
    "generator_userbits": "Userbits the generator sends.",
    "midi_time_code_command_tally": "The last MIDI TIME CODE COMMAND and how it went.",
    "midi_time_code_set_up": "What the MIDI Time Code output sends, and its source.",
    "procedure_response": "A stored PROCEDURE's commands (answer to READ).",
    "event_response": "A stored EVENT's definition (answer to READ).",
    "track_mute": "Tracks with muted outputs.",
    "vitc_insert_enable": "Whether VITC is inserted into recorded video, and on which lines.",
    "failure": "A failure needing the operator, with text for display.",
    "response_error": "Response only: fields the device doesn't support.",
    "response_segment": "Response only: one piece of a long response; poll reassembles them.",
}
_MMC_FIELD_SUMMARIES.update({
    f"short_{name}": (f"Short form of {name}: frames and subframes or status only, "
                      "for frequent UPDATE responses.")
    for name in ("selected_time_code", "selected_master_code", "requested_offset",
                 "actual_offset", "lock_deviation", "generator_time_code",
                 "midi_time_code_input", *(f"gp{n}" for n in range(8)))
})
_MMC_STRUCTURED_DATA = {
    "time_standard": ("WRITE: 'frame_rate' (24, 25, 30drop, 30nondrop) and "
                      "'encoding': 'field_definition' (default, 0 tt 00000) or "
                      "'unshifted' (the code alone, as RP-013's appendix sends). "
                      "Response: frame_rate, and encoding when unshifted."),
    "record_status": ("Response: activity (" + ", ".join(_MMC_RECORD_ACTIVITY) + "), "
                      "local_record_inhibit, local_rehearse_inhibit, no_tracks_active."),
    "vitc_insert_enable": ("'control' (" + ", ".join(_MMC_VITC_CONTROL) + "), "
                           "'first_line' and 'second_line' (0-127 or 'local'); a "
                           "response carries the same."),
    "signature": ("Response: version, version_extension, commands and fields (names "
                  "or 0xNN), extended_commands, extended_fields, command_bitmaps, "
                  "field_bitmaps."),
    "command_error": ("Response: error_halt, procedure_assemble_error, "
                      "event_define_error, unsolicited, previously_transmitted, level, "
                      "error (a name from RP-013's list, or a number), and offset and "
                      "command_bytes when the device names the command."),
    "motion_control_tally": ("Response: motion_state, motion_state_success, "
                             "motion_process ('none' if idle), motion_process_success."),
    "velocity_tally": "Response: speed (multiple of play speed) and reverse.",
    "selected_time_code_userbits": "Response: binary_groups (8 values 0-15) and flags (0-3).",
    "generator_userbits": ("'binary_groups' (8 values 0-15) or 'characters' (4), "
                           "and 'flags' 0-3 (default 0); a response carries "
                           "binary_groups and flags."),
    "generator_command_tally": ("Response: command (stop, run, copy_jam), success, "
                                "source_data_lost, frame_sync_reference_lost."),
    "generator_set_up": ("'run_reference' (" + ", ".join(_MMC_GENERATOR_RUN_REFERENCE)
                         + "), 'copy_jam_reference' (" + ", ".join(_MMC_GENERATOR_JAM_REFERENCE)
                         + "), 'copy_jam_source' (" + ", ".join(_MMC_GENERATOR_JAM_SOURCE)
                         + "), 'copy_jam_mode' (" + ", ".join(_MMC_GENERATOR_JAM_MODE)
                         + "); names or numbers (the two references 0-7, source and "
                         "mode 0-127). A response carries the same."),
    "midi_time_code_command_tally": "Response: command (off, follow) and success.",
    "midi_time_code_set_up": ("The flags " + ", ".join(_MMC_MTC_FLAGS) + " (default "
                              "false) and 'source' (" + ", ".join(_MMC_MTC_SOURCE)
                              + "); a response carries the same."),
    "procedure_response": ("Response: procedure and its commands (mmc command dicts), "
                           "or procedure 'invalid'."),
    "event_response": ("Response: event, direction, all_speeds, non_delete, "
                       "trigger_source, event_time, trigger_command; or event 'invalid'."),
    "failure": "Response: text.",
    "response_error": "Response: unsupported_fields (names or 0xNN).",
    "response_segment": "Response: first, remaining, data.",
}
_MMC_FIELD_WRITE_EXAMPLES: dict = {
    "time_standard": {"frame_rate": "25"},
    "vitc_insert_enable": {"control": "enable", "first_line": 16, "second_line": 18},
    "generator_userbits": {"characters": "REEL", "flags": 0},
    "generator_set_up": {"run_reference": "internal_standard",
                         "copy_jam_reference": "source_frame_edges",
                         "copy_jam_source": "selected_time_code", "copy_jam_mode": "continue"},
    "midi_time_code_set_up": {"transmit_while_stopped": True, "source": "generator_time_code"},
}


def _mmc_field_doc(name: str) -> dict:
    """describe's entry for one Information Field (or response-only name)."""
    code = _INFO_FIELD_NAMES[name] if name in _INFO_FIELD_NAMES else _MMC_RESPONSE_ONLY_NAMES[name]
    codec = _MMC_FIELD_CODECS.get(name)
    example: dict | None
    if name in _MASK_WRITEABLE_INFO_FIELDS:
        access = "write and masked_write"
    elif name in _WRITEABLE_INFO_FIELDS or (codec is not None and codec[0] is not None):
        access = "write"
    else:
        access = "read only"
    if code < 0x20:
        data = _MMC_TC_DATA
        example = {**_TC_EXAMPLE, "frame_rate": "25"}
    elif code < 0x40:
        data = _MMC_SHORT_DATA
        example = None
    elif name in _MMC_BYTE_FIELDS:
        names = _MMC_BYTE_FIELDS[name]
        data = ("'value': " + (f"one of {', '.join(names)}, or 0-127" if names else "0-127")
                + "; a response carries 'value'.")
        example = {"value": next(iter(names)) if names else {"step_length": 50}.get(name, 1)}
    elif name in _TRACK_BITMAP_INFO_FIELDS:
        data = _MMC_BITMAP_DATA if access != "read only" else (
            "Response: byte_count, bitmap_bytes, active_tracks (1-317) and the "
            "flags video, time_code_track, aux_track_a, aux_track_b.")
        example = {"active_tracks": [1, 2]}
    else:
        data = _MMC_STRUCTURED_DATA[name]
        example = _MMC_FIELD_WRITE_EXAMPLES.get(name)
    out: dict = {"code": f"0x{code:02X}", "access": access,
                 "summary": _MMC_FIELD_SUMMARIES[name], "data": data}
    if access != "read only":
        # A writeable field with no example gets an empty one, which fails
        # check_describe's build.
        out["write_example"] = {"name": name, **(example or {})}
    return out


def _cueing_docs(msg_type: str, time_fields: dict, example_time: dict) -> dict:
    """The command entries shared by mtc_cueing and mtc_cueing_nrt (the
    latter adds a time and delete commands)."""
    out = {}
    for name in _COMMANDS[msg_type]:
        if name.startswith("special_"):
            continue
        fields = {**time_fields, "event_number": _CUEING_EVENT_DOC}
        example = {"type": msg_type, "command": name, **example_time, "event_number": 12}
        what = name.replace("_with_info", "").replace("_", " ")
        summary = f"{what.capitalize()} ({_COMMANDS[msg_type][name]:02X})."
        if name.endswith("_with_info"):
            fields.update(_CUEING_INFO_DOCS)
            example["additional_info_message"] = {"type": "note_on", "note": 60, "velocity": 100}
            summary = f"{what.capitalize()}, with a MIDI message to act on ({_COMMANDS[msg_type][name]:02X})."
        if name == "event_name":
            fields["event_name"] = "ASCII text, required"
            example["event_name"] = "Door slam"
            summary = f"Name for an event number ({_COMMANDS[msg_type][name]:02X})."
        out[name] = {"summary": summary, "fields": fields, "example": example}
    return out


_DOCS: dict = {
    "note_on": {
        "summary": "Note On (9n).",
        "fields": {"channel": _CHANNEL_DOC, "note": "0-127, required",
                   "velocity": "0-127, default 64; 0 is a note off to receivers"},
        "example": {"type": "note_on", "channel": 0, "note": 60, "velocity": 100},
    },
    "note_off": {
        "summary": "Note Off (8n).",
        "fields": {"channel": _CHANNEL_DOC, "note": "0-127, required",
                   "velocity": "0-127 (release velocity), default 0"},
        "example": {"type": "note_off", "channel": 0, "note": 60},
    },
    "control_change": {
        "summary": ("Control Change (Bn). Controllers 120-127 decode as "
                    "channel_mode; for RPN/NRPN use rpn and nrpn."),
        "fields": {"channel": _CHANNEL_DOC, "control": "0-127, required",
                   "value": "0-127, default 0"},
        "example": {"type": "control_change", "channel": 0, "control": 7, "value": 100},
    },
    "program_change": {
        "summary": "Program Change (Cn).",
        "fields": {"channel": _CHANNEL_DOC, "program": "0-127, required"},
        "example": {"type": "program_change", "channel": 0, "program": 5},
    },
    "pitchwheel": {
        "summary": "Pitch Bend (En).",
        "fields": {"channel": _CHANNEL_DOC, "pitch": "-8192 to 8191, default 0 (center)"},
        "example": {"type": "pitchwheel", "channel": 0, "pitch": 4096},
    },
    "aftertouch": {
        "summary": "Channel Pressure (Dn): one value for the whole channel.",
        "fields": {"channel": _CHANNEL_DOC, "value": "0-127, default 0"},
        "example": {"type": "aftertouch", "channel": 0, "value": 64},
    },
    "polytouch": {
        "summary": "Polyphonic Key Pressure (An): one value per note.",
        "fields": {"channel": _CHANNEL_DOC, "note": "0-127, required",
                   "value": "0-127, default 0"},
        "example": {"type": "polytouch", "channel": 0, "note": 60, "value": 64},
    },
    "channel_mode": {
        "summary": ("Channel Mode messages: Control Change 120-127 by name. "
                    "omni_off, omni_on, mono_on and poly_on also act as All "
                    "Notes Off on the receiver."),
        "fields": {"channel": _CHANNEL_DOC},
        "commands": {
            "all_sound_off": _no_fields("CC 120, value 0.", {
                "type": "channel_mode", "command": "all_sound_off"}),
            "reset_all_controllers": _no_fields("CC 121, value 0.", {
                "type": "channel_mode", "command": "reset_all_controllers"}),
            "local_control": {
                "summary": "CC 122: local keyboard connected to the synth (127) or not (0).",
                "fields": {"on": "true or false, required"},
                "example": {"type": "channel_mode", "command": "local_control", "on": False},
            },
            "all_notes_off": _no_fields("CC 123, value 0.", {
                "type": "channel_mode", "command": "all_notes_off"}),
            "omni_off": _no_fields("CC 124, value 0.", {
                "type": "channel_mode", "command": "omni_off"}),
            "omni_on": _no_fields("CC 125, value 0.", {
                "type": "channel_mode", "command": "omni_on"}),
            "mono_on": {
                "summary": "CC 126: Mono mode.",
                "fields": {"channel_count": (
                    "0-16, required: the number of channels; 0 means as many "
                    "as the receiver has voices")},
                "example": {"type": "channel_mode", "command": "mono_on", "channel_count": 4},
            },
            "poly_on": _no_fields("CC 127, value 0.", {
                "type": "channel_mode", "command": "poly_on"}),
        },
    },
    "quarter_frame": {
        "summary": ("MTC Quarter Frame (F1), one piece. For a whole time use "
                    "mtc_quarter_frame_sequence."),
        "fields": {"frame_type": "0-7, required (which nibble)",
                   "frame_value": "0-15, required"},
        "example": {"type": "quarter_frame", "frame_type": 0, "frame_value": 0},
    },
    "songpos": {
        "summary": "Song Position Pointer (F2), in MIDI beats (6 clocks each).",
        "fields": {"pos": "0-16383, default 0"},
        "example": {"type": "songpos", "pos": 32},
    },
    "song_select": {
        "summary": "Song Select (F3).",
        "fields": {"song": "0-127, required"},
        "example": {"type": "song_select", "song": 3},
    },
    "tune_request": _no_fields("Tune Request (F6).", {"type": "tune_request"}),
    "clock": _no_fields("Timing Clock (F8), 24 per quarter note; for a steady "
                        "stream use the run_clock action.", {"type": "clock"}),
    "start": _no_fields("Start (FA).", {"type": "start"}),
    "continue": _no_fields("Continue (FB).", {"type": "continue"}),
    "stop": _no_fields("Stop (FC).", {"type": "stop"}),
    "active_sensing": _no_fields(
        "Active Sensing (FE). Received only on an input opened with "
        "'active_sensing': true.", {"type": "active_sensing"}),
    "reset": _no_fields("System Reset (FF).", {"type": "reset"}),
    "sysex": {
        "summary": "Any System Exclusive message, as raw data bytes.",
        "fields": {"data": ("list of 0-127, required; without the F0 and F7, "
                            "which are added")},
        "example": {"type": "sysex", "data": [0x7E, 0x7F, 0x06, 0x01]},
    },
    "rpn": {
        "summary": ("Registered Parameter change: CC 100/101 select it, CC 6 "
                    "(and CC 38) set it. Sent as 3 or 4 Control Changes."),
        "fields": {
            "channel": _CHANNEL_DOC,
            "parameter": (f"one of {', '.join(_RPN_NAMED_PARAMETERS)}; or give "
                          "'parameter_number'. 'null' sends only the deselect "
                          "(CC 100/101 = 127) and takes no value."),
            "parameter_number": "0-16383, in place of 'parameter'",
            "value": "0-16383 (14-bit), or 0-127 with msb_only; required",
            "msb_only": "true sends only CC 6 (7-bit value); default false",
        },
        "example": {"type": "rpn", "channel": 0, "parameter": "pitch_bend_sensitivity",
                    "value": 2, "msb_only": True},
    },
    "nrpn": {
        "summary": ("Non-Registered Parameter change: CC 98/99 select it, CC 6 "
                    "(and CC 38) set it. Sent as 3 or 4 Control Changes."),
        "fields": {
            "channel": _CHANNEL_DOC,
            "parameter_number": "0-16383, required (MSB << 7 | LSB)",
            "value": "0-16383 (14-bit), or 0-127 with msb_only; required",
            "msb_only": "true sends only CC 6 (7-bit value); default false",
        },
        "example": {"type": "nrpn", "channel": 0, "parameter_number": 0x1F28,
                    "value": 8192},
    },
    "mtc_quarter_frame_sequence": {
        "summary": ("The 8 MTC Quarter Frames (F1) for one time, the same "
                    "position as mtc_full. A snapshot, not a running clock."),
        "fields": {**_TIME_CODE_DOCS,
                   "direction": "'forward' (types 0-7, default) or 'reverse' (7-0)"},
        "example": {"type": "mtc_quarter_frame_sequence", **_TC_EXAMPLE,
                    "frame_rate": "30nondrop"},
    },
    "mtc_full": {
        "summary": "MTC Full Message (F0 7F id 01 01): jump to a position in one message.",
        "fields": {**_TIME_CODE_DOCS, "device_id": _DEVICE_ID_DOC},
        "example": {"type": "mtc_full", **_TC_EXAMPLE, "frame_rate": "25"},
    },
    "mtc_nak": {
        "summary": ("MTC sync-dropped NAK (F0 7E id 7E pp): the receiver treats "
                    "it as tape stopped. Same bytes as file_dump 'nak'."),
        "fields": {"device_id": _DEVICE_ID_DOC, "packet_number": "0-127, default 0"},
        "example": {"type": "mtc_nak", "device_id": 0x7F},
    },
    "mtc_user_bits": {
        "summary": "MTC User Bits (F0 7F id 01 02): the 32 SMPTE user bits.",
        "fields": {
            "device_id": _DEVICE_ID_DOC,
            "binary_groups": "8 values 0-15, group 1 first; or give 'characters'",
            "characters": ("4 characters (codes 0-255), in place of "
                           "'binary_groups'; the first character is groups 8 and 7"),
            "flags": "0-3, default 0: bit 0 = SMPTE bit 43, bit 1 = SMPTE bit 59",
        },
        "example": {"type": "mtc_user_bits", "characters": "ABCD"},
    },
    "gm_system": {
        "summary": "General MIDI System On/Off (F0 7E id 09 nn).",
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "on": _no_fields("GM1 System On (01).", {"type": "gm_system", "command": "on"}),
            "off": _no_fields("GM System Off (02).", {"type": "gm_system", "command": "off"}),
            "gm2_on": _no_fields("GM2 System On (03).",
                                 {"type": "gm_system", "command": "gm2_on"}),
        },
    },
    "device_inquiry": {
        "summary": "Identity Request and Identity Reply (F0 7E id 06 nn).",
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "request": _no_fields("Identity Request (01); devices answer with a reply.",
                                  {"type": "device_inquiry", "command": "request"}),
            "reply": {
                "summary": "Identity Reply (02), as a device sends it.",
                "fields": {
                    "manufacturer_id": ("required: 1-127, or a list of 3 bytes "
                                        "starting with 0 (extended ID)"),
                    "device_family_code": "0-16383, required",
                    "device_family_member_code": "0-16383, required",
                    "software_revision": "list of 4 bytes 0-127, required",
                },
                "example": {"type": "device_inquiry", "command": "reply", "device_id": 0x10,
                            "manufacturer_id": 0x41, "device_family_code": 0x1C5,
                            "device_family_member_code": 0,
                            "software_revision": [0, 3, 0, 0]},
            },
        },
    },
    "device_control": {
        "summary": "Device Control (F0 7F id 04 nn): settings for the whole device.",
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "master_volume": {
                "summary": "Master Volume (01).",
                "fields": {"value": "0-16383, required; 0 is off"},
                "example": {"type": "device_control", "command": "master_volume",
                            "value": 16383},
            },
            "master_balance": {
                "summary": "Master Balance (02).",
                "fields": {"value": "0-16383, required; 0 left, 8192 center, 16383 right"},
                "example": {"type": "device_control", "command": "master_balance",
                            "value": 8192},
            },
            "master_fine_tuning": {
                "summary": "Master Fine Tuning (03, CA-025).",
                "fields": {"value": "0-16383, required; 8192 is A440, the range +/-100 cents"},
                "example": {"type": "device_control", "command": "master_fine_tuning",
                            "value": 8192},
            },
            "master_coarse_tuning": {
                "summary": "Master Coarse Tuning (04, CA-025), in semitones.",
                "fields": {"value": "0-127, required; 64 is A440"},
                "example": {"type": "device_control", "command": "master_coarse_tuning",
                            "value": 64},
            },
            "global_parameter_control": {
                "summary": "Global Parameter Control (05, GM2 4.4): effect parameters.",
                "fields": {
                    "effect": (f"one of {', '.join(_GPC_EFFECTS)}; or give "
                               "'slot_path'. Allows the GM2 parameter names"),
                    "slot_path": "list of [msb, lsb] pairs (0-127), in place of 'effect'",
                    "parameter_width": "1-127 bytes per parameter, default 1",
                    "value_width": "1-127 bytes per value, default 1",
                    "parameters": (
                        "list of {'parameter', 'value'}, required. With width 1: "
                        "0-127, and with 'effect' a parameter can be a name ("
                        + "; ".join(f"{effect}: {', '.join(names)}"
                                    for effect, names in _GPC_PARAMETERS.items())
                        + "). Wider: a list of that many bytes"),
                },
                "example": {"type": "device_control", "command": "global_parameter_control",
                            "effect": "reverb",
                            "parameters": [{"parameter": "type", "value": 4}]},
            },
        },
    },
    "controller_destination": {
        "summary": ("Controller Destination Setting (F0 7F id 09 nn, CA-022): "
                    "route a controller to sound parameters."),
        "fields": {
            "device_id": _DEVICE_ID_DOC,
            "channel": _CHANNEL_DOC,
            "destinations": (
                "list of {'parameter', 'range'}, required. parameter: one of "
                f"{', '.join(_CONTROLLER_DESTINATIONS)}, or 0-127. range: 0-127, "
                "meaning per GM2 4.6: pitch 0x28-0x58 = -24 to +24 semitones; "
                "filter_cutoff -9600 to +9450 cents; amplitude 0 to 127/64 x "
                "100%; all three 0x40 = no change. lfo_pitch_depth 0-600 cents, "
                "lfo_filter_depth 0-2400 cents, lfo_amplitude_depth 0-100%, "
                "0 = none"),
        },
        "commands": {
            "channel_pressure": {
                "summary": "Channel Pressure as the source (01).",
                "fields": {},
                "example": {"type": "controller_destination", "command": "channel_pressure",
                            "channel": 0,
                            "destinations": [{"parameter": "pitch", "range": 0x42}]},
            },
            "poly_pressure": {
                "summary": "Polyphonic Key Pressure as the source (02).",
                "fields": {},
                "example": {"type": "controller_destination", "command": "poly_pressure",
                            "channel": 0,
                            "destinations": [{"parameter": "amplitude", "range": 0x50}]},
            },
            "control_change": {
                "summary": "A Control Change as the source (03).",
                "fields": {"control": "required, 01-1F or 40-5F"},
                "example": {"type": "controller_destination", "command": "control_change",
                            "channel": 0, "control": 0x01,
                            "destinations": [{"parameter": "filter_cutoff", "range": 0x60}]},
            },
        },
    },
    "key_based_instrument_control": {
        "summary": ("Key-Based Instrument Control (F0 7F id 0A 01, CA-023): "
                    "controller values for one key, such as one drum in a kit."),
        "fields": {
            "device_id": _DEVICE_ID_DOC,
            "channel": _CHANNEL_DOC,
            "key": "0-127, required",
            "controllers": (
                "list of {'control', 'value'} (each 0-127), required. Values are "
                "relative (64 = as preset) except absolute ones such as pan and "
                "the reverb/chorus sends; 0x78/0x79 mean fine/coarse tuning. Not "
                "allowed: " + ", ".join(f"{c:#04x}" for c in sorted(_KEY_BASED_EXCLUDED_CONTROLS))),
        },
        "example": {"type": "key_based_instrument_control", "channel": 9, "key": 38,
                    "controllers": [{"control": 0x07, "value": 0x50}]},
    },
    "notation": {
        "summary": "Notation Information (F0 7F id 03 nn): bar markers and time signatures.",
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "bar_marker": {
                "summary": "Bar Marker (01).",
                "fields": {"bar_number": ("-8192 to 8191, required: -8192 not "
                                          "running, 0 count-in, 8191 unknown")},
                "example": {"type": "notation", "command": "bar_marker", "bar_number": 5},
            },
            **{name: {
                "summary": f"Time Signature, {when} ({code}).",
                "fields": {
                    "numerator": "0-127, required",
                    "denominator": "required, a power of 2 (the note value: 2, 4, 8, ...)",
                    "clocks_per_click": "0-127, required (MIDI clocks per metronome click)",
                    "notated_32nd_notes_per_beat": "0-127, required (usually 8)",
                    "compound": "list of {'numerator', 'denominator'} for compound signatures",
                },
                "example": {"type": "notation", "command": name, "numerator": 3,
                            "denominator": 4, "clocks_per_click": 24,
                            "notated_32nd_notes_per_beat": 8},
            } for name, when, code in (
                ("time_signature_immediate", "taking effect now", "02"),
                ("time_signature_delayed", "taking effect at the next bar", "42"))},
        },
    },
    "midi_tuning": {
        "summary": ("MIDI Tuning (F0 7E|7F id 08 nn, Updated Specification). Dumps "
                    "get their checksum added."),
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "bulk_dump_request": _tuning_doc(
                "Ask for a tuning program's Bulk Dump (00).", ("tuning_program",),
                {"command": "bulk_dump_request", "tuning_program": 0}),
            "bulk_dump_reply": _tuning_doc(
                "Bulk Tuning Dump (01): a frequency for each of the 128 keys.",
                ("tuning_program", "tuning_name", "notes"),
                {"command": "bulk_dump_reply", "tuning_program": 0, "tuning_name": "Equal",
                 "notes": [{"semitone": k, "cents": 0} for k in range(128)]}),
            "note_change": _tuning_doc(
                "Single Note Tuning Change (02, Real Time): retune some keys now.",
                ("tuning_program", "changes"),
                {"command": "note_change", "tuning_program": 0,
                 "changes": [{"key": 69, "semitone": 69, "cents": 50}]}),
            "bulk_dump_request_bank": _tuning_doc(
                "Ask for a Bulk Dump from a bank (03).", ("bank", "tuning_program"),
                {"command": "bulk_dump_request_bank", "bank": 0, "tuning_program": 0}),
            "key_based_dump": _tuning_doc(
                "Key-Based Tuning Dump (04): bulk dump with a bank.",
                ("bank", "tuning_program", "tuning_name", "notes"),
                {"command": "key_based_dump", "bank": 0, "tuning_program": 0,
                 "notes": [{"no_change": True}] * 128}),
            "scale_octave_dump_1byte": _tuning_doc(
                "Scale/Octave Tuning Dump, 1-byte form (05).",
                ("bank", "tuning_program", "tuning_name", "offsets"),
                {"command": "scale_octave_dump_1byte", "bank": 0, "tuning_program": 0,
                 "offsets": [64] * 12}),
            "scale_octave_dump_2byte": _tuning_doc(
                "Scale/Octave Tuning Dump, 2-byte form (06).",
                ("bank", "tuning_program", "tuning_name", "offsets"),
                {"command": "scale_octave_dump_2byte", "bank": 0, "tuning_program": 0,
                 "offsets": [8192] * 12}),
            "note_change_bank": _tuning_doc(
                "Single Note Tuning Change with a bank (07).",
                ("bank", "tuning_program", "changes", "real_time"),
                {"command": "note_change_bank", "bank": 0, "tuning_program": 0,
                 "changes": [{"key": 60, "semitone": 60, "cents": 25}]}),
            "scale_octave_1byte": _tuning_doc(
                "Scale/Octave Tuning, 1-byte form (08): the same offset for each "
                "pitch class on the given channels.",
                ("channels", "offsets", "real_time"),
                {"command": "scale_octave_1byte", "channels": [0],
                 "offsets": [64, 50, 64, 78, 64, 64, 50, 64, 50, 64, 78, 50]}),
            "scale_octave_2byte": _tuning_doc(
                "Scale/Octave Tuning, 2-byte form (09).",
                ("channels", "offsets", "real_time"),
                {"command": "scale_octave_2byte", "channels": [0, 1], "offsets": [8192] * 12}),
        },
    },
    "mtc_cueing": {
        "summary": ("MTC Real Time Cueing (F0 7F id 05 nn): cue events now. For "
                    "set-up with times, use mtc_cueing_nrt."),
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "special_system_stop": _no_fields(
                "System Stop (00, special type 04 00).",
                {"type": "mtc_cueing", "command": "special_system_stop"}),
            **_cueing_docs("mtc_cueing", {}, {}),
        },
    },
    "mtc_cueing_nrt": {
        "summary": ("MTC Non-Real Time Cueing set-up (F0 7E id 04 nn): an event "
                    "list with times, and delete commands."),
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            **{name: _no_fields(f"Special: {name[8:].replace('_', ' ')} (00, type "
                                f"{code:02X} 00).",
                                {"type": "mtc_cueing_nrt", "command": name})
               for name, code in _MTC_CUEING_NRT_SPECIAL_TYPES.items()
               if name not in ("special_time_code_offset", "special_event_list_request")},
            **{name: {
                "summary": f"Special: {what} (00, type {_MTC_CUEING_NRT_SPECIAL_TYPES[name]:02X} 00).",
                "fields": {**_TIME_CODE_DOCS, "fractional_frames": "0-99, required"},
                "example": {"type": "mtc_cueing_nrt", "command": name, **_TC_EXAMPLE,
                            "frame_rate": "25", "fractional_frames": 0},
            } for name, what in (("special_time_code_offset", "time code offset"),
                                 ("special_event_list_request", "event list request"))},
            **_cueing_docs("mtc_cueing_nrt",
                           {**_TIME_CODE_DOCS, "fractional_frames": "0-99, required"},
                           {**_TC_EXAMPLE, "frame_rate": "25", "fractional_frames": 0}),
        },
    },
    "sample_dump": {
        "summary": ("Sample Dump Standard (F0 7E id nn). Handshakes (ack, nak, "
                    "wait, cancel, eof) are file_dump commands."),
        "fields": {"device_id": _DEVICE_ID_DOC},
        "commands": {
            "header": {
                "summary": "Dump Header (01).",
                "fields": {
                    "sample_number": "0-16383, required",
                    "sample_format": "8-28 bits per word, required",
                    "sample_period": "0-2097151 nanoseconds per sample, required",
                    "sample_length": "0-2097151 words, required",
                    "sustain_loop_start": "0-2097151 (word number), required",
                    "sustain_loop_end": "0-2097151 (word number), required",
                    "loop_type": f"one of {', '.join(_SAMPLE_LOOP_TYPES)}, required",
                },
                "example": {"type": "sample_dump", "command": "header", "sample_number": 1,
                            "sample_format": 16, "sample_period": 22676,
                            "sample_length": 44100, "sustain_loop_start": 0,
                            "sustain_loop_end": 44099, "loop_type": "forward"},
            },
            "data_packet": {
                "summary": ("Data Packet (02): 120 data bytes, zero-padded, with "
                            "its checksum added."),
                "fields": {
                    "packet_number": "0-127, required (counts up, wrapping)",
                    "words": ("sample words, each 0 to 2^sample_format - 1 (0 = "
                              "full negative); needs 'sample_format'. Or give 'data'"),
                    "sample_format": "8-28, with 'words'",
                    "data": "up to 120 raw bytes 0-127, in place of 'words'",
                },
                "example": {"type": "sample_dump", "command": "data_packet",
                            "packet_number": 0, "sample_format": 16,
                            "words": [0x8000, 0xFFFF, 0x0000]},
            },
            "request": {
                "summary": "Dump Request (03).",
                "fields": {"sample_number": "0-16383, required"},
                "example": {"type": "sample_dump", "command": "request", "sample_number": 1},
            },
            "loop_points": {
                "summary": "Loop Point Transmission (05 01).",
                "fields": {
                    "sample_number": "0-16383, required",
                    "loop_number": "0-16383, or 'all' (deletes all loops), required",
                    "loop_type": f"one of {', '.join(_SAMPLE_LOOP_TYPES)}, required",
                    "loop_start": "0-2097151 (word number), required",
                    "loop_end": "0-2097151 (word number), required",
                },
                "example": {"type": "sample_dump", "command": "loop_points",
                            "sample_number": 1, "loop_number": 0, "loop_type": "forward",
                            "loop_start": 100, "loop_end": 44000},
            },
            "loop_points_request": {
                "summary": "Loop Point Request (05 02).",
                "fields": {"sample_number": "0-16383, required",
                           "loop_number": "0-16383, or 'all', required"},
                "example": {"type": "sample_dump", "command": "loop_points_request",
                            "sample_number": 1, "loop_number": "all"},
            },
        },
    },
    "mmc": {
        "summary": ("MIDI Machine Control (F0 7F id 06 ...): transport, locate, "
                    "Information Fields, procedures and events. Give one 'command', "
                    "or several in 'batch'."),
        "fields": {
            "device_id": _DEVICE_ID_DOC,
            "batch": ("in place of 'command': a list of mmc command dicts sent in "
                      "one message. WAIT, RESUME and COMMAND SEGMENT must be alone"),
            "segment": ("true sends a command string over 48 bytes as COMMAND "
                        "SEGMENT messages; default false (over 48 bytes is an error)"),
            "segment_size": "1-45 bytes per segment with 'segment', default 45",
            "field": ("describe only: an Information Field name (as read lists) "
                      "gives that field's access, data format and a WRITE example"),
        },
        "commands": _mmc_docs(),
    },
    "msc": {
        "summary": ("MIDI Show Control (F0 7F id 02 format command): the General "
                    "commands, the Sound commands and Two-Phase Commit."),
        "fields": {
            "device_id": ("0-127: 00-6F one device, 70-7E a group, 7F all "
                          "(default 127)"),
            "command_format": (f"one of {', '.join(_MSC_FORMATS)}; or give "
                               "'command_format_raw'"),
            "command_format_raw": ("0-127, in place of 'command_format', for a "
                                   "narrower category (e.g. 0x02 moving lights, "
                                   "0x04 strobes)"),
        },
        "commands": _msc_docs(),
    },
    "file_dump": {
        "summary": ("File Dump (F0 7E id 07 nn) and the generic handshakes "
                    "(F0 7E id 7B-7F pp), which Sample Dump uses too."),
        "fields": {"device_id": "0-127, required (File Dump is point to point)"},
        "commands": {
            "header": {
                "summary": "Header (01): announces a file.",
                "fields": {
                    "source_device_id": "0-126, required (the sender)",
                    "file_type": "4 printable ASCII characters, required, e.g. 'MIDI', 'BIN '",
                    "length": "0-268435455 bytes, required; 0 = unknown",
                    "filename": "printable ASCII, default ''",
                },
                "example": {"type": "file_dump", "command": "header", "device_id": 1,
                            "source_device_id": 2, "file_type": "MIDI",
                            "length": 1024, "filename": "song.mid"},
            },
            "data_packet": {
                "summary": "Data Packet (02): up to 112 file bytes, encoded, with its checksum added.",
                "fields": {"packet_number": "0-127, required",
                           "stored_bytes": "1-112 file bytes, each 0-255, required"},
                "example": {"type": "file_dump", "command": "data_packet", "device_id": 1,
                            "packet_number": 0, "stored_bytes": [0x4D, 0x54, 0x68, 0x64, 0xFF]},
            },
            "request": {
                "summary": "Request (03): ask a device to send a file.",
                "fields": {
                    "source_device_id": "0-126, required (who asks)",
                    "file_type": "4 printable ASCII characters, required",
                    "filename": "printable ASCII, default ''",
                },
                "example": {"type": "file_dump", "command": "request", "device_id": 1,
                            "source_device_id": 2, "file_type": "MIDI",
                            "filename": "song.mid"},
            },
            **{name: {
                "summary": f"{label} handshake ({code:02X}).",
                "fields": {"packet_number": (
                    "0-127, required" if name in ("ack", "nak")
                    else "0-127, default 0 (receivers ignore it)")},
                "example": {"type": "file_dump", "command": name, "device_id": 1,
                            "packet_number": 0},
            } for name, label, code in (
                ("eof", "End of File", 0x7B), ("wait", "Wait", 0x7C),
                ("cancel", "Cancel", 0x7D), ("nak", "NAK (resend this packet)", 0x7E),
                ("ack", "ACK (packet received)", 0x7F))},
        },
    },
}


def _describe(tool_input: dict) -> str:
    """The describe action: _DOCS for 'message' {'type', 'command'}, or the
    type list when no type is given."""
    message = tool_input.get("message") or {}
    msg_type = message.get("type")
    if msg_type is None:
        return json.dumps({
            "status": "ok",
            "types": {t: _DOCS[t]["summary"] if t in _DOCS else None for t in _MESSAGE_TYPES},
            "note": ("describe with 'message': {'type': T} for T's fields; a "
                     "'time' field (delta ticks) applies in write_midi_file only"),
        })
    if msg_type not in _MESSAGE_TYPES:
        return _err(f"unknown type {msg_type!r}; describe with no 'message' lists them")
    doc = _DOCS.get(msg_type)
    if doc is None:
        return _err(f"no field documentation for {msg_type!r}")
    field = message.get("field")
    if msg_type == "mmc" and field is not None:
        if field not in _MMC_FIELD_SUMMARIES:
            return _err(f"unknown MMC Information Field {field!r}; valid: "
                        f"{sorted(_MMC_FIELD_SUMMARIES)}")
        return json.dumps({"status": "ok", "type": "mmc", "field": field,
                           **_mmc_field_doc(field)})
    out = {"status": "ok", "type": msg_type, "summary": doc["summary"],
           "fields": dict(doc["fields"])}
    commands = doc.get("commands")
    if commands is None:
        out["example"] = doc["example"]
        return json.dumps(out)
    command = message.get("command")
    if command is None:
        out["fields"]["command"] = "required, one of the names in 'commands'"
        out["commands"] = {name: entry["summary"] for name, entry in commands.items()}
        return json.dumps(out)
    if command not in commands:
        return _err(f"unknown {msg_type} command {command!r}; valid: {sorted(commands)}")
    entry = commands[command]
    out.update(command=command, summary=entry["summary"], example=entry["example"])
    out["fields"].update(entry["fields"])
    return json.dumps(out)


# mido's meta message types (mido.midifiles.meta._META_SPEC_BY_TYPE).
# Valid only in write_midi_file tracks, not on a live send.
_META_TYPES = frozenset({
    "track_name", "text", "copyright", "lyrics", "marker", "cue_marker",
    "instrument_name", "device_name", "set_tempo", "time_signature",
    "key_signature", "smpte_offset", "midi_port", "channel_prefix",
    "sequence_number", "sequencer_specific", "end_of_track",
})


def _build_meta_message(message: dict) -> "mido.MetaMessage":
    """Build a mido.MetaMessage for write_midi_file. Fields pass straight
    to mido, which supplies defaults and rejects unknown names. set_tempo
    also accepts 'bpm', converted with mido.bpm2tempo().
    """
    msg_type = message.get("type")
    if msg_type not in _META_TYPES:
        raise ValueError(f"unknown meta message type {msg_type!r}")
    time = message.get("time", 0)
    kwargs = {
        k: v for k, v in message.items()
        if k not in ("type", "time", "bpm")
    }
    if msg_type == "set_tempo" and "bpm" in message:
        kwargs["tempo"] = mido.bpm2tempo(message["bpm"])
    if "data" in kwargs and isinstance(kwargs["data"], list):
        kwargs["data"] = tuple(kwargs["data"])
    return mido.MetaMessage(msg_type, time=time, **kwargs)


# --- Decoding ----------------------------------------------------------------
# _decode_message turns a received mido.Message into the dict _build_message
# takes, with every field filled in. A SysEx decode is kept only when
# rebuilding it gives back the same bytes; otherwise the message is returned
# as plain {"type": "sysex", "data": [...]}. Either way, building the result
# reproduces the original message.


def _name_for(table: dict, code: "int | tuple") -> str:
    """The name whose value is `code` in a name -> code table (codes are
    ints, or tuples where a command takes two bytes, as in sample_dump)."""
    for name, value in table.items():
        if value == code:
            return name
    raise KeyError(code)


def _join14(lsb: int, msb: int) -> int:
    return lsb | (msb << 7)


def _decode_channel_mode(msg: "mido.Message") -> "dict | None":
    """Control Change 120-127 with a value the spec allows, as channel_mode;
    None for any other Control Change."""
    try:
        command = _name_for(_COMMANDS["channel_mode"], msg.control)
    except KeyError:
        return None
    out = {"type": "channel_mode", "command": command, "channel": msg.channel}
    if command == "local_control":
        if msg.value not in (0, 127):
            return None
        out["on"] = msg.value == 127
    elif command == "mono_on":
        if msg.value > 16:
            return None
        out["channel_count"] = msg.value
    elif msg.value != 0:
        return None
    return out


def _decode_gm_system(data: tuple) -> dict:
    _, device_id, _, code = data
    return {
        "type": "gm_system", "command": _name_for(_COMMANDS["gm_system"], code),
        "device_id": device_id,
    }


def _decode_device_inquiry(data: tuple) -> dict:
    device_id, code, rest = data[1], data[3], data[4:]
    command = _name_for(_COMMANDS["device_inquiry"], code)
    out: dict = {"type": "device_inquiry", "command": command, "device_id": device_id}
    if command == "reply":
        if rest[0] == 0:
            manufacturer_id, rest = list(rest[:3]), rest[3:]
        else:
            manufacturer_id, rest = rest[0], rest[1:]
        if len(rest) != 8:
            raise ValueError("Identity Reply has the wrong length")
        out.update(
            manufacturer_id=manufacturer_id,
            device_family_code=_join14(rest[0], rest[1]),
            device_family_member_code=_join14(rest[2], rest[3]),
            software_revision=list(rest[4:8]),
        )
    return out


def _decode_gpc(payload) -> dict:
    """Inverse of _gpc_data."""
    slot_count, parameter_width, value_width = payload[0], payload[1], payload[2]
    slots = [list(payload[3 + 2 * i:5 + 2 * i]) for i in range(slot_count)]
    rest = payload[3 + 2 * slot_count:]
    step = parameter_width + value_width
    if not rest or len(rest) % step or parameter_width < 1 or value_width < 1:
        raise ValueError("Global Parameter Control data doesn't fit its widths")
    out: dict = {}
    effect = next(
        (name for name, path in _GPC_EFFECTS.items() if slots == [list(path)]), None,
    )
    if effect is not None:
        out["effect"] = effect
    else:
        out["slot_path"] = slots
    if parameter_width != 1:
        out["parameter_width"] = parameter_width
    if value_width != 1:
        out["value_width"] = value_width
    names = _GPC_PARAMETERS.get(effect, {}) if effect is not None else {}
    parameters = []
    for i in range(0, len(rest), step):
        parameter: list | int | str = list(rest[i:i + parameter_width])
        value = list(rest[i + parameter_width:i + step])
        if parameter_width == 1:
            code = rest[i]
            parameter = _name_for(names, code) if code in names.values() else code
        parameters.append({
            "parameter": parameter, "value": value[0] if value_width == 1 else value,
        })
    out["parameters"] = parameters
    return out


def _decode_device_control(data: tuple) -> dict:
    device_id, code, payload = data[1], data[3], data[4:]
    command = _name_for(_COMMANDS["device_control"], code)
    out: dict = {"type": "device_control", "command": command, "device_id": device_id}
    if command == "global_parameter_control":
        out.update(_decode_gpc(payload))
    elif command == "master_coarse_tuning":
        lsb, msb = payload
        if lsb != 0x00:
            raise ValueError("Master Coarse Tuning LSB must be 00")
        out["value"] = msb
    else:
        lsb, msb = payload
        out["value"] = _join14(lsb, msb)
    return out


def _decode_controller_destination(data: tuple) -> dict:
    device_id, code, channel = data[1], data[3], data[4]
    command = _name_for(_COMMANDS["controller_destination"], code)
    out: dict = {"type": "controller_destination", "command": command,
                 "device_id": device_id, "channel": channel}
    pairs = data[5:]
    if command == "control_change":
        out["control"], pairs = data[5], data[6:]
    if not pairs or len(pairs) % 2:
        raise ValueError("destinations must be parameter/range pairs")
    out["destinations"] = [
        {"parameter": _name_for(_CONTROLLER_DESTINATIONS, p)
         if p in _CONTROLLER_DESTINATIONS.values() else p, "range": r}
        for p, r in zip(pairs[0::2], pairs[1::2])
    ]
    return out


def _decode_key_based(data: tuple) -> dict:
    if data[3] != 0x01:
        raise ValueError("only the Basic Message (0A 01) is defined")
    device_id, channel, key, pairs = data[1], data[4], data[5], data[6:]
    if not pairs or len(pairs) % 2:
        raise ValueError("controllers must be number/value pairs")
    return {
        "type": "key_based_instrument_control", "device_id": device_id,
        "channel": channel, "key": key,
        "controllers": [{"control": c, "value": v}
                        for c, v in zip(pairs[0::2], pairs[1::2])],
    }


def _decode_time_code(hr: int, mn: int, sc: int, fr: int, *fifth: int,
                      subframe_field: "str | None" = None) -> dict:
    """The time fields of a Standard Time Code with the flags off; inverse of
    _time_code_bytes."""
    out = {
        "hours": hr & 0x1F, "minutes": mn, "seconds": sc, "frames": fr,
        "frame_rate": _name_for(_FRAME_RATE_BITS, (hr >> 5) & 0x3),
    }
    if subframe_field:
        out[subframe_field] = fifth[0]
    return out


def _denibblize(nibbles) -> list:
    """Inverse of _nibblize: low nibble first, two nibbles per byte."""
    if len(nibbles) % 2:
        raise ValueError("odd number of nibbles")
    return [nibbles[i] | (nibbles[i + 1] << 4) for i in range(0, len(nibbles), 2)]


def _decode_file_dump_data(encoded) -> list:
    """Inverse of _encode_file_dump_data: groups of a sign byte plus up to 7
    low-7-bit bytes."""
    stored: list = []
    for start in range(0, len(encoded), 8):
        sign, *low = encoded[start:start + 8]
        stored += [b | (((sign >> (6 - i)) & 1) << 7) for i, b in enumerate(low)]
    return stored


def _checksum_ok(data: tuple) -> bool:
    """Whether the last byte is the XOR of the bytes before it."""
    return _xor_checksum(data[:-1]) == data[-1]


def _decode_mtc(data: tuple) -> dict:
    # Full Message (01 01) and User Bits (01 02).
    device_id, code = data[1], data[3]
    if code == 0x02:
        if len(data) != 13:
            raise ValueError("User Bits has 9 data bytes")
        return {"type": "mtc_user_bits", "device_id": device_id,
                "binary_groups": list(data[4:12]), "flags": data[12]}
    _, _, _, code, hr, mn, sc, fr = data
    if code != 0x01:
        raise ValueError("not an MTC Full Message or User Bits")
    return {"type": "mtc_full", "device_id": device_id,
            **_decode_time_code(hr, mn, sc, fr)}


def _decode_handshake(data: tuple) -> dict:
    # EOF/WAIT/CANCEL/NAK/ACK, shared by File Dump (and Sample Dump). The MTC
    # sync-dropped NAK has the same bytes and decodes as file_dump 'nak'.
    _, device_id, sub_id1, packet_number = data
    return {"type": "file_dump", "command": _name_for(_FILE_DUMP_HANDSHAKE, sub_id1),
            "device_id": device_id, "packet_number": packet_number}


def _decode_sample_dump(data: tuple) -> dict:
    device_id, sub_id1 = data[1], data[2]
    key: tuple
    if sub_id1 == 0x05:
        key, payload = (0x05, data[3]), data[4:]
    else:
        key, payload = (sub_id1,), data[3:]
    command = _name_for(_COMMANDS["sample_dump"], key)
    out: dict = {"type": "sample_dump", "command": command, "device_id": device_id}
    if command == "data_packet":
        if len(payload) != 1 + _SAMPLE_PACKET_BYTES + 1:
            raise ValueError("a data packet has 120 data bytes")
        out.update(packet_number=payload[0], data=list(payload[1:-1]),
                   checksum_ok=_checksum_ok(data))
        return out
    out["sample_number"] = _join14(payload[0], payload[1])
    rest = payload[2:]

    def loop_number(lsb: int, msb: int):
        return "all" if (lsb, msb) == (0x7F, 0x7F) else _join14(lsb, msb)

    if command == "request":
        if rest:
            raise ValueError("Dump Request has no data after the sample number")
    elif command == "loop_points_request":
        lsb, msb = rest
        out["loop_number"] = loop_number(lsb, msb)
    elif command == "loop_points":
        b0, b1, cc, *addresses = rest
        if len(addresses) != 6:
            raise ValueError("Loop Point Transmission is 17 bytes")
        out.update(
            loop_number=loop_number(b0, b1),
            loop_type=_name_for(_SAMPLE_LOOP_TYPES, cc),
            loop_start=_join21(*addresses[:3]), loop_end=_join21(*addresses[3:]),
        )
    else:  # header
        if len(rest) != 14:
            raise ValueError("Dump Header is 21 bytes")
        out.update(
            sample_format=rest[0],
            sample_period=_join21(*rest[1:4]), sample_length=_join21(*rest[4:7]),
            sustain_loop_start=_join21(*rest[7:10]),
            sustain_loop_end=_join21(*rest[10:13]),
            loop_type=_name_for(_SAMPLE_LOOP_TYPES, rest[13]),
        )
    return out


def _decode_file_dump(data: tuple) -> dict:
    device_id, code = data[1], data[3]
    command = _name_for(_COMMANDS["file_dump"], code)
    out: dict = {"type": "file_dump", "command": command, "device_id": device_id}
    if command == "data_packet":
        packet_number, count, *encoded = data[4:-1]
        if count != len(encoded) - 1:
            raise ValueError("Data Packet count doesn't match its data")
        out.update(packet_number=packet_number,
                   stored_bytes=_decode_file_dump_data(encoded),
                   checksum_ok=_checksum_ok(data))
        return out
    source, file_type, rest = data[4], data[5:9], data[9:]
    out.update(source_device_id=source, file_type=bytes(file_type).decode("ascii"))
    if command == "header":
        out["length"] = sum(b << (7 * i) for i, b in enumerate(rest[:4]))
        rest = rest[4:]
    out["filename"] = bytes(rest).decode("ascii")
    return out


def _decode_midi_tuning(data: tuple) -> dict:
    """Inverse of the midi_tuning builder."""
    header, device_id, code = data[0], data[1], data[3]
    command = _name_for(_COMMANDS["midi_tuning"], code)
    out: dict = {"type": "midi_tuning", "command": command, "device_id": device_id}
    real_time = header == 0x7F
    if command in _TUNING_EITHER_TIME:
        out["real_time"] = real_time
    elif real_time != (command in _TUNING_REAL_TIME_ONLY):
        raise ValueError(f"{command} has the wrong real-time/non-real-time header")
    rest = list(data[4:-1] if command in _TUNING_DUMPS else data[4:])

    def take(n: int) -> list:
        if len(rest) < n:
            raise ValueError(f"{command} is truncated")
        chunk = rest[:n]
        del rest[:n]
        return chunk

    def frequency(xx: int, yy: int, zz: int) -> dict:
        if (xx, yy, zz) == (0x7F, 0x7F, 0x7F):
            return {"no_change": True}
        return {"semitone": xx, "cents": _join14(zz, yy) * 100 / 16384}

    if command in ("scale_octave_1byte", "scale_octave_2byte"):
        out["channels"] = _decode_channel_bitmap(*take(3))
    else:
        if command not in ("bulk_dump_request", "bulk_dump_reply", "note_change"):
            out["bank"] = take(1)[0]
        out["tuning_program"] = take(1)[0]
    if command in _TUNING_DUMPS:
        out["tuning_name"] = bytes(take(16)).decode("ascii").rstrip(" ")
    if command in ("bulk_dump_reply", "key_based_dump"):
        freq = take(384)
        out["notes"] = [frequency(*freq[i:i + 3]) for i in range(0, 384, 3)]
    elif command in ("note_change", "note_change_bank"):
        entries = take(4 * take(1)[0])
        out["changes"] = [
            {"key": entries[i], **frequency(*entries[i + 1:i + 4])}
            for i in range(0, len(entries), 4)
        ]
    elif command in ("scale_octave_dump_1byte", "scale_octave_1byte"):
        out["offsets"] = take(12)
    elif command in ("scale_octave_dump_2byte", "scale_octave_2byte"):
        pairs = take(24)
        out["offsets"] = [_join14(pairs[i + 1], pairs[i]) for i in range(0, 24, 2)]
    if rest:
        raise ValueError(f"{command} has extra bytes")
    if command in _TUNING_DUMPS:
        out["checksum_ok"] = _checksum_ok(data)
    return out


def _decode_notation(data: tuple) -> dict:
    device_id, code = data[1], data[3]
    command = _name_for(_COMMANDS["notation"], code)
    out: dict = {"type": "notation", "command": command, "device_id": device_id}
    if command == "bar_marker":
        _, _, _, _, lsb, msb = data
        bar = _join14(lsb, msb)
        out["bar_number"] = bar - 0x4000 if bar >= 0x2000 else bar
        return out
    length, nn, dd, cc, bb, *compound = data[4:]
    if length != 4 + len(compound) or len(compound) % 2:
        raise ValueError("time signature length doesn't match its data")
    out.update(
        numerator=nn, denominator=2 ** dd, clocks_per_click=cc,
        notated_32nd_notes_per_beat=bb,
        compound=[{"numerator": compound[i], "denominator": 2 ** compound[i + 1]}
                  for i in range(0, len(compound), 2)],
    )
    return out


def _decode_cueing_event(command: str, sl: int, sm: int, info) -> dict:
    """Inverse of _cueing_event."""
    out: dict = {"event_number": _join14(sl, sm)}
    if command == "event_name":
        out["event_name"] = bytes(_denibblize(info)).decode("ascii")
    elif command.endswith("_with_info"):
        out["additional_info_bytes"] = _denibblize(info)
    elif info:
        raise ValueError(f"{command} carries no additional info")
    return out


def _decode_mtc_cueing(data: tuple) -> dict:
    device_id, code, sl, sm, info = data[1], data[3], data[4], data[5], data[6:]
    command = _name_for(_COMMANDS["mtc_cueing"], code)
    out: dict = {"type": "mtc_cueing", "command": command, "device_id": device_id}
    if command != "special_system_stop":
        out.update(_decode_cueing_event(command, sl, sm, info))
    return out


def _decode_mtc_cueing_nrt(data: tuple) -> dict:
    device_id, code = data[1], data[3]
    time_bytes, sl, sm, info = data[4:9], data[9], data[10], data[11:]
    out: dict = {"type": "mtc_cueing_nrt", "device_id": device_id}
    if code == 0x00:
        command = _name_for(_MTC_CUEING_NRT_SPECIAL_TYPES, sl)
        out["command"] = command
        if command in ("special_time_code_offset", "special_event_list_request"):
            out.update(_decode_time_code(*time_bytes, subframe_field="fractional_frames"))
        return out
    specials = set(_MTC_CUEING_NRT_SPECIAL_TYPES)
    command = _name_for(
        {k: v for k, v in _COMMANDS["mtc_cueing_nrt"].items() if k not in specials}, code,
    )
    out["command"] = command
    out.update(_decode_time_code(*time_bytes, subframe_field="fractional_frames"))
    out.update(_decode_cueing_event(command, sl, sm, info))
    return out


def _decode_msc_cue(data) -> dict:
    """Inverse of _encode_msc_cue_data: up to three ASCII fields separated
    by 00."""
    if not data:
        return {}
    parts = bytes(data).split(b"\x00")
    if len(parts) > 3 or not all(parts):
        raise ValueError("bad MSC cue data")
    return {
        name: part.decode("ascii")
        for name, part in zip(("q_number", "q_list", "q_path"), parts)
    }


def _decode_msc_timed_go(data) -> dict:
    return {
        **_decode_time_code(*data[:5], subframe_field="fractional_frames"),
        **_decode_msc_cue(data[5:]),
    }


def _decode_msc_set(data) -> dict:
    out = {
        "control_number": _join14(data[0], data[1]),
        "control_value": _join14(data[2], data[3]),
    }
    if len(data) > 4:
        out.update(_decode_time_code(*data[4:9], subframe_field="fractional_frames"))
    return out


def _decode_msc_single(field: str):
    def decode(data) -> dict:
        return {field: bytes(data).decode("ascii")} if data else {}
    return decode


def _decode_msc_2pc_prefix(data) -> dict:
    return {"checksum": _join14(data[0], data[1]),
            "sequence_number": _join14(data[2], data[3])}


def _decode_msc_2pc(command: str):
    def decode(data) -> dict:
        out = _decode_msc_2pc_prefix(data)
        rest = data[4:]
        if command in ("standby", "go_2pc"):
            out["cue_data"] = list(rest[:4])
            out.update(_decode_msc_cue(rest[4:]))
            if "q_number" not in out:
                raise ValueError(f"{command} requires a Q_number")
        elif command == "standing_by":
            out.update(_decode_time_code(*rest[:5], subframe_field="fractional_frames"))
            out.update(_decode_msc_cue(rest[5:]))
        else:
            out.update(_decode_msc_cue(rest))
        return out
    return decode


def _decode_msc_2pc_status(command: str):
    def decode(data) -> dict:
        checksum_lsb, checksum_msb, s1, s2, seq_lsb, seq_msb = data
        code = s1 * 4 + s2 * 512
        names = _MSC_CANCELLED_STATUS if command == "cancelled" else _MSC_ABORT_STATUS
        status = _name_for(names, code) if code in names.values() else code
        return {"checksum": _join14(checksum_lsb, checksum_msb), "status": status,
                "sequence_number": _join14(seq_lsb, seq_msb)}
    return decode


# Inverses of _MSC_DATA_BUILDERS: <data> -> fields.
_MSC_DATA_DECODERS = {
    "go": _decode_msc_cue,
    "stop": _decode_msc_cue,
    "resume": _decode_msc_cue,
    "go_off": _decode_msc_cue,
    "load": _decode_msc_cue,
    "timed_go": _decode_msc_timed_go,
    "set": _decode_msc_set,
    "fire": lambda data: {"macro_number": data[0]},
    **{name: _decode_msc_single("q_list") for name in (
        "standby_plus", "standby_minus", "sequence_plus", "sequence_minus",
        "start_clock", "stop_clock", "zero_clock", "mtc_chase_on",
        "mtc_chase_off", "open_cue_list", "close_cue_list",
    )},
    "set_clock": lambda data: {
        **_decode_time_code(*data[:5], subframe_field="fractional_frames"),
        **_decode_msc_single("q_list")(data[5:]),
    },
    "open_cue_path": _decode_msc_single("q_path"),
    "close_cue_path": _decode_msc_single("q_path"),
    **{name: _decode_msc_2pc(name) for name in
       ("standby", "standing_by", "go_2pc", "complete", "cancel")},
    "cancelled": _decode_msc_2pc_status("cancelled"),
    "abort": _decode_msc_2pc_status("abort"),
}


def _decode_msc(data: tuple) -> dict:
    device_id, command_format, code, payload = data[1], data[3], data[4], data[5:]
    command = _name_for(_COMMANDS["msc"], code)
    out: dict = {"type": "msc", "command": command, "device_id": device_id}
    try:
        out["command_format"] = _name_for(_MSC_FORMATS, command_format)
    except KeyError:
        out["command_format_raw"] = command_format
    decoder = _MSC_DATA_DECODERS.get(command)
    if decoder is not None:
        out.update(decoder(payload))
    elif payload:
        raise ValueError(f"MSC {command} carries no data")
    if command in _MSC_2PC_COMMANDS:
        # Which 6.5 byte pairings reproduce the checksum received.
        out["checksum_matches"] = [
            order for order in _MSC_CHECKSUM_ORDERS
            if _msc_2pc_checksum(device_id, command_format, code, payload, order)
            == tuple(payload[:2])
        ]
    return out


def _decode_speed(data) -> dict:
    """Inverse of _encode_standard_speed."""
    sh, sm, sl = data
    shift = (sh >> 3) & 0x7
    raw = ((sh & 0x7) << 14) | (sm << 7) | sl
    return {"speed": raw / 2 ** (14 - shift), "reverse": bool(sh & 0x40)}


def _decode_mmc_locate(data) -> dict:
    # [I/F] 00 <name>, or [TARGET] 01 + Standard Time Code.
    if data[0] == 0x00:
        if len(data) != 2:
            raise ValueError("LOCATE [I/F] is 00 <name>")
        return {"name": _info_field_name(data[1])}
    if data[0] != 0x01:
        raise ValueError("LOCATE sub-command must be 00 or 01")
    return _decode_time_code(*data[1:6], subframe_field="subframes")


def _info_field_name(code: int) -> str:
    return _name_for(_INFO_FIELD_NAMES, code)


def _decode_mmc_write_fields(data) -> list:
    """Inverse of _mmc_write: fields split by the name-byte ranges."""
    fields, at = [], 0
    while at < len(data):
        name = _info_field_name(data[at])
        if data[at] < 0x20:
            fields.append({"name": name, **_decode_standard_time_code(*data[at + 1:at + 6])})
            at += 6
            continue
        codec = _MMC_FIELD_CODECS.get(name)
        if data[at] < 0x40 or codec is None or codec[0] is None:
            raise ValueError(f"{name} can't be written")
        count = data[at + 1]
        payload = data[at + 2:at + 2 + count]
        if len(payload) != count:
            raise ValueError(f"WRITE field {name} is truncated")
        fields.append({"name": name, **codec[1](payload)})
        at += 2 + count
    return fields


def _decode_mmc_procedure(data) -> dict:
    action = _name_for(_MMC_PROCEDURE_ACTIONS, data[0])
    out: dict = {"action": action, "procedure": data[1]}
    if action == "assemble":
        out["commands"] = _mmc_parse_commands(data[2:])
    return out


def _decode_mmc_event(data) -> dict:
    action = _name_for(_MMC_EVENT_ACTIONS, data[0])
    out: dict = {"action": action, "event": data[1]}
    if action == "define":
        flags, source, name = data[2], data[3], data[4]
        nested = _mmc_parse_commands(data[5:])
        if len(nested) != 1:
            raise ValueError("EVENT [DEFINE] takes exactly one command")
        out.update(
            direction=_name_for(_MMC_EVENT_DIRECTIONS, flags & 0x03),
            all_speeds=bool(flags & 0x10), non_delete=bool(flags & 0x40),
            trigger_source=_info_field_name(source), name=_info_field_name(name),
            trigger_command=nested[0],
        )
    return out


# Inverses of _MMC_DATA_BUILDERS: <data> -> fields.
_MMC_DATA_DECODERS = {
    "locate": _decode_mmc_locate,
    "step": lambda d: {"quantity": d[0] & 0x3F, "reverse": bool(d[0] & 0x40)},
    "assign_system_master": lambda d: {"target_device_id": d[0]},
    "generator_command": lambda d: {"action": _name_for(_MMC_GENERATOR_ACTIONS, d[0])},
    "midi_time_code_command": lambda d: {
        "action": _name_for(_MMC_MTC_COMMAND_ACTIONS, d[0]),
    },
    "variable_play": _decode_speed,
    "search": _decode_speed,
    "shuttle": _decode_speed,
    "deferred_variable_play": _decode_speed,
    "record_strobe_variable": _decode_speed,
    "drop_frame_adjust": lambda d: {"name": _info_field_name(d[0])},
    "move": lambda d: {
        "destination": _info_field_name(d[0]), "source": _info_field_name(d[1]),
    },
    "add": lambda d: {
        "destination": _info_field_name(d[0]),
        "source_1": _info_field_name(d[1]), "source_2": _info_field_name(d[2]),
    },
    "subtract": lambda d: {
        "destination": _info_field_name(d[0]),
        "source_1": _info_field_name(d[1]), "source_2": _info_field_name(d[2]),
    },
    "group": lambda d: {
        "action": _name_for(_MMC_GROUP_ACTIONS, d[0]), "group": d[1],
        "device_ids": list(d[2:]),
    },
    "procedure": _decode_mmc_procedure,
    "event": _decode_mmc_event,
    "read": lambda d: {"names": [_info_field_name(b) for b in d]},
    "write": lambda d: {"fields": _decode_mmc_write_fields(d)},
    "masked_write": lambda d: {"fields": [
        {"name": _info_field_name(d[i]), "byte_number": d[i + 1],
         "mask": d[i + 2], "data": d[i + 3]}
        for i in range(0, len(d), 4)
    ]},
    "update": lambda d: {
        "action": _name_for(_MMC_UPDATE_ACTIONS, d[0]),
        "names": ["all" if b == 0x7F else _info_field_name(b) for b in d[1:]],
    },
    "command_segment": lambda d: {
        "first": bool(d[0] & 0x40), "remaining": d[0] & 0x3F, "data": list(d[1:]),
    },
}


def _mmc_parse_commands(body) -> list:
    """Inverse of _mmc_command_bytes over a run of commands: a list of mmc
    command dicts."""
    commands, at = [], 0
    while at < len(body):
        command = _name_for(_COMMANDS["mmc"], body[at])
        out: dict = {"type": "mmc", "command": command}
        decoder = _MMC_DATA_DECODERS.get(command)
        if decoder is None:
            at += 1
        else:
            count = body[at + 1]
            data = body[at + 2:at + 2 + count]
            if len(data) != count:
                raise ValueError(f"MMC {command} is truncated")
            out.update(decoder(data))
            at += 2 + count
        commands.append(out)
    return commands


def _decode_mmc(data: tuple) -> dict:
    # One command decodes to the 'command' form; several to 'batch'.
    commands = _mmc_parse_commands(data[3:])
    if not commands:
        raise ValueError("no MMC command")
    if len(commands) > 1:
        return {"type": "mmc", "device_id": data[1], "batch": commands}
    command = commands[0]
    return {"type": "mmc", "command": command["command"], "device_id": data[1],
            **{k: v for k, v in command.items() if k not in ("type", "command")}}


def _decode_mmc_response_sysex(data: tuple) -> dict:
    # Device-to-controller replies: decoded for reading, not for sending.
    # 'mmc_response' isn't a message type _build_message accepts.
    fields = _mmc_response_fields([0xF0, *data, 0xF7])
    return {"type": "mmc_response", "device_id": fields.pop("device_id"),
            "response": fields}


# SysEx decoded for reading only, with no rebuild check.
_SYSEX_READ_ONLY_DECODERS = {
    (0x7F, 0x07): _decode_mmc_response_sysex,
}


# (universal ID, sub-ID#1) -> decoder for the SysEx data (F0/F7 excluded).
_SYSEX_DECODERS = {
    (0x7E, 0x09): _decode_gm_system,
    (0x7E, 0x06): _decode_device_inquiry,
    (0x7F, 0x04): _decode_device_control,
    (0x7F, 0x01): _decode_mtc,
    **{(0x7E, code): _decode_handshake for code in _FILE_DUMP_HANDSHAKE.values()},
    (0x7E, 0x07): _decode_file_dump,
    **{(0x7E, sub_id1): _decode_sample_dump for sub_id1 in (0x01, 0x02, 0x03, 0x05)},
    (0x7E, 0x08): _decode_midi_tuning,
    (0x7F, 0x08): _decode_midi_tuning,
    (0x7F, 0x03): _decode_notation,
    (0x7F, 0x05): _decode_mtc_cueing,
    (0x7E, 0x04): _decode_mtc_cueing_nrt,
    (0x7F, 0x02): _decode_msc,
    (0x7F, 0x09): _decode_controller_destination,
    (0x7F, 0x0A): _decode_key_based,
    (0x7F, 0x06): _decode_mmc,
}


def _decode_sysex(data: tuple) -> dict:
    """A SysEx payload (F0/F7 excluded) as its typed dict, or as plain
    'sysex' when no decoder applies or the decode doesn't rebuild to the
    same bytes. A decode carrying 'checksum_ok' is kept when everything but
    the checksum byte rebuilds; 'checksum_ok' says whether the received
    checksum was right."""
    if len(data) >= 3:
        read_only = _SYSEX_READ_ONLY_DECODERS.get((data[0], data[2]))
        if read_only is not None:
            try:
                return read_only(data)
            except (IndexError, KeyError, ValueError, TypeError):
                return {"type": "sysex", "data": list(data)}
        decoder = _SYSEX_DECODERS.get((data[0], data[2]))
        if decoder is not None:
            try:
                decoded = decoder(data)
                rebuilt = tuple(_build_message(dict(decoded)).data)
                if rebuilt == data or (
                    "checksum_ok" in decoded and rebuilt[:-1] == data[:-1]
                ):
                    return decoded
            except (IndexError, KeyError, ValueError, TypeError, UnicodeDecodeError):
                pass
    return {"type": "sysex", "data": list(data)}


def _decode_message(msg: "mido.Message") -> dict:
    """A received mido.Message as the dict _build_message takes."""
    if msg.type == "sysex":
        return _decode_sysex(tuple(msg.data))
    if msg.type == "control_change":
        mode = _decode_channel_mode(msg)
        if mode is not None:
            return mode
    out: dict = {"type": msg.type}
    if msg.type in _CHANNEL_TYPES:
        out["channel"] = msg.channel
    for name in _MIDO_FIELDS.get(msg.type, {}):
        out[name] = getattr(msg, name)
    return out

class _StreamDecoder:
    """Per-input state for changes that span several wire messages: RPN/NRPN
    parameter changes and MTC Quarter Frame sequences. feed() takes each
    received message in order and returns the combined message it completes,
    or None. A combined message is returned only when building it gives back
    exactly the messages received.

    RPN/NRPN (MIDI 1.0 Detailed Spec, per channel): CC 101/100 select an RPN
    and CC 99/98 an NRPN, in either order; the selection stays until another
    one. CC 6 (Data Entry MSB) completes a 7-bit change (msb_only), and a CC 38
    (Data Entry LSB) right after it completes the 14-bit change, so a 14-bit
    sender yields two results. RPN Null (127/127) makes data entry ignored.

    Quarter Frames: eight consecutive pieces, types 0 to 7 (forward) or 7 to 0
    (reverse), complete one time.

    MMC COMMAND SEGMENT / RESPONSE SEGMENT (RP-013 pp.8, 39, 73), per
    device: a first segment starts, the down-count must step by one, and the
    last (00) completes the command or response. An out-of-order segment, or
    a normal MMC message from that device other than WAIT/RESUME, cancels it.
    A reassembled command carries 'segment': true, plus 'segment_size' when
    that size rebuilds exactly the segments received.
    """

    def __init__(self) -> None:
        # channel -> {"kind": "rpn"|"nrpn", "msb", "lsb", "value_msb"}
        self._params: dict = {}
        self._quarter_frames: list = []
        # (06 commands | 07 responses, device id) -> {"remaining", "data"}
        self._segments: dict = {}

    def feed(self, msg: "mido.Message") -> "dict | None":
        if msg.type == "control_change":
            return self._control_change(msg)
        if msg.type == "quarter_frame":
            return self._quarter_frame(msg)
        if msg.type == "sysex":
            return self._mmc_segment(tuple(msg.data))
        return None

    def _mmc_segment(self, data: tuple) -> "dict | None":
        if len(data) < 4 or data[0] != 0x7F or data[2] not in (0x06, 0x07):
            return None
        key, body = (data[2], data[1]), data[3:]
        segment_code = 0x53 if data[2] == 0x06 else 0x64
        if body[0] != segment_code:
            if body not in ((0x7C,), (0x7F,)):
                self._segments.pop(key, None)
            return None
        if len(body) < 3 or body[1] != len(body) - 2:
            self._segments.pop(key, None)
            return None
        first, remaining, piece = bool(body[2] & 0x40), body[2] & 0x3F, list(body[3:])
        state = self._segments.get(key)
        if first:
            state = {"remaining": remaining, "pieces": [piece]}
        elif state is None or state["remaining"] - 1 != remaining:
            self._segments.pop(key, None)
            return None
        else:
            state = {"remaining": remaining, "pieces": state["pieces"] + [piece]}
        if remaining:
            self._segments[key] = state
            return None
        self._segments.pop(key, None)
        pieces = state["pieces"]
        whole = [b for p in pieces for b in p]
        try:
            if data[2] == 0x07:
                fields = _mmc_response_fields([0xF0, 0x7F, data[1], 0x07, *whole, 0xF7])
                return {"type": "mmc_response", "device_id": fields.pop("device_id"),
                        "response": fields, "segments": len(pieces)}
            commands = _mmc_parse_commands(whole)
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        if len(commands) == 1:
            command = commands[0]
            out = {"type": "mmc", "command": command["command"], "device_id": data[1],
                   **{k: v for k, v in command.items() if k not in ("type", "command")}}
        else:
            out = {"type": "mmc", "device_id": data[1], "batch": commands}
        out["segment"] = True
        # 'segment_size' only when it rebuilds the sender's own split.
        if 1 <= len(pieces[0]) <= 45:
            out["segment_size"] = len(pieces[0])
            try:
                rebuilt = [list(m.data[3:]) for m in _build_mmc_segments(out)]
            except (ValueError, KeyError, IndexError, TypeError):
                rebuilt = None
            sent = [[0x53, len(p) + 1, (0x40 if i == 0 else 0) | (len(pieces) - 1 - i), *p]
                    for i, p in enumerate(pieces)]
            if rebuilt != sent:
                del out["segment_size"]
        return out

    def _control_change(self, msg: "mido.Message") -> "dict | None":
        selects = {101: ("rpn", "msb"), 100: ("rpn", "lsb"),
                   99: ("nrpn", "msb"), 98: ("nrpn", "lsb")}
        state = self._params.setdefault(msg.channel, {})
        if msg.control in selects:
            kind, part = selects[msg.control]
            if state.get("kind") != kind:
                state.clear()
                state["kind"] = kind
            state[part] = msg.value
            state["value_msb"] = None
            if kind == "rpn" and state.get("msb") == 0x7F and state.get("lsb") == 0x7F:
                return {"type": "rpn", "channel": msg.channel, "parameter": "null"}
            return None
        if msg.control not in (6, 38) or state.get("msb") is None or state.get("lsb") is None:
            return None
        number = _join14(state["lsb"], state["msb"])
        if state["kind"] == "rpn" and number == 0x3FFF:
            return None  # RPN Null: data entry is ignored
        if msg.control == 6:
            state["value_msb"] = msg.value
            value, msb_only = msg.value, True
        elif state["value_msb"] is None:
            return None  # LSB with no MSB before it
        else:
            value, msb_only = _join14(msg.value, state["value_msb"]), False
        combined: dict = {"type": state["kind"], "channel": msg.channel}
        if state["kind"] == "rpn" and number in _RPN_NAMED_PARAMETERS.values():
            combined["parameter"] = _name_for(_RPN_NAMED_PARAMETERS, number)
        else:
            combined["parameter_number"] = number
        combined.update(value=value, msb_only=msb_only)
        # The rebuilt sequence must end with the data-entry message just read.
        try:
            rebuilt = _build_rpn_or_nrpn_sequence(
                dict(combined), registered=state["kind"] == "rpn",
            )
        except (KeyError, ValueError, TypeError):
            return None
        return combined if rebuilt[-1].bytes() == msg.bytes() else None

    def _quarter_frame(self, msg: "mido.Message") -> "dict | None":
        # A run starting at type 0 goes forward 0..7; one starting at 7 goes
        # in reverse 7..0. Anything out of order starts over.
        pieces = self._quarter_frames
        if pieces:
            step = 1 if pieces[0].frame_type == 0 else -1
            if msg.frame_type != pieces[-1].frame_type + step:
                pieces.clear()
        if not pieces and msg.frame_type not in (0, 7):
            return None
        pieces.append(msg)
        if len(pieces) < 8:
            return None
        values = {p.frame_type: p.frame_value for p in pieces}
        received = [(p.frame_type, p.frame_value) for p in pieces]
        reverse = pieces[0].frame_type == 7
        pieces.clear()
        combined = {
            "type": "mtc_quarter_frame_sequence",
            "direction": "reverse" if reverse else "forward",
            **_decode_time_code(
                values[6] | (values[7] << 4), values[4] | (values[5] << 4),
                values[2] | (values[3] << 4), values[0] | (values[1] << 4),
            ),
        }
        try:
            rebuilt = _build_quarter_frame_sequence(dict(combined))
        except (KeyError, ValueError, TypeError):
            return None
        if [(m.frame_type, m.frame_value) for m in rebuilt] != received:
            return None
        return combined


def _send(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    message = tool_input.get("message") or {}
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, port = entry
    if direction != "output":
        return _err(f"handle {handle!r} is an input port, cannot send on it")

    try:
        msgs = _build_message_sequence(message)
        for msg in msgs:
            port.send(msg)
    except KeyError as e:
        return _err(f"message missing required field: {e}")
    except Exception as e:
        return _err(f"send failed: {type(e).__name__}: {e}")

    # 'sent' is a string for one message, a list for several.
    if len(msgs) == 1:
        return json.dumps({"status": "ok", "sent": str(msgs[0])})
    return json.dumps({"status": "ok", "sent": [str(m) for m in msgs]})


def _poll(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, _port = entry
    if direction != "input":
        return _err(f"handle {handle!r} is an output port, cannot poll it")

    buf = _INPUT_BUFFERS.get(handle)
    event = _INPUT_EVENTS.get(handle)
    if buf is None or event is None:
        # _open always creates both for an input handle.
        return _err(
            f"no message buffer for handle {handle!r} (internal "
            "inconsistency — the port may not have been opened correctly)"
        )

    timeout_seconds = tool_input.get("timeout_seconds", 0)
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        return _err("'timeout_seconds' must be a number (0-60, default 0)")
    if not (0 <= timeout_seconds <= _MAX_POLL_TIMEOUT):
        return _err(
            f"'timeout_seconds' must be 0-{_MAX_POLL_TIMEOUT}, got "
            f"{timeout_seconds!r}"
        )

    # Wait until something is buffered or the timeout passes. The event is
    # cleared just before each wait and the buffer re-checked after, so a
    # set left over from an earlier drain can't end the wait early, and a
    # message that lands between the check and wait() still wakes it (the
    # callback appends before it sets).
    deadline = time.monotonic() + timeout_seconds
    while not buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        event.clear()
        if buf:
            break
        event.wait(timeout=remaining)

    # 'received_at' was stamped by the callback on arrival. 'decoded' is the
    # dict 'send' takes; 'completes' is an RPN/NRPN change or Quarter Frame
    # time that this message finishes. An overflow (msg None) lost messages,
    # so partial RPN/NRPN, Quarter Frame and segment state starts over.
    stream = _STREAM_DECODERS.setdefault(handle, _StreamDecoder())
    messages = []
    while buf:
        received_at, msg, at_open = buf.popleft()
        if msg is None:
            entry = {"received_at": received_at, "overflow": True}
            stream = _STREAM_DECODERS[handle] = _StreamDecoder()
        else:
            entry = {
                "received_at": received_at, "message": str(msg),
                "hex": msg.hex(), "decoded": _decode_message(msg),
            }
            completed = stream.feed(msg)
            if completed is not None:
                entry["completes"] = completed
        if at_open:
            entry["at_open"] = True
        messages.append(entry)

    return json.dumps({"status": "ok", "messages": messages})


# MIDI Clock rate, fixed by the MIDI 1.0 spec.
_CLOCK_PULSES_PER_QUARTER_NOTE = 24


def _run_clock(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, port = entry
    if direction != "output":
        return _err(f"handle {handle!r} is an input port, cannot run_clock on it")

    bpm = tool_input.get("bpm")
    if not isinstance(bpm, (int, float)) or isinstance(bpm, bool):
        return _err("'bpm' is required and must be a number for 'run_clock'")
    if not (20 <= bpm <= 300):
        return _err(f"'bpm' must be 20-300, got {bpm!r}")

    duration_seconds = tool_input.get("duration_seconds")
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        return _err(
            "'duration_seconds' is required and must be a number for 'run_clock'"
        )
    if not (0 < duration_seconds <= _MAX_CLOCK_DURATION):
        return _err(
            f"'duration_seconds' must be > 0 and <= {_MAX_CLOCK_DURATION}, "
            f"got {duration_seconds!r}"
        )

    transport = tool_input.get("transport", "start")
    if transport not in ("start", "continue", "none"):
        return _err("'transport' must be 'start', 'continue', or 'none'")

    stop_at_end = tool_input.get("stop_at_end", True)
    if not isinstance(stop_at_end, bool):
        return _err("'stop_at_end' must be a boolean")

    interval = 60.0 / bpm / _CLOCK_PULSES_PER_QUARTER_NOTE
    n_ticks = int(duration_seconds / interval)

    try:
        transport_sent = None
        if transport != "none":
            port.send(mido.Message(transport))
            transport_sent = transport

        # Schedule against absolute tick times so the time spent in
        # port.send() doesn't add up as drift.
        start_time = time.time()
        next_tick = start_time
        for _ in range(n_ticks):
            now = time.time()
            sleep_time = next_tick - now
            if sleep_time > 0:
                time.sleep(sleep_time)
            port.send(mido.Message("clock"))
            next_tick += interval
        elapsed = time.time() - start_time

        stop_sent = False
        if stop_at_end:
            port.send(mido.Message("stop"))
            stop_sent = True
    except Exception as e:
        return _err(f"run_clock failed: {type(e).__name__}: {e}")

    return json.dumps(
        {
            "status": "ok",
            "handle": handle,
            "bpm": bpm,
            "ticks_sent": n_ticks,
            "elapsed_seconds": round(elapsed, 3),
            "transport_sent": transport_sent,
            "stop_sent": stop_sent,
        }
    )


def _read_midi_file(tool_input: dict) -> str:
    path = tool_input.get("path")
    if not path:
        return _err("'path' is required for 'read_midi_file'")

    max_messages = tool_input.get("max_messages", 100)
    if not isinstance(max_messages, int) or max_messages < 0:
        return _err("'max_messages' must be a non-negative integer")

    is_syx = path.lower().endswith(".syx")

    try:
        if is_syx:
            msgs = mido.read_syx_file(path)
        else:
            mf = mido.MidiFile(path)
    except FileNotFoundError:
        return _err(f"file not found: {path!r}")
    except Exception as e:
        return _err(f"failed to read {path!r}: {type(e).__name__}: {e}")

    if is_syx:
        total = len(msgs)
        shown = msgs[:max_messages] if max_messages else []
        return json.dumps(
            {
                "status": "ok",
                "file_type": "syx",
                "message_count": total,
                "messages": [str(m) for m in shown],
                "truncated": max_messages > 0 and total > max_messages,
            }
        )

    tracks_out = []
    for i, track in enumerate(mf.tracks):
        name = None
        tempo_bpm = None
        time_sig = None
        key_sig = None
        instrument_name = None
        for msg in track:
            if not msg.is_meta:
                continue
            if msg.type == "track_name" and name is None:
                name = msg.name
            elif msg.type == "set_tempo" and tempo_bpm is None:
                tempo_bpm = mido.tempo2bpm(msg.tempo)
            elif msg.type == "time_signature" and time_sig is None:
                time_sig = f"{msg.numerator}/{msg.denominator}"
            elif msg.type == "key_signature" and key_sig is None:
                key_sig = msg.key
            elif msg.type == "instrument_name" and instrument_name is None:
                instrument_name = msg.name

        total = len(track)
        shown = list(track)[:max_messages] if max_messages else []
        tracks_out.append(
            {
                "index": i,
                "name": name,
                "message_count": total,
                "tempo_bpm": tempo_bpm,
                "time_signature": time_sig,
                "key_signature": key_sig,
                "instrument_name": instrument_name,
                "messages": [str(m) for m in shown],
                "truncated": max_messages > 0 and total > max_messages,
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "file_type": "mid",
            "type": mf.type,
            "ticks_per_beat": mf.ticks_per_beat,
            "length_seconds": mf.length,
            "track_count": len(mf.tracks),
            "tracks": tracks_out,
        }
    )


def _write_midi_file(tool_input: dict) -> str:
    """Write a .mid/.midi file from 'tracks' or a .syx file from sysex
    'messages'. Refuses to replace an existing file unless 'overwrite'.
    """
    path = tool_input.get("path")
    if not path:
        return _err("'path' is required for 'write_midi_file'")

    overwrite = tool_input.get("overwrite", False)
    if os.path.exists(path) and not overwrite:
        return _err(
            f"file already exists: {path!r} — pass overwrite: true to "
            "replace it (refusing by default to avoid accidentally "
            "destroying an existing file)"
        )

    is_syx = path.lower().endswith(".syx")

    if is_syx:
        messages_in = tool_input.get("messages")
        if not messages_in:
            return _err("'messages' is required for writing a .syx file")
        built = []
        for i, m in enumerate(messages_in):
            if not isinstance(m, dict) or m.get("type") != "sysex":
                return _err(
                    f"'.syx' files only support sysex messages, got "
                    f"{m.get('type') if isinstance(m, dict) else m!r} at "
                    f"index {i}"
                )
            try:
                built.append(_build_message(m))
            except KeyError as e:
                return _err(f"message {i} missing required field: {e}")
            except Exception as e:
                return _err(f"message {i} invalid: {type(e).__name__}: {e}")
        try:
            mido.write_syx_file(path, built)
        except Exception as e:
            return _err(f"failed to write {path!r}: {type(e).__name__}: {e}")
    else:
        midi_file_type = tool_input.get("midi_file_type", 1)
        ticks_per_beat = tool_input.get("ticks_per_beat", 480)
        tracks_in = tool_input.get("tracks")
        if not tracks_in:
            return _err("'tracks' is required for writing a .mid file")
        try:
            mf = mido.MidiFile(type=midi_file_type, ticks_per_beat=ticks_per_beat)
        except Exception as e:
            return _err(f"invalid MIDI file parameters: {type(e).__name__}: {e}")
        for ti, track_in in enumerate(tracks_in):
            track = mido.MidiTrack()
            for mi, m in enumerate(track_in.get("messages", [])):
                msg_type = m.get("type") if isinstance(m, dict) else None
                try:
                    if msg_type in _META_TYPES:
                        track.append(_build_meta_message(m))
                    else:
                        # Multi-message types put the caller's delta on
                        # the first message and 0 on the rest.
                        track.extend(_build_message_sequence(m))
                except KeyError as e:
                    return _err(
                        f"track {ti} message {mi} missing required field: {e}"
                    )
                except Exception as e:
                    return _err(
                        f"track {ti} message {mi} invalid: "
                        f"{type(e).__name__}: {e}"
                    )
            mf.tracks.append(track)
        try:
            mf.save(path)
        except Exception as e:
            return _err(f"failed to write {path!r}: {type(e).__name__}: {e}")

    # Re-read the new file; the summary doubles as a check.
    return _read_midi_file({"path": path, "max_messages": 0})
