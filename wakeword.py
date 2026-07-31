"""Optional voice wake word for Wall-Eye.

Say "Wall-E, what do you see?" out loud and the app answers through its
configured voice. Everything runs locally:

    mic -> energy gate -> faster-whisper transcription -> wake-phrase match
        -> (command, arg) -> callback into the GUI

The heavy dependencies (faster-whisper, sounddevice) are imported LAZILY
inside the functions that need them, so importing this module - and unit
testing the pure text logic below - never requires audio hardware or the
optional packages. WakePhraseListener.start() checks for them and simply
logs a hint and does nothing if they are missing.
"""

import logging
import re
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("walleye")

SAMPLE_RATE = 16000
BLOCK_SECS = 0.1        # mic is read in 100 ms blocks
SILENCE_RMS = 0.01      # RMS below this counts as silence
SILENCE_SECS = 1.0      # stop recording after this much trailing silence
MAX_SECS = 10           # hard cap on a single utterance
PREROLL_BLOCKS = 3      # blocks kept from just before speech is detected, so
                        # the first syllable of "Wall-E" isn't clipped off
ACK_SETTLE_SECS = 0.4   # brief pause after the spoken "Yes?" so the mic does
                        # not pick the acknowledgement itself up

DEPS_HINT = ("voice wake word disabled - pip install faster-whisper "
             "sounddevice")

_model = None
_model_lock = threading.Lock()


def _whisper(model_name):
    """Lazily build (once) and return the shared faster-whisper model."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            try:
                # Load straight from the local cache - skips the hub update
                # check that otherwise delays readiness (and fails offline).
                _model = WhisperModel(model_name, device="cpu",
                                      compute_type="int8",
                                      local_files_only=True)
            except Exception:
                # Not cached yet (first ever run) - download it.
                _model = WhisperModel(model_name, device="cpu",
                                      compute_type="int8")
        return _model


def record_until_silence():
    """Record from the default mic until ~1s of trailing silence (or the
    MAX_SECS cap). Returns mono float32 samples at SAMPLE_RATE."""
    import sounddevice as sd
    chunks = []
    silent_chunks = 0
    chunk_len = int(SAMPLE_RATE * BLOCK_SECS)
    started_talking = False
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        dtype="float32") as stream:
        for _ in range(int(MAX_SECS / BLOCK_SECS)):
            block, _overflow = stream.read(chunk_len)
            block = block[:, 0]
            chunks.append(block)
            rms = float(np.sqrt(np.mean(block ** 2)))
            if rms >= SILENCE_RMS:
                started_talking = True
                silent_chunks = 0
            elif started_talking:
                silent_chunks += 1
                if silent_chunks * BLOCK_SECS >= SILENCE_SECS:
                    break
    return np.concatenate(chunks) if chunks else np.zeros(1, np.float32)


def transcribe(audio, model_name="base.en"):
    """Speech to text via the shared local faster-whisper model."""
    segments, _info = _whisper(model_name).transcribe(audio, language="en",
                                                      beam_size=1)
    return " ".join(s.text for s in segments).strip()


# ---------------------------------------------------------------------------
# Pure text logic (unit-tested, no audio involved)
# ---------------------------------------------------------------------------

# Every way Whisper tends to render the name "Wall-E" in a transcript.
# "wali"/"walli"/"wall i" are real Whisper renderings of the spoken name
# (observed in live testing as "WALI, what time is it?").
DEFAULT_ALIASES = ("wall-e", "wall e", "walle", "wally", "wall-eye",
                   "wall eye", "wally e", "wali", "walli", "wall i")


def _norm_tokens(text):
    """Lowercase, delete punctuation and split into word tokens, so matching
    is case- and punctuation-insensitive. Punctuation is REMOVED (not spaced)
    so "what's" -> "whats" and "Wall-E" -> "walle" - the same normalization
    is applied to the aliases, keeping both sides consistent."""
    return re.sub(r"[^\w\s]", "", (text or "").lower()).split()


def extract_wake_command(text, aliases=DEFAULT_ALIASES):
    """Find the wake phrase in a transcript and return what follows it.

    Returns:
        str   the command after the wake phrase (may follow it mid-sentence:
              "hey wall-e do X" -> "do x"; the result is normalized to
              lowercase words)
        ""    the utterance is ONLY the wake phrase ("Wall-E?")
        None  no wake phrase present

    When aliases overlap ("wally" vs "wally e") the longest match at the
    earliest position wins, so the trailing "e" is never mistaken for the
    start of the command.
    """
    tokens = _norm_tokens(text)
    if not tokens:
        return None
    alias_seqs = {tuple(_norm_tokens(a)) for a in aliases}
    alias_seqs.discard(())
    best = None            # (start_index, -length, end_index)
    for seq in alias_seqs:
        n = len(seq)
        for i in range(len(tokens) - n + 1):
            if tuple(tokens[i:i + n]) == seq:
                cand = (i, -n, i + n)
                if best is None or cand < best:
                    best = cand
                break      # earliest occurrence of this alias is enough
    if best is None:
        return None
    return " ".join(tokens[best[2]:])


def parse_command(text):
    """Map a spoken command to (command, arg).

    Commands:
        check   run a room check now (arg = task name or None)
        status  speak the last verdict (arg = None)
        time    speak the current time
        date    speak today's date
        chat    anything else - send it through the chat model (arg = text)

    Conversational "look at ..." phrases are routed to chat (with a camera
    frame attached by the caller via wants_to_see) rather than to a full
    room check, so "what do you see" gets a spoken description instead of a
    messy/clear verdict.
    """
    t = " ".join(_norm_tokens(text))
    if not t:
        return ("chat", text)

    if ("whats messy" in t or "what is messy" in t or "status" in t
            or "how does it look" in t or "hows the room" in t
            or "how is the room" in t):
        return ("status", None)

    if "what time" in t or "the time" in t:
        return ("time", None)
    if "what day" in t or "the date" in t or "what date" in t or "todays date" in t:
        return ("date", None)

    if wants_to_see(t):
        return ("chat", text)

    if "check" in t or "look" in t:
        return ("check", _task_in(t))

    return ("chat", text)


# Conversational phrases that mean "look through the camera and tell me what
# you see" - when one is heard the chat turn should carry a live frame.
# Stored without apostrophes so they match the punctuation-stripped text.
SEE_PHRASES = ("what do you see", "what can you see", "can you see",
               "do you see", "whats in the room", "what is in the room",
               "look at me", "look at this", "look at the room", "look around",
               "how do i look", "what do i look like", "see me", "see the room",
               "describe the room", "describe what you see", "what am i wearing",
               "what am i doing", "whats on my desk", "whats behind me")


def wants_to_see(text: str) -> bool:
    """True if the utterance asks Wall-Eye to look through the camera, so the
    chat turn should carry a live frame. Distinct from the 'check' command
    (which runs a full room check); this just lets the assistant see and chat."""
    t = " ".join(_norm_tokens(text))
    return any(phrase in t for phrase in SEE_PHRASES)


STOP_PHRASES = ("stop", "goodbye", "good bye", "bye", "thats all",
                "that is all", "that will be all", "thatll be all",
                "thank you", "thanks", "nevermind", "never mind", "im done")


def is_stop_phrase(text: str) -> bool:
    """True if the user is signing off a voice exchange. Only a SHORT,
    standalone sign-off counts, so a mid-sentence 'thanks, now...' does not
    end the conversation."""
    t = " ".join(_norm_tokens(text))
    if not t or len(t.split()) > 4:
        return False
    return any(p in t for p in STOP_PHRASES)


def _task_in(t):
    """Pull a watch-task name out of a normalized transcript, if mentioned."""
    for word in t.split():
        if word in ("room", "printer", "print", "dog", "couch", "desk"):
            return {"print": "printer", "couch": "dog-couch",
                    "dog": "dog-couch"}.get(word, word)
    return None


# ---------------------------------------------------------------------------
# Always-on listener
# ---------------------------------------------------------------------------

def _deps_available():
    """True if the optional audio/STT packages are importable."""
    import importlib.util
    return (importlib.util.find_spec("faster_whisper") is not None
            and importlib.util.find_spec("sounddevice") is not None)


class WakePhraseListener:
    """Daemon thread that listens for "Wall-E, <command>" on the default mic.

    Cheap while idle: the mic is read in 100 ms blocks and only an RMS energy
    check runs until someone actually speaks. On speech it records until
    silence, transcribes locally with faster-whisper, and looks for the wake
    phrase in the transcript:

      * "Wall-E, what do you see?"  -> on_command(*parse_command("what do you see"))
      * "Wall-E?" (nothing after)   -> speak_fn("Yes?"), then one follow-up
                                       utterance is recorded as the command
      * no wake phrase              -> ignored entirely

    Every failure is logged and swallowed - voice input is a convenience and
    must never take the app down. start() is a no-op (with a hint in the log)
    when faster-whisper / sounddevice are not installed.
    """

    def __init__(self, on_command, speak_fn=None, model_name="base.en",
                 aliases=DEFAULT_ALIASES):
        self.on_command = on_command    # callback: (command, arg) -> None
        self.speak_fn = speak_fn        # optional: (text) -> None acknowledgements
        self.model_name = model_name
        self.aliases = tuple(aliases) if aliases else DEFAULT_ALIASES
        self._run = False
        self._thread = None

    def start(self):
        """Begin listening on a daemon thread. Safe to call when the optional
        dependencies are missing (logs a hint and returns) or when already
        running (no-op)."""
        if self._thread is not None and self._thread.is_alive():
            return
        if not _deps_available():
            log.info(DEPS_HINT)
            return
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="walleye-wakeword")
        self._thread.start()
        # Warm whisper so the first command isn't slow.
        threading.Thread(target=self._warm, daemon=True).start()
        log.info("voice wake word ready - say 'Wall-E, ...'")

    def stop(self):
        """Ask the listener thread to finish. It exits after its current mic
        read (at most one block / recording pass)."""
        self._run = False

    # ----- internals -----

    def _warm(self):
        try:
            _whisper(self.model_name)
        except Exception:
            log.exception("whisper model preload failed")

    def _loop(self):
        while self._run:
            try:
                audio = self._wait_and_record()
                if audio is None:
                    continue
                self._handle_utterance(audio)
            except Exception:
                log.exception("wake word listener error")
                time.sleep(1)   # don't spin on a persistent mic error

    def _wait_and_record(self):
        """Hold the mic open, waiting cheaply (RMS gate only, no whisper) for
        speech; then record until silence. Returns the samples, or None when
        stopping. A short pre-roll is kept so the first syllable survives."""
        import sounddevice as sd
        chunk_len = int(SAMPLE_RATE * BLOCK_SECS)
        preroll = deque(maxlen=PREROLL_BLOCKS)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            dtype="float32") as stream:
            # Idle phase: discard silence, keep only the pre-roll ring.
            while True:
                if not self._run:
                    return None
                block, _overflow = stream.read(chunk_len)
                block = block[:, 0]
                rms = float(np.sqrt(np.mean(block ** 2)))
                if rms >= SILENCE_RMS:
                    break
                preroll.append(block)
            # Active phase: record until trailing silence or the cap.
            chunks = list(preroll) + [block]
            silent_chunks = 0
            for _ in range(int(MAX_SECS / BLOCK_SECS)):
                if not self._run:
                    return None
                block, _overflow = stream.read(chunk_len)
                block = block[:, 0]
                chunks.append(block)
                rms = float(np.sqrt(np.mean(block ** 2)))
                if rms >= SILENCE_RMS:
                    silent_chunks = 0
                else:
                    silent_chunks += 1
                    if silent_chunks * BLOCK_SECS >= SILENCE_SECS:
                        break
        return np.concatenate(chunks)

    def _handle_utterance(self, audio):
        text = transcribe(audio, self.model_name)
        if not text:
            return
        command_text = extract_wake_command(text, self.aliases)
        if command_text is None:
            log.debug("heard (no wake phrase): %r", text)
            return
        log.info("wake phrase heard: %r", text)
        if command_text == "":
            # Just the name - acknowledge, then take one follow-up utterance.
            command_text = self._follow_up()
            if not command_text:
                return
        if not self._run:
            return
        self.on_command(*parse_command(command_text))

    def _follow_up(self):
        """Acknowledge a bare "Wall-E?" and record the command that follows."""
        if self.speak_fn:
            try:
                self.speak_fn("Yes?")
            except Exception:
                log.exception("acknowledgement speech failed")
            time.sleep(ACK_SETTLE_SECS)
        audio = record_until_silence()
        text = transcribe(audio, self.model_name)
        log.info("follow-up heard: %r", text)
        if not text and self.speak_fn:
            try:
                self.speak_fn("I didn't catch that.")
            except Exception:
                log.exception("acknowledgement speech failed")
        return text
