"""Tests for the pure text logic in wakeword.py.

No audio hardware, faster-whisper or sounddevice involved - importing the
module must succeed without the optional dependencies installed.
"""

import pytest

import wakeword
from wakeword import (DEFAULT_ALIASES, extract_wake_command, is_stop_phrase,
                      parse_command, wants_to_see)


# ---------------------------------------------------------------------------
# extract_wake_command
# ---------------------------------------------------------------------------

class TestExtractWakeCommand:
    @pytest.mark.parametrize("alias", DEFAULT_ALIASES)
    def test_every_default_alias_is_recognised(self, alias):
        assert extract_wake_command(f"{alias} check the room") == "check the room"

    @pytest.mark.parametrize("alias", DEFAULT_ALIASES)
    def test_every_default_alias_alone_returns_empty(self, alias):
        assert extract_wake_command(alias) == ""

    def test_whisper_style_punctuation_and_case(self):
        assert (extract_wake_command("Wall-E, what do you see?")
                == "what do you see")

    def test_capitalised_and_shouted(self):
        assert extract_wake_command("WALLY! Check the room.") == "check the room"

    def test_mid_sentence_wake_phrase(self):
        assert extract_wake_command("hey wall-e do the thing") == "do the thing"

    def test_leading_filler_is_dropped(self):
        assert (extract_wake_command("okay so um Wall E, status please")
                == "status please")

    def test_only_wake_phrase_with_punctuation(self):
        assert extract_wake_command("Wall-E?") == ""

    def test_no_wake_phrase_returns_none(self):
        assert extract_wake_command("check the room please") is None

    def test_empty_and_none_input(self):
        assert extract_wake_command("") is None
        assert extract_wake_command(None) is None

    def test_longest_alias_wins_over_prefix(self):
        # "wally e" must match as a whole so the trailing "e" is not
        # swallowed into the command by the shorter "wally" alias.
        assert extract_wake_command("wally e turn around") == "turn around"

    def test_wall_eye_alias(self):
        assert (extract_wake_command("Wall-Eye, how is the room?")
                == "how is the room")

    def test_command_is_normalised_lowercase(self):
        assert (extract_wake_command("Wall-E, What's On My Desk?")
                == "whats on my desk")

    def test_custom_aliases(self):
        assert extract_wake_command("robot do it", aliases=("robot",)) == "do it"
        assert extract_wake_command("wall-e do it", aliases=("robot",)) is None

    def test_unrelated_word_containing_alias_letters(self):
        # "wallet" must not match "walle" - matching is whole-token.
        assert extract_wake_command("my wallet is on the desk") is None


# ---------------------------------------------------------------------------
# parse_command
# ---------------------------------------------------------------------------

class TestParseCommand:
    def test_check(self):
        assert parse_command("check the room") == ("check", "room")

    def test_check_without_task(self):
        assert parse_command("run a check") == ("check", None)

    def test_look_is_a_check(self):
        assert parse_command("take a look") == ("check", None)

    def test_check_task_synonyms(self):
        assert parse_command("check the print") == ("check", "printer")
        assert parse_command("check the couch") == ("check", "dog-couch")

    def test_status_phrases(self):
        for phrase in ("status", "what's messy", "how does it look",
                       "how's the room", "how is the room"):
            assert parse_command(phrase) == ("status", None), phrase

    def test_time(self):
        assert parse_command("what time is it") == ("time", None)
        assert parse_command("tell me the time") == ("time", None)

    def test_date(self):
        assert parse_command("what's the date") == ("date", None)
        assert parse_command("what day is it") == ("date", None)

    def test_everything_else_is_chat(self):
        assert parse_command("tell me a joke") == ("chat", "tell me a joke")

    def test_empty_is_chat(self):
        assert parse_command("") == ("chat", "")

    def test_see_phrases_route_to_chat_not_check(self):
        # "look at me" contains "look" but is conversational - it should go
        # through chat (with a frame attached) rather than run a room check.
        cmd, arg = parse_command("look at me")
        assert cmd == "chat"
        assert arg == "look at me"
        assert parse_command("what do you see")[0] == "chat"

    def test_dropped_commands_fall_through_to_chat(self):
        # snooze/retake/search/youtube were deliberately not ported.
        assert parse_command("snooze for ten minutes")[0] == "chat"
        assert parse_command("play something on youtube")[0] == "chat"


# ---------------------------------------------------------------------------
# wants_to_see
# ---------------------------------------------------------------------------

class TestWantsToSee:
    @pytest.mark.parametrize("phrase", [
        "what do you see", "What can you see?", "can you see me",
        "what's in the room", "look at me", "look around",
        "how do I look today", "describe the room",
        "what am I wearing", "what's on my desk",
    ])
    def test_positive(self, phrase):
        assert wants_to_see(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "tell me a joke", "what time is it", "check the room", "", None,
    ])
    def test_negative(self, phrase):
        assert wants_to_see(phrase) is False


# ---------------------------------------------------------------------------
# is_stop_phrase
# ---------------------------------------------------------------------------

class TestIsStopPhrase:
    @pytest.mark.parametrize("phrase", [
        "stop", "goodbye", "bye", "that's all", "thank you", "thanks",
        "never mind", "I'm done", "ok thanks",
    ])
    def test_positive(self, phrase):
        assert is_stop_phrase(phrase) is True

    def test_long_sentence_containing_thanks_is_not_a_stop(self):
        assert is_stop_phrase("thanks, now check the room again please") is False

    def test_empty(self):
        assert is_stop_phrase("") is False

    def test_unrelated(self):
        assert is_stop_phrase("check the room") is False


# ---------------------------------------------------------------------------
# module-level safety
# ---------------------------------------------------------------------------

def test_import_needs_no_optional_deps():
    """wakeword must import (and the listener must construct) without
    faster-whisper or sounddevice - they are only touched inside start()."""
    listener = wakeword.WakePhraseListener(on_command=lambda c, a: None)
    # stop() before start() must be harmless.
    listener.stop()
    assert listener._thread is None


def test_start_without_deps_logs_and_returns(monkeypatch, caplog):
    """When the optional packages are missing, start() logs the install hint
    once and does not spawn a listener thread."""
    monkeypatch.setattr(wakeword, "_deps_available", lambda: False)
    listener = wakeword.WakePhraseListener(on_command=lambda c, a: None)
    with caplog.at_level("INFO", logger="walleye"):
        listener.start()
    assert listener._thread is None
    assert any(wakeword.DEPS_HINT in r.message for r in caplog.records)
