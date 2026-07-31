"""Wall-Eye voice output.

Three voices, picked by the "voice" key of the config passed to speak():

    off     - no-op.
    system  - any installed pyttsx3/SAPI voice. Optional keys:
                voice_name: substring matched against installed voice names
                rate:       words per minute
    wall-e  - a fun robotic voice: the system TTS is rendered to a wav file,
              then post-processed with numpy (pitch shift, ring modulation,
              soft clip, normalize) and played back.

The DSP steps are pure numpy functions so they can be unit-tested without
any audio hardware. pyttsx3 is imported lazily inside the functions that
need it, so importing this module never requires audio libraries.

speak() is non-blocking: synthesis and playback run on a worker thread,
one request at a time. A request that arrives while another is still
speaking is dropped rather than queued, so alerts never pile up.
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import wave

import numpy as np

log = logging.getLogger("walleye")

VOICE_OFF = "off"
VOICE_SYSTEM = "system"
VOICE_WALL_E = "wall-e"

DEFAULT_RATE_WPM = 180

# Tuning for the "wall-e" robot voice.
WALL_E_PITCH_FACTOR = 1.3    # resample factor; >1 raises the pitch
WALL_E_RING_FREQ_HZ = 35.0   # tremolo/ring carrier - low enough to buzz, not warble
WALL_E_RING_DEPTH = 0.35     # 0 = no effect, 1 = full amplitude swing
WALL_E_CLIP_DRIVE = 2.0      # tanh drive; adds a touch of speaker grit
NORMALIZE_PEAK = 0.9         # leave headroom so playback never hard-clips


# ---------------------------------------------------------------------------
# Pure DSP (unit-tested, no audio hardware needed)
# ---------------------------------------------------------------------------

def pitch_shift(samples: np.ndarray, rate: int, factor: float) -> np.ndarray:
    """Shift pitch by resampling.

    Reads the input at `factor` times normal speed, so playing the result at
    the original sample rate raises the pitch by `factor` (and shortens the
    clip by the same amount). This deliberately does NOT preserve duration:
    the chipmunk-style speed-up is part of the robot character.
    """
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0 or factor == 1.0:
        return samples.copy()
    if factor <= 0:
        raise ValueError(f"pitch factor must be positive, got {factor}")
    out_len = max(1, int(round(samples.size / factor)))
    src_positions = np.linspace(0, samples.size - 1, out_len)
    return np.interp(src_positions, np.arange(samples.size), samples)


def ring_mod(samples: np.ndarray, sr: int, freq: float, depth: float) -> np.ndarray:
    """Amplitude-modulate with a sine carrier for a metallic buzz.

    depth 0 leaves the signal untouched; depth 1 swings the gain over the
    full 1 - 2*depth .. 1 range.
    """
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0 or depth == 0.0:
        return samples.copy()
    t = np.arange(samples.size) / float(sr)
    carrier = (1.0 - depth) + depth * np.sin(2.0 * np.pi * freq * t)
    return samples * carrier


def soft_clip(samples: np.ndarray, drive: float = WALL_E_CLIP_DRIVE) -> np.ndarray:
    """Gentle tanh saturation.

    tanh bounds every output sample strictly inside (-1, 1) no matter how
    hot the input is, while compressing only the loudest peaks - mild
    speaker distortion. normalize() restores the level afterwards.
    """
    samples = np.asarray(samples, dtype=np.float64)
    if drive <= 0:
        raise ValueError(f"drive must be positive, got {drive}")
    return np.tanh(samples * drive)


def normalize(samples: np.ndarray, peak: float = NORMALIZE_PEAK) -> np.ndarray:
    """Scale so the absolute peak equals `peak`. Silence passes through."""
    samples = np.asarray(samples, dtype=np.float64)
    current = np.max(np.abs(samples)) if samples.size else 0.0
    if current == 0.0:
        return samples.copy()
    return samples * (peak / current)


def robotize(samples: np.ndarray, sr: int) -> np.ndarray:
    """Full wall-e chain: pitch up, ring-modulate, soft-clip, normalize."""
    out = pitch_shift(samples, sr, WALL_E_PITCH_FACTOR)
    out = ring_mod(out, sr, WALL_E_RING_FREQ_HZ, WALL_E_RING_DEPTH)
    out = soft_clip(out)
    return normalize(out)


# ---------------------------------------------------------------------------
# Wav helpers
# ---------------------------------------------------------------------------

def _read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read a wav file as mono float64 in [-1, 1] plus its sample rate."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit wav, got sample width {width}")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sr


def _write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    """Write mono float samples in [-1, 1] as a 16-bit wav file."""
    clipped = np.clip(samples, -1.0, 1.0)
    ints = np.round(clipped * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(ints.tobytes())


# ---------------------------------------------------------------------------
# TTS engines (lazy pyttsx3 imports)
# ---------------------------------------------------------------------------

def _make_engine(cfg: dict):
    """Build a configured pyttsx3 engine. Imported here so the module can be
    imported (and the DSP tested) on machines without audio support."""
    import pyttsx3

    engine = pyttsx3.init()
    rate = cfg.get("rate", DEFAULT_RATE_WPM)
    try:
        engine.setProperty("rate", int(rate))
    except (TypeError, ValueError):
        log.warning("Ignoring invalid voice rate %r", rate)
    wanted = cfg.get("voice_name", "")
    if wanted:
        for v in engine.getProperty("voices"):
            if wanted.lower() in (v.name or "").lower():
                engine.setProperty("voice", v.id)
                break
        else:
            log.warning("No installed voice matches %r; using the default", wanted)
    return engine


def _speak_system(text: str, cfg: dict) -> None:
    """Speak directly through the platform TTS engine."""
    engine = _make_engine(cfg)
    engine.say(text)
    engine.runAndWait()


def _speak_wall_e(text: str, cfg: dict) -> None:
    """Render TTS to a wav, run the robot DSP chain, and play the result."""
    tmp_dir = tempfile.mkdtemp(prefix="walleye_tts_")
    raw_path = os.path.join(tmp_dir, "raw.wav")
    fx_path = os.path.join(tmp_dir, "robot.wav")
    try:
        engine = _make_engine(cfg)
        engine.save_to_file(text, raw_path)
        engine.runAndWait()
        samples, sr = _read_wav(raw_path)
        _write_wav(fx_path, robotize(samples, sr), sr)
        _play_wav(fx_path)
    finally:
        for p in (raw_path, fx_path):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _play_wav(path: str) -> None:
    """Play a wav file with whatever the platform offers.

    Playback is best-effort: a missing player or busy device must never
    crash the app, so every failure is logged and swallowed.
    """
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)
        elif sys.platform == "darwin":
            subprocess.run(["afplay", path], check=True)
        else:
            subprocess.run(["aplay", "-q", path], check=True)
    except Exception as exc:
        log.warning("Could not play %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

# Only one utterance at a time; overlapping requests are dropped, not queued.
_speaking = threading.Lock()


def speak(text: str, cfg: dict) -> bool:
    """Say `text` using the configured voice. Non-blocking.

    cfg keys (all optional):
        voice       "off" | "system" | "wall-e"   (default "system")
        voice_name  substring matched against installed TTS voice names
        rate        speaking rate in words per minute

    Returns True if the request was accepted, False if it was dropped
    (empty text, voice off, or another utterance is still playing).
    """
    if not text or not text.strip():
        return False
    cfg = cfg or {}
    voice = cfg.get("voice", VOICE_SYSTEM)
    if voice == VOICE_OFF:
        return False
    if not _speaking.acquire(blocking=False):
        log.debug("Dropping overlapping speech request: %r", text[:60])
        return False

    def worker() -> None:
        try:
            if voice == VOICE_WALL_E:
                _speak_wall_e(text, cfg)
            else:
                if voice != VOICE_SYSTEM:
                    log.warning("Unknown voice %r; using system voice", voice)
                _speak_system(text, cfg)
        except Exception as exc:
            # Voice output is a convenience; never let it take the app down.
            log.warning("Speech failed: %s", exc)
        finally:
            _speaking.release()

    threading.Thread(target=worker, name="walleye-speech", daemon=True).start()
    return True
