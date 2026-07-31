"""Pure helpers for the Daily Briefing (Home) tab - greeting + date wording.

No I/O, no Qt: just deterministic text so it's trivially unit-tested. The tab
composes these with live data (reminders, room status, weather) at render time.
"""
import datetime as dt


def greeting_for_hour(hour: int) -> str:
    """A time-of-day greeting. 5-11 morning, 12-16 afternoon, 17-21 evening, else night."""
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 22:
        return "Good evening"
    return "Good evening"      # late night still reads as evening, not "night"


def date_line(when: dt.datetime) -> str:
    """'Wednesday, July 8' - no zero-padded day."""
    return f"{when.strftime('%A, %B')} {when.day}"


def clock_line(when: dt.datetime) -> str:
    """'8:42' (12-hour, no leading zero)."""
    return when.strftime("%I:%M").lstrip("0")


def ampm(when: dt.datetime) -> str:
    return when.strftime("%p")


def summarize_day(reminder_count: int, todo_open: int, room_clear) -> str:
    """A one-liner overview for the briefing. room_clear is True/False/None."""
    bits = []
    if reminder_count:
        bits.append(f"{reminder_count} reminder{'s' if reminder_count != 1 else ''}")
    if todo_open:
        bits.append(f"{todo_open} to-do{'s' if todo_open != 1 else ''}")
    lead = "Here's your day: " + ", ".join(bits) + "." if bits else \
        "Nothing on your plate right now."
    if room_clear is True:
        lead += " Your room looks tidy."
    elif room_clear is False:
        lead += " Your room could use a tidy."
    return lead
