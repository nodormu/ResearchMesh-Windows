"""Record audio from your own configured microphone for a bounded window and
transcribe it locally via faster-whisper (CPU, no cloud STT).

Windows port of the Linux `listen.py`. Same motivation and shape as
`speak.py`/`vision.py`/`text_embeddings.py`: self-hosted, config-driven,
declared to Claude either way so a fresh clone doesn't need a code change to
gain the capability once configured. It does nothing until `config.toml` has
`[listen]` set up (or, on Windows, works out of the box against the system's
default microphone if one exists — see below).

Two independent reasons this can decline to record, both returned as a
`status` field rather than raised, matching `speak.py`'s pattern:
  - `"disabled"`       — `[listen].enabled` is explicitly false. Checked
                          BEFORE `device`, so a disabled tool never opens
                          the microphone at all. This is a genuinely
                          different switch from the one below: it can be
                          flipped even when a device is fully configured
                          and working — e.g. to guarantee the mic stays
                          closed for a while without losing/unsetting
                          `device`.
  - `"not_configured"` — either an explicitly requested `device` (from the
                          tool call or `[listen].device`) doesn't match any
                          real input device, OR no device was requested and
                          Windows itself has no default input device at
                          all. Same "not wired up yet, here's what to do
                          about it" shape `vision_query`/`speak` use for a
                          missing server URL / voice model.

Settings are re-read from config.toml on every call (not cached at import),
so editing `enabled`/`device`/`model_size`/durations takes effect on the
very next call, no restart needed — same as every other config-driven tool
here.

Real asymmetry from the Linux version, worth knowing before touching this
file: Linux's `listen.py` shells out to `timeout <N> parecord ...` because
capture there goes through a named PipeWire source and a CLI recorder.
Windows has no PipeWire/PulseAudio and no `parecord`/`timeout` — capture
here goes through `sounddevice.rec(...)` instead, entirely in-process, with
`soundfile` writing the result to a temp WAV. There is also no PipeWire-style
requirement to name a source up front: `sounddevice` already has a sensible
notion of a "default input device" that normally just works, so `device` is
OPTIONAL here (unlike the Linux version, where it's mandatory) — set it only
to pin a specific microphone (see `python -m sounddevice` to list devices,
or pass a numeric index or a substring of a device's name).

`faster-whisper` has no CLI entry point either way — it's a Python library,
used via `from faster_whisper import WhisperModel` directly IN-PROCESS, same
as the Linux version.

Requires:  pip install faster-whisper sounddevice soundfile
"""

import asyncio
import json
import os
import tempfile
import tomllib
from pathlib import Path

TOOLS = [
    {
        "name": "listen",
        "description": (
            "Record audio from your own configured microphone for a "
            "bounded window and transcribe it locally via faster-whisper "
            "(CPU, no cloud STT). Configured under [listen] in "
            "config.toml. If listen is disabled in config, or no "
            "microphone device is available/configured, this returns a "
            "{'status': 'disabled' | 'not_configured', ...} result — tell "
            "the user what happened rather than assuming a transcript "
            "exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "integer",
                    "description": (
                        "How many seconds to record. Defaults to "
                        "[listen].default_duration_seconds if unset "
                        "(typically 8-10s). Clamped to "
                        "[listen].max_duration_seconds as a safety cap "
                        "regardless of what's requested here."
                    ),
                },
                "device": {
                    "type": "string",
                    "description": (
                        "Optional sounddevice input device to override "
                        "[listen].device for this call — a numeric index "
                        "(as a string) or a substring of the device's "
                        "name. If omitted, the system default microphone "
                        "is used."
                    ),
                },
            },
            "required": [],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# core/listen.py -> parent is core/, parent.parent is the repo root, same
# resolution speak.py/vision.py use for their config path.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "listen":
        return json.dumps({"error": f"unknown listen tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("listen", {})
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"config.toml is not valid TOML: {e}") from e


def _coerce_device(device: str):
    """A sounddevice device may be a numeric index or a name substring —
    both are valid values for its `device=` kwarg, but only a numeric
    string should be turned into an int; a name substring must stay a str.
    """
    try:
        return int(device)
    except ValueError:
        return device


def _run(tool_input: dict) -> str:
    try:
        config = _load_config()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # 1. `enabled` first, before touching anything else — a disabled tool
    #    should never open the microphone at all.
    if not config.get("enabled", True):
        return json.dumps(
            {
                "status": "disabled",
                "reason": "listen is disabled — set [listen].enabled = "
                "true in config.toml to turn it back on",
            }
        )

    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError:
        return json.dumps(
            {
                "error": "sounddevice/soundfile are not installed — "
                "`pip install sounddevice soundfile` to enable the "
                "listen tool"
            }
        )

    # 2. Resolve the input device. Unlike the Linux version, this is
    # OPTIONAL — sounddevice's own default input device is used if none is
    # requested, and only reported as not_configured if that default
    # doesn't actually exist (no microphone at all) or an explicitly
    # requested device doesn't match anything real.
    raw_device = tool_input.get("device") or config.get("device")
    device = None
    if raw_device:
        device = _coerce_device(str(raw_device))
        try:
            sd.query_devices(device, "input")
        except Exception:
            return json.dumps(
                {
                    "status": "not_configured",
                    "reason": (
                        f"microphone device {raw_device!r} not found — "
                        "run `python -m sounddevice` to list available "
                        "devices, or unset [listen].device to use the "
                        "system default microphone"
                    ),
                }
            )
    else:
        try:
            default_input = sd.default.device[0]
        except Exception:
            default_input = -1
        if default_input is None or default_input < 0:
            return json.dumps(
                {
                    "status": "not_configured",
                    "reason": (
                        "no default microphone device found — set "
                        "[listen].device in config.toml (run `python -m "
                        "sounddevice` to list available devices)"
                    ),
                }
            )

    # 3. Resolve + clamp duration.
    duration = tool_input.get("duration_seconds") or config.get(
        "default_duration_seconds", 8
    )
    max_duration = config.get("max_duration_seconds", 30)
    duration = max(1, min(int(duration), int(max_duration)))

    model_size = config.get("model_size", "base")
    samplerate = int(config.get("samplerate", 16000))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        # 4. Capture, entirely in-process — no `timeout`/`parecord`
        # subprocess to shell out to on Windows (see module docstring).
        try:
            recording = sd.rec(
                int(duration * samplerate),
                samplerate=samplerate,
                channels=1,
                dtype="float32",
                device=device,
            )
            sd.wait()
        except Exception as e:
            return json.dumps(
                {"status": "error", "reason": f"capture failed: {e}"}
            )

        try:
            sf.write(wav_path, recording, samplerate)
        except Exception as e:
            return json.dumps(
                {"status": "error", "reason": f"could not write capture to disk: {e}"}
            )

        # 5. Transcribe in-process (no CLI entry point for faster-whisper,
        # same as the Linux version — see module docstring).
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return json.dumps(
                {
                    "error": "faster-whisper is not installed — "
                    "`pip install faster-whisper` to enable the listen "
                    "tool"
                }
            )

        try:
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            segments, info = model.transcribe(wav_path)
            transcript = " ".join(seg.text.strip() for seg in segments)
        except Exception as e:
            return json.dumps(
                {"status": "error", "reason": f"transcription failed: {e}"}
            )
    finally:
        # Not `trash` — this is a scratch recording, not user data the
        # trash convention is meant for, same reasoning as speak.py's own
        # temp WAV cleanup.
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return json.dumps(
        {
            "status": "ok",
            "transcript": transcript,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration_seconds": duration,
        }
    )
