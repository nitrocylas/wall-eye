"""Tests for the pure DSP core of speech.py and the speak() gatekeeping.

None of these touch audio hardware: pyttsx3 is only imported inside the
synthesis helpers, which are never reached here.
"""
import wave

import numpy as np
import pytest

import speech


class TestPhoneticize:
    def test_respells_the_name_for_speech(self):
        assert speech.phoneticize("Wall-Eye online.") == "Wally online."
        assert speech.phoneticize("wall eye is watching") == \
            "Wally is watching"
        assert speech.phoneticize("Ask WALL-EYE anything") == \
            "Ask Wally anything"

    def test_possessive_and_plural_forms(self):
        assert speech.phoneticize("Wall-Eyes cameras") == "Wallies cameras"

    def test_leaves_other_words_alone(self):
        assert speech.phoneticize("the wallpaper and my eye") == \
            "the wallpaper and my eye"
        assert speech.phoneticize("eyewall of the storm") == \
            "eyewall of the storm"


class TestBackendSelection:
    def test_off_is_off_regardless_of_kokoro(self):
        assert speech.resolve_backend("off", True) == "off"
        assert speech.resolve_backend("off", False) == "off"

    def test_cute_needs_kokoro(self):
        assert speech.resolve_backend("cute", True) == "cute"
        assert speech.resolve_backend("cute", False) == "system"

    def test_wall_e_prefers_kokoro_base(self):
        assert speech.resolve_backend("wall-e", True) == "wall-e-kokoro"
        assert speech.resolve_backend("wall-e", False) == "wall-e-system"

    def test_unknown_and_system_map_to_system(self):
        assert speech.resolve_backend("system", True) == "system"
        assert speech.resolve_backend("dalek-9000", True) == "system"


SR = 22050


def sine(freq: float = 440.0, seconds: float = 0.25, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    return np.sin(2.0 * np.pi * freq * t)


# ---------------------------------------------------------------------------
# pitch_shift
# ---------------------------------------------------------------------------

class TestPitchShift:
    def test_factor_one_is_identity(self):
        x = sine()
        out = speech.pitch_shift(x, SR, 1.0)
        assert np.allclose(out, x)

    def test_output_length_shrinks_by_factor(self):
        x = sine()
        out = speech.pitch_shift(x, SR, 1.3)
        assert out.shape == (round(x.size / 1.3),)

    def test_raises_dominant_frequency(self):
        # A 200 Hz tone shifted by 2.0 should land near 400 Hz.
        x = sine(freq=200.0, seconds=1.0)
        out = speech.pitch_shift(x, SR, 2.0)
        spectrum = np.abs(np.fft.rfft(out))
        peak_hz = np.fft.rfftfreq(out.size, 1.0 / SR)[np.argmax(spectrum)]
        assert peak_hz == pytest.approx(400.0, abs=5.0)

    def test_empty_input(self):
        assert speech.pitch_shift(np.array([]), SR, 1.3).size == 0

    def test_rejects_nonpositive_factor(self):
        with pytest.raises(ValueError):
            speech.pitch_shift(sine(), SR, 0.0)


# ---------------------------------------------------------------------------
# ring_mod
# ---------------------------------------------------------------------------

class TestRingMod:
    def test_preserves_shape(self):
        x = sine()
        assert speech.ring_mod(x, SR, 35.0, 0.35).shape == x.shape

    def test_zero_depth_is_identity(self):
        x = sine()
        assert np.allclose(speech.ring_mod(x, SR, 35.0, 0.0), x)

    def test_actually_modulates(self):
        # A constant (DC) input must come out varying at the carrier rate.
        x = np.full(SR, 0.5)
        out = speech.ring_mod(x, SR, 35.0, 0.35)
        assert np.std(out) > 0.05
        # Gain envelope stays within the documented 1 - 2*depth .. 1 band.
        gain = out / 0.5
        assert gain.max() <= 1.0 + 1e-9
        assert gain.min() >= 1.0 - 2 * 0.35 - 1e-9

    def test_carrier_frequency_appears(self):
        x = np.full(SR, 1.0)  # 1-second DC input, so bins are exactly 1 Hz
        out = speech.ring_mod(x, SR, 35.0, 0.35)
        spectrum = np.abs(np.fft.rfft(out - np.mean(out)))
        assert np.argmax(spectrum) == 35


# ---------------------------------------------------------------------------
# soft_clip
# ---------------------------------------------------------------------------

class TestSoftClip:
    def test_bounds(self):
        x = np.linspace(-5.0, 5.0, 1001)
        out = speech.soft_clip(x)
        assert np.all(np.abs(out) <= 1.0 + 1e-9)

    def test_compresses_peaks_more_than_body(self):
        # Saturation: gain at a loud peak must be lower than at a quiet sample.
        quiet_gain = speech.soft_clip(np.array([0.1]))[0] / 0.1
        loud_gain = speech.soft_clip(np.array([1.0]))[0] / 1.0
        assert loud_gain < quiet_gain

    def test_monotonic_and_odd(self):
        x = np.linspace(-2.0, 2.0, 401)
        out = speech.soft_clip(x)
        assert np.all(np.diff(out) > 0)
        assert np.allclose(out, -out[::-1])

    def test_rejects_nonpositive_drive(self):
        with pytest.raises(ValueError):
            speech.soft_clip(sine(), drive=0.0)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_peak_hits_target(self):
        x = sine() * 0.1
        out = speech.normalize(x, peak=0.9)
        assert np.max(np.abs(out)) == pytest.approx(0.9)

    def test_attenuates_loud_input(self):
        x = sine() * 3.0
        out = speech.normalize(x, peak=0.9)
        assert np.max(np.abs(out)) == pytest.approx(0.9)

    def test_silence_passes_through(self):
        x = np.zeros(100)
        out = speech.normalize(x)
        assert out.shape == x.shape
        assert np.all(out == 0.0)
        assert not np.any(np.isnan(out))


# ---------------------------------------------------------------------------
# robotize (chain sanity)
# ---------------------------------------------------------------------------

class TestRobotize:
    def test_output_is_valid_audio(self):
        out = speech.robotize(sine(seconds=0.5), SR)
        assert out.size > 0
        assert np.all(np.abs(out) <= 1.0 + 1e-9)
        assert np.max(np.abs(out)) == pytest.approx(speech.NORMALIZE_PEAK)


# ---------------------------------------------------------------------------
# wav round trip
# ---------------------------------------------------------------------------

class TestWavRoundTrip:
    def test_write_then_read(self, tmp_path):
        path = str(tmp_path / "tone.wav")
        x = sine(seconds=0.1) * 0.5
        speech._write_wav(path, x, SR)
        back, sr = speech._read_wav(path)
        assert sr == SR
        assert back.shape == x.shape
        assert np.allclose(back, x, atol=1e-4)

    def test_read_rejects_non_16bit(self, tmp_path):
        path = str(tmp_path / "eight.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(1)
            wf.setframerate(SR)
            wf.writeframes(b"\x80" * 100)
        with pytest.raises(ValueError):
            speech._read_wav(path)


# ---------------------------------------------------------------------------
# speak() gatekeeping (no audio path reached)
# ---------------------------------------------------------------------------

class TestSpeakGate:
    def test_voice_off_is_noop(self):
        assert speech.speak("hello", {"voice": "off"}) is False

    def test_empty_text_is_dropped(self):
        assert speech.speak("", {"voice": "system"}) is False
        assert speech.speak("   ", {"voice": "system"}) is False

    def test_overlapping_request_is_dropped(self, monkeypatch):
        import threading

        release = threading.Event()
        started = threading.Event()

        def slow_speak(text, cfg):
            started.set()
            release.wait(timeout=5)

        monkeypatch.setattr(speech, "_speak_system", slow_speak)
        try:
            assert speech.speak("first", {"voice": "system"}) is True
            assert started.wait(timeout=5)
            assert speech.speak("second", {"voice": "system"}) is False
        finally:
            release.set()
        # The lock must free up once the utterance finishes.
        assert speech._speaking.acquire(timeout=5)
        speech._speaking.release()
