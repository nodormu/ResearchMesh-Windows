"""Byte-snapshot and behaviour tests for core/midi1.py.

    python test_midi1.py            check against test_midi1_snapshot.json
    python test_midi1.py --record   rewrite the snapshot from the current code

The snapshot pins the exact wire bytes (and mido 'time' values) that
_build_message_sequence produces for every message type and command, the
exception type for invalid input, the output of decode_mmc_response, and the
bytes of each meta message. It describes what the code does, not what the
spec says: re-record only when a change to the output is intended, and review
the snapshot diff when you do.

Also checked, without the snapshot:
  - worked examples printed in the official specs, byte for byte
  - the TOOLS 'command' enum equals the commands the code accepts
  - every TOOLS message 'type' has a builder
  - poll's wait logic, with a fake input handle (no MIDI port)
  - a live send/poll loopback over a loopback port when one exists: 'Midi
    Through' (Linux), the IAC Driver (macOS, enabled in Audio MIDI Setup) or
    loopMIDI (Windows)
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "test_midi1_snapshot.json"
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")
        FAILURES.append(name)


# --- Cases -----------------------------------------------------------------
# (name, message dict). A case whose name starts with "err:" must raise; its
# exception type is snapshotted. Every other case must build.

TC = {"hours": 1, "minutes": 37, "seconds": 52, "frames": 16}  # RP-004/008 example

TUNING_NOTES = [
    {"no_change": True} if k % 3 == 0 else {"semitone": k, "cents": (k * 7) % 100}
    for k in range(128)
]

CASES: list[tuple[str, dict]] = [
    # Channel Voice
    ("note_on", {"type": "note_on", "channel": 3, "note": 60, "velocity": 100}),
    ("note_on default velocity", {"type": "note_on", "note": 61}),
    ("note_on with time", {"type": "note_on", "note": 62, "velocity": 1, "time": 480}),
    ("note_off", {"type": "note_off", "channel": 15, "note": 60, "velocity": 40}),
    ("note_off default velocity", {"type": "note_off", "note": 60}),
    ("control_change", {"type": "control_change", "channel": 1, "control": 7, "value": 100}),
    ("program_change", {"type": "program_change", "channel": 9, "program": 127}),
    ("pitchwheel min", {"type": "pitchwheel", "pitch": -8192}),
    ("pitchwheel max", {"type": "pitchwheel", "pitch": 8191}),
    ("pitchwheel default", {"type": "pitchwheel"}),
    ("aftertouch", {"type": "aftertouch", "channel": 2, "value": 90}),
    ("polytouch", {"type": "polytouch", "channel": 2, "note": 64, "value": 33}),
    # Channel Mode
    *[(f"channel_mode {c}", {"type": "channel_mode", "command": c, "channel": 4})
      for c in ("all_sound_off", "reset_all_controllers", "all_notes_off",
                "omni_off", "omni_on", "poly_on")],
    ("channel_mode local_control on", {"type": "channel_mode", "command": "local_control", "on": True}),
    ("channel_mode local_control off", {"type": "channel_mode", "command": "local_control", "on": False}),
    ("channel_mode mono_on 0", {"type": "channel_mode", "command": "mono_on", "channel_count": 0}),
    ("channel_mode mono_on 4", {"type": "channel_mode", "command": "mono_on", "channel_count": 4}),
    # System Common / Real-Time
    ("quarter_frame", {"type": "quarter_frame", "frame_type": 7, "frame_value": 6}),
    ("songpos", {"type": "songpos", "pos": 16383}),
    ("songpos default", {"type": "songpos"}),
    ("song_select", {"type": "song_select", "song": 5}),
    ("tune_request", {"type": "tune_request"}),
    *[(t, {"type": t}) for t in ("clock", "start", "stop", "continue", "active_sensing", "reset")],
    # SysEx
    ("sysex", {"type": "sysex", "data": [0x41, 0x10, 0x42, 0x12, 0x7F]}),
    ("sysex empty", {"type": "sysex", "data": []}),
    # MTC
    *[(f"mtc_full {fr}", {"type": "mtc_full", **TC, "frame_rate": fr})
      for fr in ("24", "25", "30drop", "30nondrop")],
    ("mtc_full device_id", {"type": "mtc_full", **TC, "frame_rate": "25", "device_id": 5}),
    ("mtc_nak", {"type": "mtc_nak"}),
    ("mtc_nak device packet", {"type": "mtc_nak", "device_id": 3, "packet_number": 9}),
    ("mtc_quarter_frame_sequence forward", {"type": "mtc_quarter_frame_sequence", **TC, "frame_rate": "30nondrop"}),
    ("mtc_quarter_frame_sequence reverse", {"type": "mtc_quarter_frame_sequence", **TC, "frame_rate": "30nondrop", "direction": "reverse", "time": 12}),
    # RPN / NRPN
    *[(f"rpn {p}", {"type": "rpn", "parameter": p, "value": 300, "channel": 1})
      for p in ("pitch_bend_sensitivity", "fine_tuning", "coarse_tuning",
                "tuning_program_select", "tuning_bank_select")],
    ("rpn mpe_configuration", {"type": "rpn", "parameter": "mpe_configuration", "value": 7, "msb_only": True}),
    ("rpn parameter_number", {"type": "rpn", "parameter_number": 5, "value": 64, "msb_only": True, "time": 3}),
    ("nrpn", {"type": "nrpn", "parameter_number": 0x1234, "value": 16383, "channel": 15}),
    ("nrpn msb_only", {"type": "nrpn", "parameter_number": 1, "value": 127, "msb_only": True}),
    # MMC: no-data transport commands
    *[(f"mmc {c}", {"type": "mmc", "command": c})
      for c in ("stop", "play", "deferred_play", "fast_forward", "rewind",
                "record_strobe", "record_exit", "record_pause", "pause", "eject",
                "chase", "command_error_reset", "mmc_reset")],
    ("mmc stop device_id", {"type": "mmc", "command": "stop", "device_id": 2}),
    # MMC: commands with data
    ("mmc locate", {"type": "mmc", "command": "locate", **TC, "subframes": 50, "frame_rate": "30drop"}),
    ("mmc step forward", {"type": "mmc", "command": "step", "quantity": 5}),
    ("mmc step reverse", {"type": "mmc", "command": "step", "quantity": 63, "reverse": True}),
    ("mmc assign_system_master", {"type": "mmc", "command": "assign_system_master", "target_device_id": 4}),
    *[(f"mmc generator_command {a}", {"type": "mmc", "command": "generator_command", "action": a})
      for a in ("stop", "run", "copy_jam")],
    *[(f"mmc midi_time_code_command {a}", {"type": "mmc", "command": "midi_time_code_command", "action": a})
      for a in ("off", "follow")],
    *[(f"mmc {c} speed {s}{' rev' if r else ''}", {"type": "mmc", "command": c, "speed": s, "reverse": r})
      for c in ("variable_play", "search", "shuttle", "deferred_variable_play", "record_strobe_variable")
      for s, r in ((1.0, False), (0.5, True), (100.25, False))],
    ("mmc variable_play speed 0", {"type": "mmc", "command": "variable_play", "speed": 0}),
    ("mmc wait", {"type": "mmc", "command": "wait", "device_id": 9}),
    ("mmc resume", {"type": "mmc", "command": "resume", "device_id": 9}),
    ("mmc drop_frame_adjust", {"type": "mmc", "command": "drop_frame_adjust", "name": "gp3"}),
    ("mmc move", {"type": "mmc", "command": "move", "destination": "gp0", "source": "actual_offset"}),
    ("mmc add", {"type": "mmc", "command": "add", "destination": "gp1", "source_1": "gp2", "source_2": "selected_time_code"}),
    ("mmc subtract", {"type": "mmc", "command": "subtract", "destination": "gp1", "source_1": "gp1", "source_2": "gp7"}),
    ("mmc group assign", {"type": "mmc", "command": "group", "action": "assign", "group": 3, "device_ids": [1, 2, 5]}),
    ("mmc group dis_assign all", {"type": "mmc", "command": "group", "action": "dis_assign", "group": 0x7F, "device_ids": [0x7F]}),
    ("mmc procedure assemble", {"type": "mmc", "command": "procedure", "action": "assemble", "procedure": 2,
                                "commands": [{"type": "mmc", "command": "stop"},
                                             {"type": "mmc", "command": "locate", **TC, "subframes": 0, "frame_rate": "25"}]}),
    *[(f"mmc procedure {a}", {"type": "mmc", "command": "procedure", "action": a, "procedure": 2})
      for a in ("delete", "set", "execute")],
    ("mmc procedure delete all", {"type": "mmc", "command": "procedure", "action": "delete", "procedure": 0x7F}),
    ("mmc event define", {"type": "mmc", "command": "event", "action": "define", "event": 1,
                          "direction": "both", "all_speeds": True, "non_delete": True,
                          "trigger_source": "selected_time_code", "name": "gp4",
                          "trigger_command": {"type": "mmc", "command": "play"}}),
    ("mmc event define forward", {"type": "mmc", "command": "event", "action": "define", "event": 3,
                                  "direction": "forward", "trigger_source": "midi_time_code_input",
                                  "name": "gp0", "trigger_command": {"type": "mmc", "command": "stop"}}),
    *[(f"mmc event {a}", {"type": "mmc", "command": "event", "action": a, "event": 1})
      for a in ("delete", "set", "test")],
    ("mmc read", {"type": "mmc", "command": "read", "names": ["selected_time_code", "track_mute"]}),
    ("mmc write", {"type": "mmc", "command": "write", "fields": [
        {"name": "gp0", **TC, "frame_rate": "30nondrop", "subframes": 12},
        {"name": "generator_time_code", "hours": 0, "minutes": 0, "seconds": 1, "frames": 2,
         "frame_rate": "24", "use_status_byte": True, "estimated": True, "no_time_code": True,
         "color_frame": True, "blank": True, "negative": True}]}),
    ("mmc masked_write", {"type": "mmc", "command": "masked_write", "fields": [
        {"name": "track_mute", "byte_number": 0, "mask": 0x7F, "data": 0x15},
        {"name": "track_record_ready", "byte_number": 2, "mask": 0x01, "data": 0x01}]}),
    ("mmc update begin", {"type": "mmc", "command": "update", "action": "begin", "names": ["gp0", "track_mute"]}),
    ("mmc update end", {"type": "mmc", "command": "update", "action": "end", "names": ["gp0"]}),
    ("mmc update end all", {"type": "mmc", "command": "update", "action": "end", "names": ["all"]}),
    # MSC
    *[(f"msc {c}", {"type": "msc", "command_format": "lighting", "command": c})
      for c in ("go", "stop", "resume", "all_off", "restore", "reset", "go_off")],
    ("msc go cue list path", {"type": "msc", "command_format": "sound", "command": "go",
                              "q_number": "235.6", "q_list": "36.6", "q_path": "59"}),
    ("msc go cue number", {"type": "msc", "command_format": "all_types", "command": "go", "q_number": "1"}),
    ("msc command_format_raw", {"type": "msc", "command_format_raw": 0x11, "command": "stop", "device_id": 3}),
    ("msc load", {"type": "msc", "command_format": "video", "command": "load", "q_number": "12", "q_list": "3"}),
    ("msc timed_go", {"type": "msc", "command_format": "machinery", "command": "timed_go",
                      **TC, "fractional_frames": 40, "frame_rate": "30nondrop", "q_number": "7"}),
    ("msc set", {"type": "msc", "command_format": "lighting", "command": "set",
                 "control_number": 300, "control_value": 16000}),
    ("msc set with time", {"type": "msc", "command_format": "lighting", "command": "set",
                           "control_number": 1, "control_value": 2, **TC,
                           "fractional_frames": 5, "frame_rate": "25"}),
    ("msc fire", {"type": "msc", "command_format": "pyro", "command": "fire", "macro_number": 100}),
    *[(f"msc format {f}", {"type": "msc", "command_format": f, "command": "go"})
      for f in ("projection", "process_control")],
    # General MIDI, Device Inquiry, Device Control
    ("gm_system on", {"type": "gm_system", "command": "on"}),
    ("gm_system off", {"type": "gm_system", "command": "off", "device_id": 16}),
    ("device_inquiry request", {"type": "device_inquiry", "command": "request"}),
    ("device_inquiry reply", {"type": "device_inquiry", "command": "reply", "device_id": 0x10,
                              "manufacturer_id": 0x41, "device_family_code": 0x0123,
                              "device_family_member_code": 300, "software_revision": [1, 2, 3, 4]}),
    ("device_inquiry reply extended id", {"type": "device_inquiry", "command": "reply",
                                          "manufacturer_id": [0, 0x20, 0x6B], "device_family_code": 2,
                                          "device_family_member_code": 3, "software_revision": [0, 0, 0, 1]}),
    ("device_control master_volume", {"type": "device_control", "command": "master_volume", "value": 16383}),
    ("device_control master_balance", {"type": "device_control", "command": "master_balance", "value": 8192, "device_id": 1}),
    # MIDI Tuning
    ("midi_tuning bulk_dump_request", {"type": "midi_tuning", "command": "bulk_dump_request", "tuning_program": 3}),
    ("midi_tuning bulk_dump_reply", {"type": "midi_tuning", "command": "bulk_dump_reply", "tuning_program": 3,
                                     "tuning_name": "Just C", "notes": TUNING_NOTES}),
    ("midi_tuning note_change", {"type": "midi_tuning", "command": "note_change", "tuning_program": 0,
                                 "changes": [{"key": 69, "semitone": 69, "cents": 0},
                                             {"key": 70, "semitone": 70, "cents": 99.99},
                                             {"key": 71, "no_change": True}]}),
    # Notation
    *[(f"notation bar_marker {b}", {"type": "notation", "command": "bar_marker", "bar_number": b})
      for b in (-8192, 0, 1, 8191)],
    ("notation time_signature_immediate", {"type": "notation", "command": "time_signature_immediate",
                                           "numerator": 6, "denominator": 8, "clocks_per_click": 36,
                                           "notated_32nd_notes_per_beat": 8}),
    ("notation time_signature_delayed compound", {"type": "notation", "command": "time_signature_delayed",
                                                  "numerator": 3, "denominator": 4, "clocks_per_click": 24,
                                                  "notated_32nd_notes_per_beat": 8,
                                                  "compound": [{"numerator": 2, "denominator": 8},
                                                               {"numerator": 5, "denominator": 16}]}),
    # MTC Cueing, real time
    ("mtc_cueing special_system_stop", {"type": "mtc_cueing", "command": "special_system_stop"}),
    *[(f"mtc_cueing {c}", {"type": "mtc_cueing", "command": c, "event_number": 300})
      for c in ("punch_in", "punch_out", "event_start", "event_stop", "cue_point")],
    *[(f"mtc_cueing {c} message", {"type": "mtc_cueing", "command": c, "event_number": 1,
                                   "additional_info_message": {"type": "note_on", "channel": 1, "note": 70, "velocity": 127}})
      for c in ("event_start_with_info", "event_stop_with_info", "cue_point_with_info")],
    ("mtc_cueing cue_point_with_info bytes", {"type": "mtc_cueing", "command": "cue_point_with_info",
                                             "event_number": 2, "additional_info_bytes": [0x91, 0x46, 0x7F]}),
    ("mtc_cueing event_name", {"type": "mtc_cueing", "command": "event_name", "event_number": 4, "event_name": "Hit"}),
    # MTC Cueing, non-real time
    *[(f"mtc_cueing_nrt {c}", {"type": "mtc_cueing_nrt", "command": c})
      for c in ("special_enable_event_list", "special_disable_event_list",
                "special_clear_event_list", "special_system_stop")],
    *[(f"mtc_cueing_nrt {c}", {"type": "mtc_cueing_nrt", "command": c, **TC,
                               "fractional_frames": 20, "frame_rate": "25"})
      for c in ("special_time_code_offset", "special_event_list_request")],
    *[(f"mtc_cueing_nrt {c}", {"type": "mtc_cueing_nrt", "command": c, **TC, "fractional_frames": 0,
                               "frame_rate": "30drop", "event_number": 1000})
      for c in ("punch_in", "punch_out", "delete_punch_in", "delete_punch_out",
                "event_start", "event_stop", "delete_event_start", "delete_event_stop",
                "cue_point", "delete_cue_point")],
    *[(f"mtc_cueing_nrt {c}", {"type": "mtc_cueing_nrt", "command": c, **TC, "fractional_frames": 1,
                               "frame_rate": "24", "event_number": 5,
                               "additional_info_bytes": [0xB0, 0x07, 0x64]})
      for c in ("event_start_with_info", "event_stop_with_info", "cue_point_with_info")],
    ("mtc_cueing_nrt event_name", {"type": "mtc_cueing_nrt", "command": "event_name", **TC,
                                   "fractional_frames": 0, "frame_rate": "25", "event_number": 6,
                                   "event_name": "Intro"}),
    # File Dump
    ("file_dump request", {"type": "file_dump", "command": "request", "device_id": 1,
                           "source_device_id": 2, "file_type": "MIDI", "filename": "song.mid"}),
    ("file_dump header", {"type": "file_dump", "command": "header", "device_id": 1, "source_device_id": 2,
                          "file_type": "BIN ", "filename": "a.bin", "length": 0x0ABCDEF}),
    ("file_dump header unknown length", {"type": "file_dump", "command": "header", "device_id": 1,
                                         "source_device_id": 2, "file_type": "TEXT", "length": 0}),
    ("file_dump data_packet", {"type": "file_dump", "command": "data_packet", "device_id": 1, "packet_number": 3,
                               "stored_bytes": [0x00, 0xFF, 0x80, 0x7F, 0x01, 0xFE, 0x55, 0xAA, 0x10, 0x90]}),
    *[(f"file_dump {c}", {"type": "file_dump", "command": c, "device_id": 1, "packet_number": 4})
      for c in ("ack", "nak")],
    *[(f"file_dump {c}", {"type": "file_dump", "command": c, "device_id": 1})
      for c in ("eof", "wait", "cancel")],
    # GM2 / CA (step 6a)
    ("gm_system gm2_on", {"type": "gm_system", "command": "gm2_on"}),
    ("device_control master_fine_tuning", {"type": "device_control", "command": "master_fine_tuning", "value": 8192}),
    ("device_control master_coarse_tuning", {"type": "device_control", "command": "master_coarse_tuning", "value": 70}),
    ("device_control gpc reverb", {"type": "device_control", "command": "global_parameter_control",
                                   "effect": "reverb", "parameters": [{"parameter": "type", "value": 4},
                                                                      {"parameter": "time", "value": 64}]}),
    ("device_control gpc chorus numeric", {"type": "device_control", "command": "global_parameter_control",
                                           "effect": "chorus", "parameters": [{"parameter": 3, "value": 16},
                                                                              {"parameter": "send_to_reverb", "value": 9}]}),
    ("device_control gpc raw widths", {"type": "device_control", "command": "global_parameter_control",
                                       "slot_path": [[1, 3], [2, 4]], "parameter_width": 2, "value_width": 3,
                                       "parameters": [{"parameter": [0, 1], "value": [5, 6, 7]}]}),
    ("controller_destination channel_pressure", {"type": "controller_destination", "command": "channel_pressure",
                                                 "channel": 6, "destinations": [{"parameter": "pitch", "range": 0x42},
                                                                                {"parameter": "filter_cutoff", "range": 0x60}]}),
    ("controller_destination poly_pressure", {"type": "controller_destination", "command": "poly_pressure",
                                              "destinations": [{"parameter": "amplitude", "range": 0x40}]}),
    ("controller_destination control_change", {"type": "controller_destination", "command": "control_change",
                                               "channel": 2, "control": 1, "destinations": [{"parameter": 0x05, "range": 0x20}]}),
    ("key_based_instrument_control", {"type": "key_based_instrument_control", "channel": 9, "key": 38,
                                      "controllers": [{"control": 7, "value": 0x50}, {"control": 0x78, "value": 0x40}]}),
    ("rpn modulation_depth_range", {"type": "rpn", "parameter": "modulation_depth_range", "value": 0x0040}),
    ("rpn null", {"type": "rpn", "parameter": "null", "channel": 3}),
    ("err: controller_destination control 0x30", {"type": "controller_destination", "command": "control_change",
                                                  "control": 0x30, "destinations": [{"parameter": 0, "range": 1}]}),
    ("err: key_based control data entry", {"type": "key_based_instrument_control", "key": 1,
                                           "controllers": [{"control": 6, "value": 1}]}),
    ("err: gpc effect and slot_path", {"type": "device_control", "command": "global_parameter_control",
                                       "effect": "reverb", "slot_path": [[1, 1]], "parameters": [{"parameter": 0, "value": 1}]}),
    ("err: gpc value width mismatch", {"type": "device_control", "command": "global_parameter_control",
                                       "slot_path": [[1, 1]], "value_width": 2, "parameters": [{"parameter": 0, "value": [1]}]}),
    # MIDI Tuning Updated Specification (step 6b)
    ("midi_tuning bulk_dump_request_bank", {"type": "midi_tuning", "command": "bulk_dump_request_bank",
                                            "bank": 2, "tuning_program": 5}),
    ("midi_tuning key_based_dump", {"type": "midi_tuning", "command": "key_based_dump", "bank": 1,
                                    "tuning_program": 3, "tuning_name": "Werckmeister", "notes": TUNING_NOTES}),
    ("midi_tuning scale_octave_dump_1byte", {"type": "midi_tuning", "command": "scale_octave_dump_1byte",
                                             "bank": 0, "tuning_program": 7, "tuning_name": "Just",
                                             "offsets": [64, 52, 68, 80, 50, 62, 54, 66, 78, 48, 60, 76]}),
    ("midi_tuning scale_octave_dump_2byte", {"type": "midi_tuning", "command": "scale_octave_dump_2byte",
                                             "bank": 127, "tuning_program": 127, "tuning_name": "",
                                             "offsets": [8192, 0, 16383, 8000, 8400, 1, 2, 3, 9000, 7000, 8192, 100]}),
    ("midi_tuning note_change_bank", {"type": "midi_tuning", "command": "note_change_bank", "bank": 4,
                                      "tuning_program": 1, "changes": [{"key": 60, "semitone": 60, "cents": 50}]}),
    ("midi_tuning note_change_bank non real time", {"type": "midi_tuning", "command": "note_change_bank",
                                                    "real_time": False, "bank": 4, "tuning_program": 1,
                                                    "changes": [{"key": 61, "no_change": True}]}),
    ("midi_tuning scale_octave_1byte", {"type": "midi_tuning", "command": "scale_octave_1byte",
                                        "channels": [0, 9, 15], "offsets": [64] * 11 + [70]}),
    ("midi_tuning scale_octave_1byte non real time", {"type": "midi_tuning", "command": "scale_octave_1byte",
                                                      "real_time": False, "channels": [6, 7, 13, 14],
                                                      "offsets": list(range(58, 70))}),
    ("midi_tuning scale_octave_2byte", {"type": "midi_tuning", "command": "scale_octave_2byte",
                                        "channels": list(range(16)), "offsets": [8192] * 12}),
    ("midi_tuning scale_octave_2byte non real time", {"type": "midi_tuning", "command": "scale_octave_2byte",
                                                      "real_time": False, "channels": [3],
                                                      "offsets": [0, 16383] * 6}),
    ("err: scale_octave 11 offsets", {"type": "midi_tuning", "command": "scale_octave_1byte",
                                      "channels": [0], "offsets": [64] * 11}),
    ("err: scale_octave channel 16", {"type": "midi_tuning", "command": "scale_octave_2byte",
                                      "channels": [16], "offsets": [8192] * 12}),
    ("err: tuning real_time not bool", {"type": "midi_tuning", "command": "note_change_bank", "real_time": "yes",
                                        "bank": 0, "tuning_program": 0, "changes": [{"key": 1, "no_change": True}]}),
    ("err: key_based_dump missing bank", {"type": "midi_tuning", "command": "key_based_dump",
                                          "tuning_program": 0, "notes": TUNING_NOTES}),
    # MTC User Bits (step 6c)
    ("mtc_user_bits binary_groups", {"type": "mtc_user_bits",
                                     "binary_groups": [6, 1, 2, 5, 3, 0, 1, 0], "flags": 2}),
    ("mtc_user_bits characters", {"type": "mtc_user_bits", "characters": "REEL", "device_id": 3}),
    ("err: mtc_user_bits both inputs", {"type": "mtc_user_bits", "characters": "ABCD",
                                        "binary_groups": [0] * 8}),
    ("err: mtc_user_bits nibble 16", {"type": "mtc_user_bits", "binary_groups": [16] + [0] * 7}),
    ("err: mtc_user_bits flags 4", {"type": "mtc_user_bits", "binary_groups": [0] * 8, "flags": 4}),
    # Sample Dump Standard (step 6d)
    ("sample_dump header", {"type": "sample_dump", "command": "header", "sample_number": 300,
                            "sample_format": 16, "sample_period": 22676, "sample_length": 100000,
                            "sustain_loop_start": 1000, "sustain_loop_end": 99000, "loop_type": "forward"}),
    ("sample_dump request", {"type": "sample_dump", "command": "request", "sample_number": 5, "device_id": 1}),
    ("sample_dump data_packet words 12-bit", {"type": "sample_dump", "command": "data_packet", "packet_number": 0,
                                              "sample_format": 12, "words": [0xFFF, 0x000, 0x800, 0x123]}),
    ("sample_dump data_packet words 24-bit", {"type": "sample_dump", "command": "data_packet", "packet_number": 127,
                                              "sample_format": 24, "words": [i * 559241 for i in range(30)]}),
    ("sample_dump data_packet raw data", {"type": "sample_dump", "command": "data_packet", "packet_number": 9,
                                          "data": [(i * 7) % 128 for i in range(120)]}),
    ("sample_dump loop_points", {"type": "sample_dump", "command": "loop_points", "sample_number": 1,
                                 "loop_number": 2, "loop_type": "bidirectional", "loop_start": 10,
                                 "loop_end": 20000}),
    ("sample_dump loop_points delete all", {"type": "sample_dump", "command": "loop_points", "sample_number": 1,
                                            "loop_number": "all", "loop_type": "off", "loop_start": 0,
                                            "loop_end": 0}),
    ("sample_dump loop_points_request all", {"type": "sample_dump", "command": "loop_points_request",
                                             "sample_number": 16383, "loop_number": "all"}),
    ("err: sample_dump too many 8-bit words", {"type": "sample_dump", "command": "data_packet", "packet_number": 0,
                                               "sample_format": 8, "words": [0] * 61}),
    ("err: sample_dump format 29", {"type": "sample_dump", "command": "data_packet", "packet_number": 0,
                                    "sample_format": 29, "words": [0]}),
    ("err: sample_dump data and words", {"type": "sample_dump", "command": "data_packet", "packet_number": 0,
                                         "data": [0], "words": [0], "sample_format": 8}),
    ("err: sample_dump bad loop_type", {"type": "sample_dump", "command": "loop_points", "sample_number": 0,
                                        "loop_number": 0, "loop_type": "pingpong", "loop_start": 0, "loop_end": 1}),
    # MSC Sound Commands and Two-Phase Commit (step 6e)
    *[(f"msc {c}", {"type": "msc", "command_format": "sound", "command": c})
      for c in ("standby_plus", "standby_minus", "sequence_plus", "sequence_minus",
                "start_clock", "stop_clock", "zero_clock", "mtc_chase_on", "mtc_chase_off")],
    ("msc standby_plus q_list", {"type": "msc", "command_format": "sound", "command": "standby_plus", "q_list": "3"}),
    ("msc set_clock", {"type": "msc", "command_format": "sound", "command": "set_clock", **TC,
                       "fractional_frames": 0, "frame_rate": "30nondrop"}),
    ("msc set_clock q_list", {"type": "msc", "command_format": "sound", "command": "set_clock", **TC,
                              "fractional_frames": 7, "frame_rate": "25", "q_list": "12.5"}),
    ("msc open_cue_list", {"type": "msc", "command_format": "sound", "command": "open_cue_list", "q_list": "4"}),
    ("msc close_cue_list", {"type": "msc", "command_format": "sound", "command": "close_cue_list", "q_list": "4"}),
    ("msc open_cue_path", {"type": "msc", "command_format": "sound", "command": "open_cue_path", "q_path": "59"}),
    ("msc close_cue_path", {"type": "msc", "command_format": "sound", "command": "close_cue_path", "q_path": "59"}),
    ("msc standby", {"type": "msc", "command_format": "machinery", "command": "standby", "checksum": 0x1234,
                     "sequence_number": 1, "cue_data": [1, 2, 3, 4], "q_number": "34", "q_list": "2"}),
    ("msc standby default cue_data", {"type": "msc", "command_format": "lighting", "command": "standby",
                                      "checksum": 0, "sequence_number": 16383, "q_number": "118.1"}),
    ("msc standing_by", {"type": "msc", "command_format": "machinery", "command": "standing_by",
                         "checksum": 5, "sequence_number": 1, "hours": 0, "minutes": 0, "seconds": 20,
                         "frames": 0, "fractional_frames": 0, "frame_rate": "30nondrop"}),
    ("msc standing_by with cue", {"type": "msc", "command_format": "machinery", "command": "standing_by",
                                  "checksum": 5, "sequence_number": 1, **TC, "fractional_frames": 1,
                                  "frame_rate": "25", "q_number": "34"}),
    ("msc go_2pc", {"type": "msc", "command_format": "sound", "command": "go_2pc", "checksum": 300,
                    "sequence_number": 10, "cue_data": [0, 0, 0, 127], "q_number": "109"}),
    ("msc complete", {"type": "msc", "command_format": "sound", "command": "complete", "checksum": 7,
                      "sequence_number": 10}),
    ("msc cancel", {"type": "msc", "command_format": "sound", "command": "cancel", "checksum": 8,
                    "sequence_number": 11, "q_number": "109", "q_list": "1", "q_path": "2"}),
    ("msc cancelled named", {"type": "msc", "command_format": "sound", "command": "cancelled", "checksum": 9,
                             "status": "completing", "sequence_number": 11}),
    ("msc cancelled manual override", {"type": "msc", "command_format": "sound", "command": "cancelled",
                                       "checksum": 9, "status": "manual_override_in_progress",
                                       "sequence_number": 11}),
    ("msc abort named", {"type": "msc", "command_format": "process_control", "command": "abort", "checksum": 1,
                         "status": "deadman_interlock_not_established", "sequence_number": 7}),
    ("msc abort manual override in progress", {"type": "msc", "command_format": "lighting", "command": "abort",
                                               "checksum": 1, "status": "manual_override_in_progress",
                                               "sequence_number": 7}),
    ("msc abort numeric dependent status", {"type": "msc", "command_format": "sound", "command": "abort",
                                            "checksum": 2, "status": 0x1008, "sequence_number": 8}),
    ("err: msc open_cue_list without q_list", {"type": "msc", "command_format": "sound", "command": "open_cue_list"}),
    ("err: msc standby without q_number", {"type": "msc", "command_format": "sound", "command": "standby",
                                           "checksum": 0, "sequence_number": 1}),
    ("err: msc standby without checksum", {"type": "msc", "command_format": "sound", "command": "standby",
                                           "sequence_number": 1, "q_number": "1"}),
    ("err: msc abort status low bits", {"type": "msc", "command_format": "sound", "command": "abort",
                                        "checksum": 0, "status": 0x8001, "sequence_number": 1}),
    ("err: msc cancelled abort-only status", {"type": "msc", "command_format": "sound", "command": "cancelled",
                                              "checksum": 0, "status": "checksum_error", "sequence_number": 1}),
    ("msc 2pc computed checksum, cancelled", {"type": "msc", "command_format": "lighting",
                                             "command": "cancelled", "checksum": "msb_first",
                                             "status": "paused", "sequence_number": 7}),
    ("msc 2pc computed checksum, standing_by", {"type": "msc", "command_format": "sound",
                                               "command": "standing_by", "checksum": "lsb_first",
                                               "sequence_number": 2, **TC, "frame_rate": "25",
                                               "fractional_frames": 0, "q_number": "4.5"}),
    ("err: msc 2pc checksum name", {"type": "msc", "command_format": "sound", "command": "complete",
                                    "checksum": "both", "sequence_number": 1}),
    ("err: msc cue_data 3 values", {"type": "msc", "command_format": "sound", "command": "go_2pc", "checksum": 0,
                                    "sequence_number": 1, "cue_data": [0, 0, 0], "q_number": "1"}),
    # MMC LOCATE [I/F] and several commands per message (step 6f-1)
    ("mmc locate i/f", {"type": "mmc", "command": "locate", "name": "gp3"}),
    ("mmc several commands", {"type": "mmc", "device_id": 5, "batch": [
        {"command": "stop"},
        {"command": "locate", **TC, "subframes": 0, "frame_rate": "25"},
        {"command": "play"}]}),
    ("mmc several commands with all-call", {"type": "mmc", "device_id": 5, "batch": [
        {"command": "assign_system_master", "target_device_id": 2}, {"command": "chase"}]}),
    ("err: mmc locate name and time", {"type": "mmc", "command": "locate", "name": "gp1", **TC,
                                       "subframes": 0, "frame_rate": "25"}),
    ("err: mmc locate i/f non-GP field", {"type": "mmc", "command": "locate", "name": "selected_time_code"}),
    ("err: mmc wait with other commands", {"type": "mmc", "batch": [{"command": "wait"}, {"command": "stop"}]}),
    ("err: mmc command and batch", {"type": "mmc", "command": "stop", "batch": [{"command": "play"}]}),
    ("err: mmc command string over 48 bytes", {"type": "mmc", "batch": [
        {"command": "locate", **TC, "subframes": 0, "frame_rate": "25"}] * 7}),
    # MMC COMMAND SEGMENT (step 6f-3)
    ("mmc command_segment single", {"type": "mmc", "command": "command_segment", "first": True,
                                    "remaining": 2, "data": [0x44, 0x06, 0x01, 0x21]}),
    ("mmc seven locates segmented", {"type": "mmc", "device_id": 3, "segment": True,
                                     "batch": [{"command": "locate", **TC, "subframes": 0, "frame_rate": "25"}] * 7}),
    ("mmc segmented size 1", {"type": "mmc", "segment": True, "segment_size": 1,
                              "batch": [{"command": "stop"}, {"command": "play"}]}),
    ("err: mmc command_segment with other commands", {"type": "mmc", "batch": [
        {"command": "command_segment", "first": True, "remaining": 0, "data": [1]},
        {"command": "stop"}]}),
    ("err: mmc segment_size 46", {"type": "mmc", "segment": True, "segment_size": 46,
                                  "command": "stop"}),
    ("err: mmc command_segment remaining 64", {"type": "mmc", "command": "command_segment",
                                               "first": True, "remaining": 64, "data": [1]}),
    ("mmc read new fields", {"type": "mmc", "command": "read",
                             "names": ["motion_control_tally", "short_generator_time_code", "signature"]}),
    ("mmc update short field", {"type": "mmc", "command": "update", "action": "begin",
                                "names": ["short_selected_time_code", "motion_control_tally"]}),
    # MMC count-prefixed fields in WRITE (step 6f-2b-1)
    ("mmc write byte fields", {"type": "mmc", "command": "write", "fields": [
        {"name": "stop_mode", "value": "enable_monitoring"},
        {"name": "chase_mode", "value": "absolute_resolve"},
        {"name": "step_length", "value": 0x32},
        {"name": "fixed_speed", "value": "local"},
        {"name": "time_standard", "frame_rate": "25"}]}),
    ("mmc write vitc and update rate", {"type": "mmc", "command": "write", "fields": [
        {"name": "vitc_insert_enable", "control": "enable", "first_line": 0x10, "second_line": "local"},
        {"name": "update_rate", "value": 2}]}),
    ("mmc write track bitmaps", {"type": "mmc", "command": "write", "fields": [
        {"name": "track_mute", "video": True, "active_tracks": [1, 2, 9, 10]},
        {"name": "track_record_ready", "bitmap_bytes": [0x20, 0x01]},
        {"name": "track_input_monitor", "active_tracks": []}]}),
    ("mmc write time code and byte field", {"type": "mmc", "command": "write", "fields": [
        {"name": "gp1", **TC, "frame_rate": "24"}, {"name": "record_mode", "value": "rehearse"}]}),
    ("err: mmc write read-only record_status", {"type": "mmc", "command": "write",
                                                "fields": [{"name": "record_status", "value": 1}]}),
    ("err: mmc write stop_mode bad name", {"type": "mmc", "command": "write",
                                           "fields": [{"name": "stop_mode", "value": "sometimes"}]}),
    ("err: mmc write time_standard bad encoding", {"type": "mmc", "command": "write", "fields": [
        {"name": "time_standard", "frame_rate": "25", "encoding": "sideways"}]}),
    ("err: mmc write track 318", {"type": "mmc", "command": "write",
                                  "fields": [{"name": "track_mute", "active_tracks": [318]}]}),
    ("mmc write generator_userbits", {"type": "mmc", "command": "write", "fields": [
        {"name": "generator_userbits", "characters": "REEL", "flags": 1}]}),
    ("mmc write set-ups", {"type": "mmc", "command": "write", "fields": [
        {"name": "generator_set_up", "run_reference": "internal_drop_a", "copy_jam_reference": "external",
         "copy_jam_source": "selected_master_code", "copy_jam_mode": "continue"},
        {"name": "midi_time_code_set_up", "transmit_while_stopped": True, "transmit_userbits": True,
         "source": "generator_time_code"}]}),
    ("err: mmc write generator_set_up run_reference 9", {"type": "mmc", "command": "write", "fields": [
        {"name": "generator_set_up", "run_reference": 9, "copy_jam_reference": 0,
         "copy_jam_source": 1, "copy_jam_mode": 0}]}),
    # Errors
    ("err: unknown type", {"type": "bogus"}),
    ("err: missing type", {}),
    ("err: note_on missing note", {"type": "note_on"}),
    ("err: note_on velocity 128", {"type": "note_on", "note": 1, "velocity": 128}),
    ("err: sysex byte 128", {"type": "sysex", "data": [128]}),
    ("err: sysex missing data", {"type": "sysex"}),
    ("err: mtc_full missing frame_rate", {"type": "mtc_full", **TC}),
    ("err: mtc_full minutes 60", {"type": "mtc_full", **TC, "minutes": 60, "frame_rate": "25"}),
    ("err: mtc_full hours 24", {"type": "mtc_full", **TC, "hours": 24, "frame_rate": "25"}),
    ("err: mtc_full bad frame_rate", {"type": "mtc_full", **TC, "frame_rate": "29.97"}),
    ("err: mmc missing command", {"type": "mmc"}),
    ("err: mmc bad command", {"type": "mmc", "command": "fly"}),
    ("err: mmc device_id 128", {"type": "mmc", "command": "stop", "device_id": 128}),
    ("err: mmc write read-only field", {"type": "mmc", "command": "write", "fields": [
        {"name": "actual_offset", **TC, "frame_rate": "25"}]}),
    ("err: mmc masked_write time code field", {"type": "mmc", "command": "masked_write", "fields": [
        {"name": "gp0", "byte_number": 0, "mask": 1, "data": 1}]}),
    ("err: mmc update begin all", {"type": "mmc", "command": "update", "action": "begin", "names": ["all"]}),
    ("err: mmc group assign 7F", {"type": "mmc", "command": "group", "action": "assign", "group": 0x7F, "device_ids": [1]}),
    ("err: mmc procedure execute 7F", {"type": "mmc", "command": "procedure", "action": "execute", "procedure": 0x7F}),
    ("err: mmc procedure nested assemble", {"type": "mmc", "command": "procedure", "action": "assemble", "procedure": 1,
                                            "commands": [{"type": "mmc", "command": "procedure", "action": "assemble",
                                                          "procedure": 2, "commands": [{"type": "mmc", "command": "stop"}]}]}),
    ("err: mmc speed negative", {"type": "mmc", "command": "shuttle", "speed": -1}),
    ("err: msc both formats", {"type": "msc", "command_format": "lighting", "command_format_raw": 1, "command": "go"}),
    ("err: msc q_list without q_number", {"type": "msc", "command_format": "lighting", "command": "go", "q_list": "1"}),
    ("err: msc set partial time", {"type": "msc", "command_format": "lighting", "command": "set",
                                   "control_number": 1, "control_value": 2, "hours": 1}),
    ("err: device_inquiry mfr 0 as int", {"type": "device_inquiry", "command": "reply", "manufacturer_id": 0,
                                          "device_family_code": 1, "device_family_member_code": 1,
                                          "software_revision": [0, 0, 0, 0]}),
    ("err: midi_tuning 127 notes", {"type": "midi_tuning", "command": "bulk_dump_reply", "tuning_program": 0,
                                    "notes": TUNING_NOTES[:127]}),
    ("err: midi_tuning cents 100", {"type": "midi_tuning", "command": "note_change", "tuning_program": 0,
                                    "changes": [{"key": 1, "semitone": 1, "cents": 100}]}),
    ("err: notation denominator 3", {"type": "notation", "command": "time_signature_immediate", "numerator": 3,
                                     "denominator": 3, "clocks_per_click": 24, "notated_32nd_notes_per_beat": 8}),
    ("err: mtc_cueing both info sources", {"type": "mtc_cueing", "command": "cue_point_with_info", "event_number": 1,
                                           "additional_info_bytes": [1],
                                           "additional_info_message": {"type": "clock"}}),
    ("err: file_dump missing device_id", {"type": "file_dump", "command": "eof"}),
    ("err: file_dump source 127", {"type": "file_dump", "command": "request", "device_id": 1,
                                   "source_device_id": 127, "file_type": "MIDI"}),
    ("err: file_dump file_type length", {"type": "file_dump", "command": "request", "device_id": 1,
                                         "source_device_id": 1, "file_type": "MID"}),
    ("err: rpn both parameter fields", {"type": "rpn", "parameter": "fine_tuning", "parameter_number": 1, "value": 1}),
    ("err: rpn msb_only value 128", {"type": "rpn", "parameter": "fine_tuning", "value": 128, "msb_only": True}),
    ("err: quarter_frame_sequence bad direction", {"type": "mtc_quarter_frame_sequence", **TC,
                                                   "frame_rate": "25", "direction": "sideways"}),
]

MMC_RESPONSES: list[tuple[str, list[int]]] = [
    ("selected_time_code", [0xF0, 0x7F, 0x01, 0x07, 0x01, 0x61, 0x25, 0x34, 0x10, 0x00, 0xF7]),
    ("gp0 status byte", [0xF0, 0x7F, 0x01, 0x07, 0x08, 0x20, 0x40, 0x40, 0x60, 0x48, 0xF7]),
    ("track_mute bitmap", [0xF0, 0x7F, 0x7F, 0x07, 0x62, 0x02, 0x05, 0x40, 0xF7]),
    ("response_error", [0xF0, 0x7F, 0x01, 0x07, 0x42, 0x02, 0x01, 0x4C, 0xF7]),
    ("time_standard with count 0", [0xF0, 0x7F, 0x01, 0x07, 0x45, 0x00, 0xF7]),
    ("err not mmc response", [0xF0, 0x7F, 0x01, 0x06, 0x01, 0xF7]),
    ("err missing F7", [0xF0, 0x7F, 0x01, 0x07, 0x01, 0x00]),
    ("err wrong length", [0xF0, 0x7F, 0x01, 0x07, 0x01, 0x00, 0x00, 0xF7]),
    ("err bitmap count mismatch", [0xF0, 0x7F, 0x01, 0x07, 0x62, 0x03, 0x05, 0xF7]),
    ("err empty", []),
    # Step 6f-2a: several fields per response, Short time code, handshakes.
    ("rp013 master: time code + motion control tally",
     [0xF0, 0x7F, 0x01, 0x07, 0x01, 0x60, 0x16, 0x05, 0x2C, 0x00, 0x48, 0x03, 0x02, 0x7F, 0x01, 0xF7]),
    ("rp013 master: short selected time code", [0xF0, 0x7F, 0x01, 0x07, 0x21, 0x2D, 0x00, 0xF7]),
    ("rp013 slave: selected time code", [0xF0, 0x7F, 0x02, 0x07, 0x01, 0x6A, 0x01, 0x3A, 0x3C, 0x00, 0xF7]),
    ("handshake wait", [0xF0, 0x7F, 0x01, 0x07, 0x7C, 0xF7]),
    ("unregistered 5-byte name", [0xF0, 0x7F, 0x01, 0x07, 0x10, 1, 2, 3, 4, 5, 0xF7]),
    ("err extension set", [0xF0, 0x7F, 0x01, 0x07, 0x00, 0x01, 0xF7]),
    # Step 6f-2b-1: count-prefixed field formats.
    ("stop_mode enable", [0xF0, 0x7F, 0x01, 0x07, 0x4A, 0x01, 0x01, 0xF7]),
    ("record_status bits", [0xF0, 0x7F, 0x01, 0x07, 0x4D, 0x01, 0x51, 0xF7]),
    ("vitc_insert_enable", [0xF0, 0x7F, 0x01, 0x07, 0x63, 0x03, 0x01, 0x10, 0x12, 0xF7]),
    ("time_standard field-definition form", [0xF0, 0x7F, 0x01, 0x07, 0x45, 0x01, 0x60, 0xF7]),
    ("time_standard unshifted form", [0xF0, 0x7F, 0x01, 0x07, 0x45, 0x01, 0x03, 0xF7]),
    ("fixed_speed undefined code", [0xF0, 0x7F, 0x01, 0x07, 0x56, 0x01, 0x3E, 0xF7]),
    ("stop_mode wrong length", [0xF0, 0x7F, 0x01, 0x07, 0x4A, 0x02, 0x01, 0x01, 0xF7]),
    # Step 6f-2b-2a: structured fields.
    ("rp013 appendix signature", [0xF0, 0x7F, 0x01, 0x07, 0x40, 0x2E, 0x01, 0x00, 0x00, 0x00,
                                  0x14, 0x7F, 0x71, 0, 0, 0, 0, 0, 0, 0, 0,
                                  0x3D, 0x60, 0x7F, 0, 0, 0, 0, 0, 0, 0x09,
                                  0x14, 0x3E, 0x1E, 0, 0, 0, 0x3E, 0x1E, 0, 0, 0,
                                  0x3F, 0x62, 0x00, 0x38, 0x00, 0x33, 0, 0, 0, 0x09, 0xF7]),
    ("command_error power-up state", [0xF0, 0x7F, 0x01, 0x07, 0x43, 0x04, 0x00, 0x00, 0x7F, 0x00, 0xF7]),
    ("command_error with command", [0xF0, 0x7F, 0x01, 0x07, 0x43, 0x06, 0x11, 0x7F, 0x40, 0x02, 0x00, 0x46, 0xF7]),
    ("velocity_tally reverse half speed", [0xF0, 0x7F, 0x01, 0x07, 0x49, 0x03, 0x40, 0x40, 0x00, 0xF7]),
    ("selected_time_code_userbits", [0xF0, 0x7F, 0x01, 0x07, 0x47, 0x09, 0x0C, 0x04, 0x05, 0x04, 0x05,
                                     0x04, 0x02, 0x05, 0x01, 0xF7]),
    # Step 6f-2b-2b.
    ("generator_command_tally copy/jam lost source", [0xF0, 0x7F, 0x01, 0x07, 0x5B, 0x02, 0x02, 0x12, 0xF7]),
    ("generator_set_up", [0xF0, 0x7F, 0x01, 0x07, 0x5C, 0x03, 0x12, 0x01, 0x01, 0xF7]),
    ("mtc_command_tally follow", [0xF0, 0x7F, 0x01, 0x07, 0x5E, 0x02, 0x02, 0x01, 0xF7]),
    ("mtc_set_up", [0xF0, 0x7F, 0x01, 0x07, 0x5F, 0x02, 0x15, 0x06, 0xF7]),
    ("procedure_response", [0xF0, 0x7F, 0x01, 0x07, 0x60, 0x0A, 0x02, 0x01,
                            0x44, 0x06, 0x01, 0x61, 0x25, 0x34, 0x10, 0x00, 0xF7]),
    ("procedure_response invalid", [0xF0, 0x7F, 0x01, 0x07, 0x60, 0x01, 0x7F, 0xF7]),
    ("event_response", [0xF0, 0x7F, 0x01, 0x07, 0x61, 0x09, 0x03, 0x42, 0x01,
                        0x61, 0x25, 0x34, 0x10, 0x00, 0x06, 0xF7]),
    ("failure text", [0xF0, 0x7F, 0x01, 0x07, 0x65, 0x09] + list(b"Tape jam!") + [0xF7]),
    # Step 6f-3.
    ("response_segment first of two", [0xF0, 0x7F, 0x01, 0x07, 0x64, 0x04, 0x41, 0x01, 0x60, 0x16, 0xF7]),
]

META_CASES: list[tuple[str, dict]] = [
    ("track_name", {"type": "track_name", "name": "Piano"}),
    ("set_tempo bpm", {"type": "set_tempo", "bpm": 120}),
    ("set_tempo tempo", {"type": "set_tempo", "tempo": 400000, "time": 96}),
    ("time_signature", {"type": "time_signature", "numerator": 7, "denominator": 8}),
    ("key_signature", {"type": "key_signature", "key": "F#m"}),
    ("smpte_offset", {"type": "smpte_offset", "frame_rate": 25, "hours": 1, "minutes": 2,
                      "seconds": 3, "frames": 4, "sub_frames": 5}),
    ("sequencer_specific", {"type": "sequencer_specific", "data": [0x41, 0x10]}),
    ("end_of_track", {"type": "end_of_track"}),
]


# --- Snapshot ----------------------------------------------------------------

def build_snapshot(midi1) -> dict:
    out: dict = {"messages": {}, "mmc_responses": {}, "meta": {}}
    for name, msg in CASES:
        try:
            built = midi1._build_message_sequence(dict(msg))
            out["messages"][name] = [[m.hex(), m.time] for m in built]
        except Exception as e:  # the exception type is what's recorded
            out["messages"][name] = {"error": type(e).__name__}
    for name, data in MMC_RESPONSES:
        out["mmc_responses"][name] = json.loads(midi1._decode_mmc_response({"data": data}))
    for name, msg in META_CASES:
        m = midi1._build_meta_message(dict(msg))
        out["meta"][name] = [bytes(m.bytes()).hex(" ").upper(), m.time]
    return out


def check_case_expectations(snapshot: dict) -> None:
    """Every 'err:' case raises and every other case builds."""
    wrong = [
        name for name, _ in CASES
        if isinstance(snapshot["messages"][name], dict) != name.startswith("err:")
    ]
    check("cases build or raise as named", not wrong, f"{wrong}")


def compare_snapshot(current: dict, recorded: dict) -> None:
    for section in ("messages", "mmc_responses", "meta"):
        cur, rec = current[section], recorded.get(section, {})
        changed = [k for k in cur if k in rec and cur[k] != rec[k]]
        missing = [k for k in cur if k not in rec]
        dropped = [k for k in rec if k not in cur]
        detail = "; ".join(
            f"{k}: recorded {rec[k]} != now {cur[k]}" for k in changed[:5]
        )
        check(f"snapshot {section}: {len(cur)} entries match", not changed, detail)
        check(f"snapshot {section}: no unrecorded entries", not missing, f"{missing}")
        check(f"snapshot {section}: no recorded entries dropped", not dropped, f"{dropped}")


# --- Spec worked examples ------------------------------------------------------
# Bytes printed in the official specs. Unlike the snapshot, these don't move
# when the snapshot is re-recorded.

SPEC_EXAMPLES: list[tuple[str, dict, list[str]]] = [
    ("RP-004/008 quarter frames, 01:37:52:16 30fps non-drop",
     {"type": "mtc_quarter_frame_sequence", **TC, "frame_rate": "30nondrop"},
     ["F1 00", "F1 11", "F1 24", "F1 33", "F1 45", "F1 52", "F1 61", "F1 76"]),
    ("MSC 1.0 cue 235.6 list 36.6 path 59",
     {"type": "msc", "command_format": "sound", "command": "go",
      "q_number": "235.6", "q_list": "36.6", "q_path": "59"},
     ["F0 7F 7F 02 10 01 32 33 35 2E 36 00 33 36 2E 36 00 35 39 F7"]),
    ("RP-013 p.39 GROUP dis-assign all devices from all groups",
     {"type": "mmc", "command": "group", "action": "dis_assign", "group": 0x7F,
      "device_ids": [0x7F]},
     ["F0 7F 7F 06 52 03 01 7F 7F F7"]),
    ("MTC spec additional info 91 46 7F nibblized",
     {"type": "mtc_cueing", "command": "cue_point_with_info", "event_number": 0,
      "additional_info_bytes": [0x91, 0x46, 0x7F]},
     ["F0 7F 7F 05 0C 00 00 01 09 06 04 0F 07 F7"]),
    ("MIDI Tuning 'Changing Tuning Programs' Bn 64 03 65 00 06 tt",
     {"type": "rpn", "parameter": "tuning_program_select", "value": 5, "msb_only": True},
     ["B0 64 03", "B0 65 00", "B0 06 05"]),
    ("CA-022 example: channel pressure -> pitch +2, cutoff +4800, LFO amp 25%",
     {"type": "controller_destination", "command": "channel_pressure", "channel": 6,
      "destinations": [{"parameter": "pitch", "range": 0x42},
                       {"parameter": "filter_cutoff", "range": 0x60},
                       {"parameter": "lfo_amplitude_depth", "range": 0x20}]},
     ["F0 7F 7F 09 01 06 00 42 01 60 05 20 F7"]),
    ("CA-026 Modulation Depth Range: Bn 64 05 65 00",
     {"type": "rpn", "parameter": "modulation_depth_range", "value": 3, "msb_only": True},
     ["B0 64 05", "B0 65 00", "B0 06 03"]),
    ("GM2 4.4 reverb: F0 7F <id> 04 05 01 01 01 01 01 pp vv",
     {"type": "device_control", "command": "global_parameter_control", "effect": "reverb",
      "parameters": [{"parameter": "type", "value": 4}]},
     ["F0 7F 7F 04 05 01 01 01 01 01 00 04 F7"]),
    ("RP-004/008 User Bits: characters reassemble as hhhhgggg ffffeeee ddddcccc bbbbaaaa",
     {"type": "mtc_user_bits", "characters": "ABCD"},
     ["F0 7F 7F 01 02 04 04 03 04 02 04 01 04 00 F7"]),
    ("Detailed Spec p.38: 12-bit word FFFH is sent as 7F 7C (checksum 00 by hand)",
     {"type": "sample_dump", "command": "data_packet", "packet_number": 0,
      "sample_format": 12, "words": [0xFFF]},
     ["F0 7E 7F 02 00 7F 7C " + " ".join(["00"] * 118) + " 00 F7"]),
    ("MSC 6.7: status 80 04 (completing) is s1 = 01, s2 = 40",
     {"type": "msc", "command_format": "sound", "command": "cancelled", "checksum": 0,
      "status": "completing", "sequence_number": 0},
     ["F0 7F 7F 02 10 25 00 00 01 40 00 00 F7"]),
    ("RP-013 appendix p.82: WRITE <TIME STANDARD> 03, <COMMAND ERROR LEVEL> 7F to group 7C",
     {"type": "mmc", "command": "write", "device_id": 0x7C, "fields": [
         {"name": "time_standard", "frame_rate": "30nondrop", "encoding": "unshifted"},
         {"name": "command_error_level", "value": "all_enabled"}]},
     ["F0 7F 7C 06 40 06 45 01 03 44 01 7F F7"]),
    # RP-013 p.8 gives aa..mm placeholders: an 11-byte string as 4 + 4 + 3,
    # <count> 05 42, 05 01, 04 00. Here the 11 bytes are LOCATE 01:37:52:16
    # (25 fps), STOP, PLAY, DEFERRED PLAY.
    ("RP-013 p.8 segmentation: 11-byte command string as counts 05/05/04, ids 42/01/00",
     {"type": "mmc", "segment": True, "segment_size": 4,
      "batch": [{"command": "locate", **TC, "subframes": 0, "frame_rate": "25"}, {"command": "stop"}, {"command": "play"},
                {"command": "deferred_play"}]},
     ["F0 7F 7F 06 53 05 42 44 06 01 21 F7", "F0 7F 7F 06 53 05 01 25 34 10 00 F7",
      "F0 7F 7F 06 53 04 00 01 02 03 F7"]),
    # Hand-computed from MSC 1.1.1 section 6.5 (the spec prints no checksum
    # example): STANDBY, device 01, sound, seq 1, cue data 0, Q_number "1".
    # Bytes 10 20 00 00 01 00 00 00 00 00 31 (+00) as pairs, plus the device
    # ID, AND 7F7F: low-first 2042+1 = 2043 -> 43 20; high-first 4220+1 =
    # 4221 -> 21 42.
    ("MSC 6.5 checksum, pairs low byte first (hand-computed)",
     {"type": "msc", "command_format": "sound", "command": "standby", "device_id": 1,
      "checksum": "lsb_first", "sequence_number": 1, "q_number": "1"},
     ["F0 7F 01 02 10 20 43 20 01 00 00 00 00 00 31 F7"]),
    ("MSC 6.5 checksum, pairs high byte first (hand-computed)",
     {"type": "msc", "command_format": "sound", "command": "standby", "device_id": 1,
      "checksum": "msb_first", "sequence_number": 1, "q_number": "1"},
     ["F0 7F 01 02 10 20 21 42 01 00 00 00 00 00 31 F7"]),
    # Captured from real hardware, not printed in a spec.
    ("Roland TR-8S Identity Reply (captured 2026-10-02)",
     {"type": "device_inquiry", "command": "reply", "device_id": 0x10,
      "manufacturer_id": 0x41, "device_family_code": 0x45 | (0x03 << 7),
      "device_family_member_code": 0, "software_revision": [0, 3, 0, 0]},
     ["F0 7E 10 06 02 41 45 03 00 00 00 03 00 00 F7"]),
    ("Arturia KeyStep 37 Identity Reply, 3-byte manufacturer ID (captured 2026-10-02)",
     {"type": "device_inquiry", "command": "reply", "device_id": 0x7F,
      "manufacturer_id": [0x00, 0x20, 0x6B], "device_family_code": 0x02,
      "device_family_member_code": 0x08, "software_revision": [0, 6, 1, 1]},
     ["F0 7E 7F 06 02 00 20 6B 02 00 08 00 00 06 01 01 F7"]),
]


def check_spec_examples(midi1) -> None:
    for name, msg, expected in SPEC_EXAMPLES:
        got = [m.hex() for m in midi1._build_message_sequence(dict(msg))]
        check(name, got == expected, f"got {got}")


# --- Decoding --------------------------------------------------------------------

# Bytes with two names decode to one of them.
DECODE_ALIASES = {"mtc_nak": "file_dump"}

# Multi-message types decode one wire message at a time.
SEQUENCE_TYPES = {"rpn", "nrpn", "mtc_quarter_frame_sequence"}


def check_round_trip(midi1) -> None:
    """Every built wire message decodes to a dict that rebuilds to the same
    bytes; every type that has a decoder decodes to itself."""
    mismatched, decoded_as = [], {}
    for name, msg in CASES:
        if name.startswith("err:"):
            continue
        for wire in midi1._build_message_sequence(dict(msg)):
            decoded = midi1._decode_message(wire)
            rebuilt = midi1._build_message(dict(decoded))
            if rebuilt.bytes() != wire.bytes():
                mismatched.append(f"{name}: {decoded}")
            decoded_as.setdefault(msg["type"], set()).add(decoded["type"])
    check("round trip: decode then build gives the same bytes", not mismatched,
          "; ".join(mismatched[:5]))

    decodable = {t for kinds in decoded_as.values() for t in kinds}
    partial = sorted(
        t for t, kinds in decoded_as.items()
        if t not in SEQUENCE_TYPES and DECODE_ALIASES.get(t, t) in decodable
        and kinds != {DECODE_ALIASES.get(t, t)}
    )
    check("round trip: a type with a decoder always decodes to itself",
          not partial, f"{[(t, sorted(decoded_as[t])) for t in partial]}")

    raw = sorted(t for t, kinds in decoded_as.items()
                 if kinds == {"sysex"} and t != "sysex")
    check(f"round trip: all {len(decoded_as)} built types decode to a typed dict",
          not raw, f"decoded as raw sysex: {raw}")

    for kind in ("mmc", "msc"):
        builders = set(getattr(midi1, f"_{kind.upper()}_DATA_BUILDERS"))
        decoders = set(getattr(midi1, f"_{kind.upper()}_DATA_DECODERS"))
        check(f"{kind}: every data builder has a decoder and vice versa",
              builders == decoders,
              f"no decoder: {sorted(builders - decoders)}; "
              f"no builder: {sorted(decoders - builders)}")

    for name, data in MMC_RESPONSES:
        if name.startswith("err") or not data:
            continue
        got = midi1._decode_message(midi1.mido.Message("sysex", data=data[1:-1]))
        action = json.loads(midi1._decode_mmc_response({"data": data}))
        expected_response = {k: v for k, v in action.items() if k not in ("status", "device_id")}
        check(f"receive decodes MMC Response: {name}",
              got.get("type") == "mmc_response" and got.get("response") == expected_response,
              f"got {got}")


DEVICE_REPLIES = [
    ("Roland TR-8S", [0x7E, 0x10, 0x06, 0x02, 0x41, 0x45, 0x03, 0x00, 0x00,
                      0x00, 0x03, 0x00, 0x00],
     {"type": "device_inquiry", "command": "reply", "device_id": 0x10,
      "manufacturer_id": 0x41, "device_family_code": 0x1C5,
      "device_family_member_code": 0, "software_revision": [0, 3, 0, 0]}),
    ("Arturia KeyStep 37", [0x7E, 0x7F, 0x06, 0x02, 0x00, 0x20, 0x6B, 0x02, 0x00,
                            0x08, 0x00, 0x00, 0x06, 0x01, 0x01],
     {"type": "device_inquiry", "command": "reply", "device_id": 0x7F,
      "manufacturer_id": [0x00, 0x20, 0x6B], "device_family_code": 2,
      "device_family_member_code": 8, "software_revision": [0, 6, 1, 1]}),
]


def check_msc_checksum(midi1) -> None:
    """A decoded 2PC message lists the 6.5 pairings its checksum matches."""
    base = {"type": "msc", "command_format": "sound", "command": "go_2pc", "device_id": 5,
            "sequence_number": 300, "q_number": "12.5", "q_list": "2"}
    wrong = []
    for order in ("lsb_first", "msb_first"):
        decoded = midi1._decode_message(midi1._build_message(dict(base, checksum=order)))
        if decoded.get("checksum_matches") != [order]:
            wrong.append(f"{order}: {decoded.get('checksum_matches')}")
    check("msc checksum: a computed checksum decodes as matching its own pairing only",
          not wrong, f"{wrong}")
    zero = midi1._decode_message(midi1._build_message(dict(base, checksum=0)))
    check("msc checksum: a checksum matching neither pairing gives []",
          zero.get("checksum_matches") == [], f"{zero}")
    plain = midi1._decode_message(midi1._build_message(
        {"type": "msc", "command_format": "sound", "command": "go", "q_number": "1"}))
    check("msc checksum: non-2PC commands carry no checksum_matches",
          "checksum_matches" not in plain, f"{plain}")


def check_decode_edges(midi1) -> None:
    """Wrong checksums are reported; malformed messages stay raw."""
    def build(msg):
        return list(midi1._build_message(msg).data)

    def decode(data):
        return midi1._decode_message(midi1.mido.Message("sysex", data=data))

    for name, msg in (
        ("midi_tuning bulk_dump_reply", dict(CASES)["midi_tuning bulk_dump_reply"]),
        ("file_dump data_packet", dict(CASES)["file_dump data_packet"]),
        ("midi_tuning key_based_dump", dict(CASES)["midi_tuning key_based_dump"]),
        ("midi_tuning scale_octave_dump_2byte", dict(CASES)["midi_tuning scale_octave_dump_2byte"]),
        ("sample_dump data_packet", dict(CASES)["sample_dump data_packet words 12-bit"]),
    ):
        good = build(msg)
        bad = good[:-1] + [good[-1] ^ 0x01]
        d_good, d_bad = decode(good), decode(bad)
        check(f"{name}: correct checksum decodes with checksum_ok true",
              d_good.get("checksum_ok") is True, f"{d_good.get('checksum_ok')}")
        same_fields = {k: v for k, v in d_bad.items() if k != "checksum_ok"} == \
            {k: v for k, v in d_good.items() if k != "checksum_ok"}
        check(f"{name}: wrong checksum decodes with checksum_ok false",
              d_bad.get("checksum_ok") is False and same_fields, f"{d_bad.get('checksum_ok')}")

    for name, data in (
        ("truncated MTC Full", [0x7F, 0x7F, 0x01, 0x01, 0x61, 0x25]),
        ("Identity Reply one byte short", [0x7E, 0x10, 0x06, 0x02, 0x41, 0x45, 0x03,
                                           0x00, 0x00, 0x00, 0x03, 0x00]),
        ("notation length byte wrong", [0x7F, 0x7F, 0x03, 0x02, 0x09, 0x04, 0x02, 0x18, 0x08]),
        ("MTC User Bits with a nibble above 15", [0x7F, 0x7F, 0x01, 0x02, 0x10, 0, 0, 0, 0, 0, 0, 0, 0]),
        ("MTC User Bits with flag bits 2-6 set", [0x7F, 0x7F, 0x01, 0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0x04]),
        ("manufacturer SysEx", [0x41, 0x10, 0x42, 0x12, 0x7F]),
        ("minutes with a flag bit set", [0x7F, 0x7F, 0x01, 0x01, 0x61, 0x65, 0x34, 0x10]),
    ):
        got = decode(data)
        check(f"malformed stays raw: {name}",
              got == {"type": "sysex", "data": data}, f"got {got}")


def check_stream_decoder(midi1) -> None:
    """RPN/NRPN and Quarter Frame reassembly."""
    M = midi1.mido.Message

    def feed(msgs):
        decoder = midi1._StreamDecoder()
        return [c for c in (decoder.feed(m) for m in msgs) if c is not None]

    def cc(control, value, channel=0):
        return M("control_change", channel=channel, control=control, value=value)

    wrong = []
    for name, msg in CASES:
        if name.startswith("err:") or msg["type"] not in SEQUENCE_TYPES:
            continue
        wire = midi1._build_message_sequence(dict(msg))
        combined = feed(wire)
        rebuilt = midi1._build_message_sequence(dict(combined[-1])) if combined else []
        if [m.bytes() for m in rebuilt] != [m.bytes() for m in wire]:
            wrong.append(f"{name}: {combined}")
    check("stream: every rpn/nrpn/quarter frame case reassembles and rebuilds",
          not wrong, "; ".join(wrong[:3]))

    got = feed([cc(99, 0x24), cc(98, 0x34), cc(6, 0x40), cc(38, 0x05)])
    check("stream: NRPN with MSB selected first (Hydrasynth order), 7- then 14-bit",
          got == [
              {"type": "nrpn", "channel": 0, "parameter_number": 0x1234, "value": 0x40, "msb_only": True},
              {"type": "nrpn", "channel": 0, "parameter_number": 0x1234,
               "value": (0x40 << 7) | 0x05, "msb_only": False},
          ], f"{got}")

    got = feed([cc(101, 0), cc(100, 0), cc(6, 2), cc(6, 12)])
    check("stream: selection persists across data entries",
          [c["value"] for c in got] == [2, 12]
          and all(c.get("parameter") == "pitch_bend_sensitivity" for c in got), f"{got}")

    got = feed([cc(99, 1, 0), cc(98, 2, 0), cc(99, 3, 1), cc(98, 4, 1),
                cc(6, 9, 0), cc(6, 10, 1)])
    check("stream: channels are tracked separately",
          [(c["channel"], c["parameter_number"], c["value"]) for c in got]
          == [(0, (1 << 7) | 2, 9), (1, (3 << 7) | 4, 10)], f"{got}")

    check("stream: RPN Null is reported and the data entry after it ignored",
          feed([cc(101, 127), cc(100, 127), cc(6, 5)])
          == [{"type": "rpn", "channel": 0, "parameter": "null"}])
    check("stream: Data Entry LSB with no MSB is ignored",
          feed([cc(99, 1), cc(98, 2), cc(38, 5)]) == [])

    def qf(**time):
        return midi1._build_message_sequence(
            {"type": "mtc_quarter_frame_sequence", "frame_rate": "25", **time})

    a = qf(hours=1, minutes=2, seconds=3, frames=4)
    b = qf(hours=1, minutes=2, seconds=3, frames=6)
    got = feed(a[3:] + b + a)
    check("stream: quarter frames joined mid-sequence complete on the next full run",
          [(c["frames"], c["direction"]) for c in got] == [(6, "forward"), (4, "forward")],
          f"{got}")
    rev = qf(hours=0, minutes=59, seconds=58, frames=24, direction="reverse")
    got = feed(rev)
    check("stream: reverse quarter frames",
          len(got) == 1 and got[0]["direction"] == "reverse" and got[0]["minutes"] == 59,
          f"{got}")
    check("stream: a missing quarter frame completes nothing",
          feed(a[:5] + a[6:]) == [])

    # MMC COMMAND SEGMENT / RESPONSE SEGMENT (RP-013 pp.8, 39, 73).
    def sx(*data):
        return M("sysex", data=data)

    def wire(msg):
        return midi1._build_message_sequence(dict(msg))

    spec = dict({n: m for n, m, _ in SPEC_EXAMPLES}[
        "RP-013 p.8 segmentation: 11-byte command string as counts 05/05/04, ids 42/01/00"])
    got = feed(wire(spec))
    check("stream: RP-013 p.8 segments reassemble to the four commands, segment_size 4",
          len(got) == 1 and [c["command"] for c in got[0].get("batch", [])]
          == ["locate", "stop", "play", "deferred_play"] and got[0].get("segment_size") == 4
          and [m.bytes() for m in wire(got[0])] == [m.bytes() for m in wire(spec)], f"{got}")
    long = {"type": "mmc", "device_id": 3, "segment": True, "batch": [{"command": "locate", **TC, "subframes": 0, "frame_rate": "25"}] * 7}
    got = feed(wire(long))
    check("stream: 56-byte command string in 45 + 11 rebuilds exactly",
          len(wire(long)) == 2 and len(got) == 1
          and [m.bytes() for m in wire(got[0])] == [m.bytes() for m in wire(long)], f"{got}")
    uneven = [sx(0x7F, 1, 6, 0x53, 3, 0x42, 0x01, 0x02), sx(0x7F, 1, 6, 0x53, 4, 0x01, 0x03, 0x01, 0x02),
              sx(0x7F, 1, 6, 0x53, 2, 0x00, 0x03)]
    got = feed(uneven)
    check("stream: an uneven split reassembles, without segment_size",
          len(got) == 1 and [c["command"] for c in got[0]["batch"]]
          == ["stop", "play", "deferred_play", "stop", "play", "deferred_play"]
          and "segment_size" not in got[0] and got[0]["segment"] is True, f"{got}")
    check("stream: a missing middle segment completes nothing",
          feed([uneven[0], uneven[2]]) == [])
    check("stream: a later segment with no first segment completes nothing",
          feed(uneven[1:]) == [])
    check("stream: a byte count that doesn't match cancels",
          feed([uneven[0], sx(0x7F, 1, 6, 0x53, 9, 0x01, 0x03, 0x01, 0x02), uneven[2]]) == [])
    stop_1, stop_2 = sx(0x7F, 1, 6, 0x01), sx(0x7F, 2, 6, 0x01)
    check("stream: a normal MMC message from the same device cancels",
          feed([uneven[0], stop_1] + uneven[1:]) == [])
    check("stream: other devices' messages and WAIT don't cancel",
          len(feed([uneven[0], stop_2, sx(0x7F, 1, 6, 0x7C)] + uneven[1:])) == 1)
    resp = [sx(0x7F, 1, 7, 0x64, 4, 0x41, 0x01, 0x60, 0x16),
            sx(0x7F, 1, 7, 0x64, 9, 0x00, 0x05, 0x2C, 0x00, 0x48, 0x03, 0x02, 0x7F, 0x01)]
    got = feed(resp)
    check("stream: RESPONSE SEGMENTs reassemble to time code + motion control tally",
          len(got) == 1 and got[0]["type"] == "mmc_response" and got[0]["segments"] == 2
          and [f.get("name") for f in got[0]["response"]["fields"]]
          == ["selected_time_code", "motion_control_tally"], f"{got}")


def check_track_bitmap(midi1) -> None:
    """RP-013 Standard Track Bitmap: byte 0 is video, reserved, time code,
    aux A, aux B, track 1, track 2; byte 1 is tracks 3-9."""
    got = midi1._decode_track_bitmap([0b1100101, 0b1000001, 0b0000001])
    check("track bitmap: byte 0 flags and tracks 1-2, byte 1 tracks 3-9, byte 2 from 10",
          got == {"video": True, "time_code_track": True, "aux_track_a": False,
                  "aux_track_b": False, "active_tracks": [1, 2, 3, 9, 10]}, f"{got}")


def check_mmc_response_examples(midi1) -> None:
    """RP-013's appendix responses, field by field."""
    master = json.loads(midi1._decode_mmc_response({"data": dict(MMC_RESPONSES)[
        "rp013 master: time code + motion control tally"]}))
    fields = master.get("fields", [])
    check("RP-013 appendix: master response has two fields",
          master.get("type") == "fields" and len(fields) == 2, f"{master}")
    if len(fields) == 2:
        tc, _tally = fields
        check("RP-013 appendix: SELECTED TIME CODE 00:22:05:12, 30 fps, status byte",
              (tc["name"], tc["hours"], tc["minutes"], tc["seconds"], tc["frames"],
               tc["frame_rate"], tc["use_status_byte"])
              == ("selected_time_code", 0, 22, 5, 12, "30nondrop", True), f"{tc}")
    short = json.loads(midi1._decode_mmc_response({"data": dict(MMC_RESPONSES)[
        "rp013 master: short selected time code"]}))
    check("RP-013 appendix: Short SELECTED TIME CODE frame 13",
          (short.get("name"), short.get("frames"), short.get("use_status_byte"))
          == ("short_selected_time_code", 13, True), f"{short}")
    check("RP-013 appendix: MOTION CONTROL TALLY 02 7F 01 is PLAY achieved, no process",
          len(fields) == 2 and {k: fields[1].get(k) for k in (
              "motion_state", "motion_state_success", "motion_process")}
          == {"motion_state": "play", "motion_state_success": "requested_motion_achieved",
              "motion_process": "none"}, f"{fields[1:] if fields else master}")
    sig = json.loads(midi1._decode_mmc_response({"data": dict(MMC_RESPONSES)["rp013 appendix signature"]}))
    listed_commands = {
        "extension", "stop", "play", "deferred_play", "fast_forward", "rewind", "record_strobe",
        "record_exit", "chase", "command_error_reset", "mmc_reset", "write", "read", "update",
        "locate", "variable_play", "move", "add", "subtract", "drop_frame_adjust", "procedure",
        "event", "group", "command_segment", "deferred_variable_play", "wait", "resume"}
    listed_fields = {
        "selected_time_code", "selected_master_code", "requested_offset", "actual_offset",
        "lock_deviation", "gp0", "gp1", "gp2", "gp3", "short_selected_time_code",
        "short_selected_master_code", "short_requested_offset", "short_actual_offset",
        "short_lock_deviation", "short_gp0", "short_gp1", "short_gp2", "short_gp3",
        "signature", "update_rate", "response_error", "command_error", "command_error_level",
        "time_standard", "motion_control_tally", "record_mode", "record_status",
        "control_disable", "resolved_play_mode", "chase_mode", "procedure_response",
        "event_response", "response_segment", "failure", "wait", "resume"}
    check("RP-013 appendix SIGNATURE: version 1.00, commands as listed",
          sig.get("version") == "1.00" and set(sig.get("commands", [])) == listed_commands,
          f"extra {set(sig.get('commands', [])) - listed_commands}, "
          f"missing {listed_commands - set(sig.get('commands', []))}")
    check("RP-013 appendix SIGNATURE: fields as listed",
          set(sig.get("fields", [])) == listed_fields,
          f"extra {set(sig.get('fields', [])) - listed_fields}, "
          f"missing {listed_fields - set(sig.get('fields', []))}")
    power_up = json.loads(midi1._decode_mmc_response({"data": dict(MMC_RESPONSES)[
        "command_error power-up state"]}))
    check("RP-013 p.53: COMMAND ERROR power-up state 04 00 00 7F 00 is 'no_errors'",
          (power_up.get("error"), power_up.get("error_halt"), power_up.get("level"))
          == ("no_errors", False, 0), f"{power_up}")
    slave = json.loads(midi1._decode_mmc_response({"data": dict(MMC_RESPONSES)[
        "rp013 slave: selected time code"]}))
    check("RP-013 appendix: slave SELECTED TIME CODE 10:01:58:28",
          (slave.get("hours"), slave.get("minutes"), slave.get("seconds"), slave.get("frames"))
          == (10, 1, 58, 28), f"{slave}")


def check_device_replies(midi1) -> None:
    for device, data, expected in DEVICE_REPLIES:
        got = midi1._decode_message(midi1.mido.Message("sysex", data=data))
        check(f"decode {device} Identity Reply", got == expected, f"got {got}")


# --- Schema consistency ---------------------------------------------------------

def check_schema(midi1) -> None:
    props = midi1.TOOLS[0]["input_schema"]["properties"]["message"]["properties"]
    accepted: set = set()
    for t in midi1._COMMANDS:
        msg = {"type": t, "command": "__bogus__", "device_id": 1,
               "command_format": "lighting", "tuning_program": 0, "value": 0}
        try:
            midi1._build_message(msg)
            check(f"{t} rejects an unknown command", False)
        except ValueError as e:
            found = re.search(r"must be one of (\[.*?\])", str(e))
            accepted |= set(json.loads(found.group(1).replace("'", '"')))
    enum = set(props["command"]["enum"])
    check("command enum == commands the code accepts",
          enum == accepted,
          f"not in enum: {sorted(accepted - enum)}; not accepted: {sorted(enum - accepted)}")
    no_builder = []
    for t in props["type"]["enum"]:
        try:
            midi1._build_message_sequence({"type": t})
        except Exception as e:
            if "unknown message type" in str(e):
                no_builder.append(t)
    check("every type in the schema has a builder", not no_builder, f"{no_builder}")

    mmc = set(midi1._COMMANDS["mmc"])
    no_data = set(midi1._MMC_NO_DATA_COMMANDS) | {"wait", "resume"}
    with_data = set(midi1._MMC_DATA_BUILDERS)
    check("mmc: every command is either no-data or has a data builder",
          mmc == no_data | with_data and not (no_data & with_data),
          f"neither: {sorted(mmc - no_data - with_data)}; "
          f"both: {sorted(no_data & with_data)}; "
          f"unknown builders: {sorted(with_data - mmc)}")

    msc = set(midi1._COMMANDS["msc"])
    no_data = set(midi1._MSC_NO_DATA_COMMANDS)
    with_data = set(midi1._MSC_DATA_BUILDERS)
    check("msc: every command is either no-data or has a data builder",
          msc == no_data | with_data and not (no_data & with_data),
          f"neither: {sorted(msc - no_data - with_data)}; "
          f"both: {sorted(no_data & with_data)}; "
          f"unknown builders: {sorted(with_data - msc)}")


# --- poll wait logic (fake handle, no port) -------------------------------------

class _FakePort:
    def close(self) -> None:
        pass


def check_describe(midi1) -> None:
    """describe's docs (_DOCS) match the code: types and commands exist,
    every example builds and decodes back to the same bytes, and every field
    an example uses is documented."""
    docs = midi1._DOCS
    check("describe: every documented type is a message type",
          set(docs) <= set(midi1._MESSAGE_TYPES), f"{sorted(set(docs) - set(midi1._MESSAGE_TYPES))}")
    wrong_commands = [t for t, d in docs.items() if "commands" in d
                      and set(d["commands"]) != set(midi1._COMMANDS[t])]
    check("describe: a documented type with commands documents each one",
          not wrong_commands, f"{wrong_commands}")

    examples = []
    for t, d in docs.items():
        for command, entry in d.get("commands", {None: d}).items():
            examples.append((t, command, {**d["fields"], **entry["fields"]}, entry["example"]))
    undocumented, mismatched = [], []
    for t, command, fields, example in examples:
        extra = set(example) - {"type", "command"} - set(fields)
        if extra or example.get("type") != t or example.get("command") != command:
            undocumented.append(f"{t}/{command}: {sorted(extra)}")
        try:
            wire = midi1._build_message_sequence(dict(example))
            if len(wire) == 1:
                rebuilt = [midi1._build_message(dict(midi1._decode_message(wire[0])))]
            else:
                stream = midi1._StreamDecoder()
                completed = [c for c in (stream.feed(m) for m in wire) if c is not None]
                rebuilt = midi1._build_message_sequence(dict(completed[-1]))
            if [m.bytes() for m in rebuilt] != [m.bytes() for m in wire]:
                mismatched.append(f"{t}/{command}")
        except Exception as e:  # reported as a failure
            mismatched.append(f"{t}/{command}: {type(e).__name__}: {e}")
    check(f"describe: all {len(examples)} examples use only documented fields",
          not undocumented, f"{undocumented[:5]}")
    check(f"describe: all {len(examples)} examples build and decode to the same bytes",
          not mismatched, f"{mismatched[:5]}")

    def describe(**message):
        return json.loads(midi1._run({"action": "describe", **({"message": message} if message else {})}))

    listing = describe()
    check("describe: no type lists every message type",
          list(listing.get("types", {})) == list(midi1._MESSAGE_TYPES), f"{listing}"[:200])
    one = describe(type="channel_mode", command="mono_on")
    check("describe: a command merges type and command fields, with its example",
          set(one.get("fields", {})) == {"channel", "channel_count"}
          and one.get("example", {}).get("command") == "mono_on", f"{one}")
    check("describe: unknown type and unknown command are errors",
          "error" in describe(type="nope") and "error" in describe(type="channel_mode", command="nope"))
    field_names = set(midi1._INFO_FIELD_NAMES) | set(midi1._MMC_RESPONSE_ONLY_NAMES)
    check("describe: every MMC Information Field and response-only name is documented",
          field_names == set(midi1._MMC_FIELD_SUMMARIES),
          f"missing {sorted(field_names - set(midi1._MMC_FIELD_SUMMARIES))}, "
          f"extra {sorted(set(midi1._MMC_FIELD_SUMMARIES) - field_names)}")
    write_failures, access_wrong = [], []
    for name in sorted(field_names):
        entry = midi1._mmc_field_doc(name)
        codec = midi1._MMC_FIELD_CODECS.get(name)
        writeable = name in midi1._WRITEABLE_INFO_FIELDS or bool(codec and codec[0])
        if (entry["access"] != "read only") != writeable or (
                (entry["access"] == "write and masked_write")
                != (name in midi1._MASK_WRITEABLE_INFO_FIELDS)):
            access_wrong.append(name)
        if "write_example" not in entry:
            continue
        message = {"type": "mmc", "command": "write", "fields": [entry["write_example"]]}
        try:
            wire = midi1._build_message(dict(message))
            if midi1._build_message(dict(midi1._decode_message(wire))).bytes() != wire.bytes():
                write_failures.append(f"{name}: rebuild differs")
        except Exception as e:  # reported as a failure
            write_failures.append(f"{name}: {type(e).__name__}: {e}")
    check("describe: field access (write, masked_write, read only) matches the code",
          not access_wrong, f"{access_wrong}")
    check("describe: every field's WRITE example builds and decodes to the same bytes",
          not write_failures, f"{write_failures[:5]}")
    short = describe(type="mmc", field="short_gp0")
    check("describe: a field lookup returns its access and data",
          short.get("access") == "read only" and "data" in short
          and "error" in describe(type="mmc", field="nope"), f"{short}")

    missing = [t for t in midi1._MESSAGE_TYPES if t not in docs]
    check(f"describe: all {len(midi1._MESSAGE_TYPES)} message types are documented",
          not missing, f"{missing}")


# Python's own wording for a type mistake that reached an operation unchecked.
PYTHON_TYPE_WORDING = (
    "not supported between", "unsupported operand", "object is not", "has no len",
    "expected string", "object has no attribute", "can't", "cannot", "argument",
    "must be real number", "invalid literal", "index", "unhashable",
    "object cannot be interpreted", "bytes must be in range", "ord()",
)


def check_wrong_types() -> None:
    """Every field of every valid case (and of the first entry of a list of
    objects) set to a wrong-typed value gives midi1's own KeyError,
    ValueError or TypeError, never Python's."""
    from core import midi1

    bad_values = ["1", 1.5, [1], None, True, {}]
    raw, count = [], 0
    for name, msg in CASES:
        if name.startswith("err:"):
            continue
        variants = []
        for key, value in msg.items():
            if key == "type":
                continue
            variants += [(f"{key}={b!r}", {**msg, key: b}) for b in bad_values]
            if isinstance(value, list) and value and isinstance(value[0], dict):
                variants += [(f"{key}[0].{k}={b!r}", {**msg, key: [{**value[0], k: b}, *value[1:]]})
                             for k in value[0] for b in bad_values]
        for label, bad in variants:
            count += 1
            try:
                midi1._build_message_sequence(dict(bad))
            except (KeyError, ValueError, TypeError) as e:
                if isinstance(e, KeyError) or not any(w in str(e) for w in PYTHON_TYPE_WORDING):
                    continue
                raw.append(f"{name} {label}: {type(e).__name__}: {e}")
            except Exception as e:  # any other exception type is a raw error
                raw.append(f"{name} {label}: {type(e).__name__}: {e}")
    check(f"wrong types: all {count} wrong-typed variants get midi1's own error",
          not raw, "; ".join(raw[:3]))


def check_rawmidi_proc(midi1) -> None:
    """_rawmidi_output's port numbering and _rawmidi_avail's parsing, on
    made-up /proc/asound files (a card with two rawmidi devices)."""
    import glob as glob_module
    import tempfile

    real_glob = glob_module.glob
    with tempfile.TemporaryDirectory() as root:
        card = f"{root}/card9"
        os.makedirs(card)
        with open(f"{card}/midi0", "w") as f:
            f.write("X\n\nType: Legacy\nOutput 0\n  Tx bytes     : 5\nOutput 1\n  Tx bytes     : 0\n"
                    "Input 0\n  Rx bytes     : 0\n")
        with open(f"{card}/midi1", "w") as f:
            f.write("X\n\nType: Legacy\nOutput 0\n  Tx bytes     : 9\n  Owner PID    : 1\n"
                    "  Mode         : native\n  Buffer size  : 4096\n  Avail        : 1234\n"
                    "Input 0\n  Rx bytes     : 0\nInput 1\n  Rx bytes     : 0\n")
        glob_module.glob = lambda pattern: real_glob(pattern.replace("/proc/asound", root))
        try:
            mapping = [midi1._rawmidi_output(9, port) for port in range(5)]
        finally:
            glob_module.glob = real_glob
        check("rawmidi: ports number across devices, max(outputs, inputs) each",
              mapping == [(f"{card}/midi0", 0), (f"{card}/midi0", 1), (f"{card}/midi1", 0),
                          None, None], f"{mapping}")
        check("rawmidi: Avail is read from an open output, None from a closed one",
              midi1._rawmidi_avail(f"{card}/midi1", 0) == 1234
              and midi1._rawmidi_avail(f"{card}/midi0", 0) is None)


def _fake_input(midi1, name: str):
    buf, event = deque(maxlen=10), threading.Event()
    midi1._OPEN_PORTS[name] = ("input", _FakePort())
    midi1._INPUT_BUFFERS[name] = buf
    midi1._INPUT_EVENTS[name] = event
    return buf, event


def _timed_poll(midi1, name: str, timeout: float):
    t0 = time.monotonic()
    result = json.loads(midi1._poll({"handle": name, "timeout_seconds": timeout}))
    return time.monotonic() - t0, result["messages"]


def check_poll_wait(midi1) -> None:
    buf, event = _fake_input(midi1, "fake-stale")
    event.set()
    elapsed, msgs = _timed_poll(midi1, "fake-stale", 0.5)
    check("poll: leftover event doesn't end the wait early",
          elapsed >= 0.45 and msgs == [], f"{elapsed:.3f}s")

    buf, event = _fake_input(midi1, "fake-arrive")

    def deliver() -> None:
        time.sleep(0.2)
        buf.append((time.time(), midi1.mido.Message("clock"), False))
        event.set()

    threading.Thread(target=deliver).start()
    elapsed, msgs = _timed_poll(midi1, "fake-arrive", 2.0)
    check("poll: message arriving mid-wait returns promptly",
          0.15 <= elapsed < 1.0 and len(msgs) == 1, f"{elapsed:.3f}s, {len(msgs)} msgs")

    buf, event = _fake_input(midi1, "fake-ready")
    buf.append((time.time(), midi1.mido.Message("clock"), False))
    elapsed, msgs = _timed_poll(midi1, "fake-ready", 2.0)
    check("poll: buffered message returns without waiting",
          elapsed < 0.1 and len(msgs) == 1, f"{elapsed:.3f}s")

    _fake_input(midi1, "fake-zero")
    elapsed, msgs = _timed_poll(midi1, "fake-zero", 0)
    check("poll: timeout 0 returns at once", elapsed < 0.1 and msgs == [], f"{elapsed:.3f}s")

    buf, event = _fake_input(midi1, "fake-decode")
    midi1._STREAM_DECODERS["fake-decode"] = midi1._StreamDecoder()
    M = midi1.mido.Message
    for m in (M("sysex", data=DEVICE_REPLIES[0][1]),
              M("control_change", control=99, value=0x24),
              M("control_change", control=98, value=0x34),
              M("control_change", control=6, value=0x40),
              M("control_change", control=38, value=0x05)):
        buf.append((time.time(), m, False))
    _, msgs = _timed_poll(midi1, "fake-decode", 0)
    check("poll: entries carry message, hex and decoded",
          all({"received_at", "message", "hex", "decoded"} <= set(m) for m in msgs)
          and msgs[0]["decoded"] == DEVICE_REPLIES[0][2]
          and msgs[0]["hex"] == "F0 7E 10 06 02 41 45 03 00 00 00 03 00 00 F7",
          f"{msgs[:1]}")
    check("poll: NRPN data entries carry the completed change",
          [m.get("completes", {}).get("value") for m in msgs]
          == [None, None, None, 0x40, (0x40 << 7) | 0x05], f"{[m.get('completes') for m in msgs]}")

    buf, event = _fake_input(midi1, "fake-overflow")
    midi1._STREAM_DECODERS["fake-overflow"] = midi1._StreamDecoder()
    for m in (M("control_change", control=99, value=1), M("control_change", control=98, value=2),
              None, M("control_change", control=6, value=9)):
        buf.append((time.time(), m, False))
    _, msgs = _timed_poll(midi1, "fake-overflow", 0)
    check("poll: an overflow is an entry in order, and partial NRPN state starts over",
          [m.get("overflow", False) for m in msgs] == [False, False, True, False]
          and "completes" not in msgs[3], f"{msgs}")

    buf, event = _fake_input(midi1, "fake-at-open")
    for m, at_open in ((M("note_on", note=60), True), (None, True), (M("note_off", note=60), False)):
        buf.append((time.time(), m, at_open))
    _, msgs = _timed_poll(midi1, "fake-at-open", 0)
    check("poll: 'at_open' appears on burst entries only, overflow entries included",
          [m.get("at_open", False) for m in msgs] == [True, True, False]
          and msgs[1].get("overflow") is True, f"{msgs}")

    burst = midi1._OpenBurst(100.0)
    got = [burst.member(t) for t in (100.0002, 100.0015, 100.019, 100.05, 100.0505)]
    check("_OpenBurst: arrivals within 20 ms of the open or the previous one; the first gap ends it",
          got == [True, True, True, False, False], f"{got}")
    late = midi1._OpenBurst(100.0)
    check("_OpenBurst: a first message over 20 ms after the open isn't in a burst",
          [late.member(t) for t in (100.03, 100.031)] == [False, False])

    for name in ("fake-stale", "fake-arrive", "fake-ready", "fake-zero", "fake-decode",
                 "fake-overflow", "fake-at-open"):
        midi1._close({"handle": name})


# --- Live loopback (only when a loopback port exists) ---------------------------

# Port names that loop output back to input, by platform.
LOOPBACK_NAMES = ("Midi Through", "IAC Driver", "loopMIDI")

def check_live_loopback(midi1) -> None:
    def call(**kw) -> dict:
        return json.loads(asyncio.run(midi1.execute("midi1", kw)))

    devices = call(action="list_devices")
    out_port = next((p for p in devices.get("outputs", [])
                     if any(n in p for n in LOOPBACK_NAMES)), None)
    in_port = next((p for p in devices.get("inputs", [])
                    if any(n in p for n in LOOPBACK_NAMES)), None)
    if not (out_port and in_port):
        print(f"  skip  live loopback (no port named like {LOOPBACK_NAMES})")
        return
    i = call(action="open", port_name=in_port, direction="input")
    o = call(action="open", port_name=out_port, direction="output")
    # Past the 20 ms in which arrivals count as the burst at open.
    time.sleep(2 * midi1._AT_OPEN_GAP)
    try:
        sent = [
            {"type": "note_on", "note": 60, "velocity": 64},
            {"type": "sysex", "data": [1, 2, 3]},
            {"type": "rpn", "parameter": "pitch_bend_sensitivity", "value": 2, "msb_only": True},
        ]
        expected = []
        for m in sent:
            r = call(action="send", handle=o["handle"], message=m)
            expected += r["sent"] if isinstance(r["sent"], list) else [r["sent"]]
        got = call(action="poll", handle=i["handle"], timeout_seconds=2)
        received = [m["message"] for m in got["messages"]]
        check("live loopback: everything sent comes back", received == expected,
              f"sent {expected}, got {received}")
        check("live loopback: open returns 'opened_at'; messages arriving after the "
              "burst window aren't 'at_open'",
              isinstance(i.get("opened_at"), float)
              and not any(m.get("at_open") for m in got["messages"]), f"{i}, {got['messages'][:2]}")
        t0 = time.monotonic()
        empty = call(action="poll", handle=i["handle"], timeout_seconds=1)
        elapsed = time.monotonic() - t0
        check("live loopback: empty poll waits its timeout",
              empty["messages"] == [] and elapsed >= 0.95, f"{elapsed:.3f}s")

        # Active Sensing: dropped by default, passed with 'active_sensing': true.
        sensing = call(action="open", port_name=in_port, direction="input", active_sensing=True)
        try:
            call(action="send", handle=o["handle"], message={"type": "active_sensing"})
            call(action="send", handle=o["handle"], message={"type": "note_off", "note": 60})
            plain = call(action="poll", handle=i["handle"], timeout_seconds=1)
            with_as = call(action="poll", handle=sensing["handle"], timeout_seconds=1)
        finally:
            call(action="close", handle=sensing["handle"])
        check("live loopback: Active Sensing dropped on a default input",
              [m["decoded"]["type"] for m in plain["messages"]] == ["note_off"],
              f"{plain}")
        check("live loopback: Active Sensing received with 'active_sensing': true",
              [m["decoded"] for m in with_as["messages"]][:1] == [{"type": "active_sensing"}]
              and len(with_as["messages"]) == 2, f"{with_as}")
    finally:
        call(action="close", handle=o["handle"])
        call(action="close", handle=i["handle"])

    check_live_bursts(midi1, call, in_port, out_port)

    for kw, name in (({"direction": "output", "active_sensing": True}, "on an output"),
                     ({"direction": "input", "active_sensing": "yes"}, "not a boolean"),
                     ({"direction": "input", "port_name": "no such port"}, "unknown port")):
        r = call(action="open", **{"port_name": in_port, **kw})
        check(f"open: error for active_sensing / port ({name})", "error" in r, f"{r}")


def check_live_bursts(midi1, call, in_port: str, out_port: str) -> None:
    """Bursts through the loopback, sent straight from the output port object
    (one 'send' per tool call is too slow to make a burst). With the "alsa"
    backend these are checks. With "rtmidi" (macOS, Windows) the large ones
    are reported as measurements: how that platform handles them is what
    they find out."""
    M = midi1.mido.Message
    alsa = midi1._PORT_BACKEND == "alsa"

    def measure_or_check(name, condition, detail):
        if alsa:
            check(name, condition, detail)
        else:
            print(f"  info  [{midi1._PORT_BACKEND}] {name}: "
                  f"{'yes' if condition else 'NO'} ({detail})")

    def burst(n: int):
        i = call(action="open", port_name=in_port, direction="input")
        o = call(action="open", port_name=out_port, direction="output")
        out = midi1._OPEN_PORTS[o["handle"]][1]
        sent = [M("polytouch", note=k % 128, value=(k // 128) % 128) for k in range(n)]
        # _AlsaOutput retries while the receiver's queue is full; an error
        # here means it gave up. Count what went out.
        sent_ok = 0
        for m in sent:
            try:
                out.send(m)
            except OSError:
                break
            sent_ok += 1
        time.sleep(1.0)
        pools = ""
        if alsa:
            with open("/proc/asound/seq/clients") as f:
                pools = f.read()
        got = call(action="poll", handle=i["handle"])["messages"]
        call(action="close", handle=o["handle"])
        call(action="close", handle=i["handle"])
        return sent[:sent_ok], got, pools

    sent, got, pools = burst(1500)
    if alsa:
        check("live burst: the input client's kernel pool is 2000 events",
              re.search(r'"midi1" \[User Legacy\].*?Input pool :\s+Pool size\s+:\s+2000', pools,
                        re.DOTALL) is not None)
    measure_or_check("live burst: 1500 events in a burst all arrive, in order",
          [m["hex"] for m in got] == [m.hex() for m in sent], f"{len(got)} of {len(sent)}")

    sent, got, _ = burst(6000)
    measure_or_check("live burst: 6000 events all arrive, in order, with no overflow",
          len(sent) == 6000 and [m["hex"] for m in got] == [m.hex() for m in sent],
          f"{len(got)} entries for {len(sent)} sent, "
          f"overflow {any(m.get('overflow') for m in got)}")

    # 16,354 data bytes was the first size rtmidi's output couldn't send.
    for size in (8000, 16354, 100000):
        i = call(action="open", port_name=in_port, direction="input")
        o = call(action="open", port_name=out_port, direction="output")
        data = [k % 128 for k in range(size)]
        try:
            sent_result = call(action="send", handle=o["handle"],
                               message={"type": "sysex", "data": data})
            got = call(action="poll", handle=i["handle"], timeout_seconds=2)["messages"]
            time.sleep(0.2)
            got += call(action="poll", handle=i["handle"])["messages"]
        finally:
            call(action="close", handle=o["handle"])
            call(action="close", handle=i["handle"])
        measure_or_check(f"live burst: a {size:,}-byte SysEx is sent and arrives whole",
              "error" not in sent_result and len(got) == 1
              and got[0]["decoded"] == {"type": "sysex", "data": data},
              f"{sent_result.get('error')}; "
              f"{[(m.get('decoded', {}).get('type'), len(m.get('decoded', {}).get('data', []))) for m in got]}")


def main() -> int:
    from core import midi1

    if midi1.mido is None:
        print("mido is not installed; nothing to test")
        return 1

    current = build_snapshot(midi1)
    if "--record" in sys.argv[1:]:
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(current, f, indent=1, sort_keys=True)
            f.write("\n")
        counts = {k: len(v) for k, v in current.items()}
        print(f"recorded {SNAPSHOT_PATH}: {counts}")

    print("\ncases")
    check_case_expectations(current)

    print("\nsnapshot")
    if not os.path.exists(SNAPSHOT_PATH):
        check("snapshot file exists (run with --record)", False, SNAPSHOT_PATH)
    else:
        with open(SNAPSHOT_PATH) as f:
            compare_snapshot(current, json.load(f))

    print("\nspec examples")
    check_spec_examples(midi1)

    print("\ndecoding")
    check_round_trip(midi1)
    check_decode_edges(midi1)
    check_msc_checksum(midi1)
    check_stream_decoder(midi1)
    check_track_bitmap(midi1)
    check_mmc_response_examples(midi1)
    check_device_replies(midi1)

    print("\nschema")
    check_schema(midi1)
    check_describe(midi1)
    check_wrong_types()
    check_rawmidi_proc(midi1)

    print("\npoll wait")
    check_poll_wait(midi1)

    print("\nlive")
    check_live_loopback(midi1)

    total = len(FAILURES)
    print(f"\n{'FAILED' if total else 'all checks passed'}"
          + (f": {total} failure(s)" if total else ""))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
