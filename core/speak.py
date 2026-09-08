"""Speak text aloud through your own local text-to-speech engine (Piper) and
play it through your configured audio output.

Windows port of the Linux `speak.py`. Same motivation and shape as
`vision.py`/`text_embeddings.py`: self-hosted, config-driven, declared to
Claude either way so a fresh clone doesn't need a code change to gain the
capability once configured. It does nothing until `config.toml` has
`[speak]` set up — see that block for what's required.

Two independent reasons this can decline to actually speak, both returned as
a `status` field rather than raised, matching `vision.py`'s pattern:
  - `"disabled"`   — `[speak].enabled` is explicitly false. Checked BEFORE
                      anything else, so a disabled tool never touches the
                      filesystem or opens an audio device. This is a
                      genuinely different switch from the one below: it can
                      be flipped even when a voice model is fully configured
                      and working, e.g. to mute output for a while without
                      losing/unsetting `voice_model`.
  - `"not_configured"` — `[speak].voice_model` is unset, or the file it
                      points at doesn't exist. Same "not wired up yet,
                      here's what to do about it" shape `vision_query` uses
                      for a missing server URL.

Settings are re-read from config.toml on every call (not cached at import),
so editing `enabled`/`voice_model`/`sink`/`timeout` takes effect on the very
next call, no restart needed — same as every other config-driven tool here.

Real asymmetry from the Linux version, worth knowing before touching this
file: Linux's `speak.py` shells out to TWO subprocesses (`python3 -m piper`,
then `paplay`) because Piper only exposes a CLI entry point there. The
`piper-tts` PyPI package also ships a real Python API — `piper.PiperVoice` —
so this Windows port synthesizes IN-PROCESS (`PiperVoice.load(...)
.synthesize_wav(...)`) rather than shelling out at all, and plays the
result back with `sounddevice`/`soundfile` instead of `paplay` (which
doesn't exist on Windows; PipeWire/PulseAudio sink names have no Windows
equivalent either). `sink`, if set, is a `sounddevice` output device index
or a substring of its name (see `python -m sounddevice` to list devices) —
optional, since Windows already has a perfectly good default output device
without any configuration.

Requires:  pip install piper-tts sounddevice soundfile
           A Piper voice model (.onnx + matching .onnx.json sidecar) — the
           sidecar is expected at `<voice_model path>.json`, same convention
           as upstream Piper and the Linux version of this tool.
"""

import asyncio
import json
import os
import tempfile
import threading
import tomllib
import wave
from pathlib import Path

TOOLS = [
    {
        "name": "speak",
        "description": (
            "Speak text aloud through your own local text-to-speech engine "
            "(Piper) and play it through your configured audio output. "
            "Configured under [speak] in config.toml. If speak is disabled "
            "in config, or no voice model is configured/installed, this "
            "returns a {'status': 'disabled' | 'not_configured', ...} "
            "result and does NOT fall back to any other TTS or to text-only "
            "output — tell the user what happened and wait for guidance "
            "rather than silently trying something else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to speak aloud.",
                },
                "voice": {
                    "type": "string",
                    "description": (
                        "Optional path to a different Piper .onnx voice "
                        "model for this call, overriding config.toml's "
                        "[speak].voice_model. Must have a matching "
                        "<path>.json sidecar file, same as the default."
                    ),
                },
            },
            "required": ["text"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# core/speak.py -> parent is core/, parent.parent is the repo root, same
# resolution vision.py/text_embeddings.py use for their config path.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "speak":
        return json.dumps({"error": f"unknown speak tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f).get("speak", {})
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        # Surfaced through _run's return, not raised, so a syntax error
        # while hand-editing config.toml shows up as a normal tool error
        # rather than an unhandled exception in the chat loop.
        raise ValueError(f"config.toml is not valid TOML: {e}") from e


def _run(tool_input: dict) -> str:
    try:
        config = _load_config()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    text = tool_input.get("text")
    if not text:
        return json.dumps({"error": "'text' is required"})

    # 1. `enabled` first, before touching anything else — a disabled tool
    #    should have zero filesystem/audio-device side effects.
    if not config.get("enabled", True):
        return json.dumps(
            {
                "status": "disabled",
                "reason": "speak is disabled — set [speak].enabled = true "
                "in config.toml to turn it back on",
            }
        )

    # 2. Voice model configured and actually present on disk.
    voice_model = tool_input.get("voice") or config.get("voice_model")
    if not voice_model:
        return json.dumps(
            {
                "status": "not_configured",
                "reason": (
                    "no voice model configured — set [speak].voice_model "
                    "in config.toml to a Piper .onnx voice file"
                ),
            }
        )
    voice_path = Path(voice_model).expanduser()
    if not voice_path.is_file():
        return json.dumps(
            {
                "status": "not_configured",
                "reason": f"voice model not found: {voice_path}",
            }
        )
    sidecar_path = Path(str(voice_path) + ".json")
    if not sidecar_path.is_file():
        return json.dumps(
            {
                "status": "not_configured",
                "reason": f"voice config sidecar not found: {sidecar_path}",
            }
        )

    timeout = float(config.get("timeout", 30))
    sink = config.get("sink")  # sounddevice output device index or name substring

    try:
        from piper import PiperVoice
    except ImportError:
        return json.dumps(
            {
                "error": "piper-tts is not installed — `pip install "
                "piper-tts` to enable the speak tool"
            }
        )
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError:
        return json.dumps(
            {
                "error": "sounddevice/soundfile are not installed — "
                "`pip install sounddevice soundfile` to enable playback "
                "for the speak tool"
            }
        )

    # 3. Synthesize to a temp WAV file, in-process via piper.PiperVoice —
    # no subprocess involved (see module docstring for why this differs
    # from the Linux version).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        try:
            voice = PiperVoice.load(str(voice_path))
            with wave.open(wav_path, "wb") as wav_file:
                voice.synthesize_wav(text, wav_file)
        except Exception as e:
            return json.dumps(
                {"status": "error", "reason": f"synthesis failed: {e}"}
            )

        # 4. Play it — block until playback finishes, since a "speak" tool
        # call is expected to have actually finished speaking before the
        # tool result returns to the conversation. A watchdog timer stands
        # in for the subprocess `timeout=` the Linux version relies on:
        # sd.wait() alone has no time limit of its own.
        try:
            data, samplerate = sf.read(wav_path, dtype="float32")
        except Exception as e:
            return json.dumps(
                {"status": "error", "reason": f"could not read synthesized audio: {e}"}
            )

        watchdog = threading.Timer(timeout, sd.stop)
        watchdog.start()
        try:
            sd.play(data, samplerate, device=sink)
            sd.wait()
        except Exception as e:
            return json.dumps(
                {"status": "error", "reason": f"playback failed: {e}"}
            )
        finally:
            watchdog.cancel()
    finally:
        # Not `trash` — this is a synthesized scratch file, not user data,
        # so an ordinary remove (rather than the recoverable-trash
        # convention used for user-facing deletes elsewhere) is appropriate.
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return json.dumps({"status": "ok", "text": text})
