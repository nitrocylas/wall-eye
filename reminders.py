"""Recurring reminders for Wall-Eye.

A reminder is a message plus a recurrence rule. Wall-Eye pops a little card on
the dashboard (and optionally speaks) when one comes due. This module holds ONLY the
pure pieces - the recurrence math, a natural-language parser, and a tiny JSON
store - so every non-trivial decision is unit-tested without a clock or a UI.

Recurrence kinds:
    once      fire a single time at `date` + `time`, then disable itself
    daily     every day at `time`
    weekdays  Monday-Friday at `time`
    weekly    on the chosen `days` (0=Mon .. 6=Sun) at `time`
    interval  every `every_minutes` minutes, measured from the last fire

The engine decides "is this due?" with `is_due(reminder, now, last_fired)`, which
is built on `next_fire()` - the first scheduled moment strictly after an anchor.
Anchoring on the last fire (not on `now`) means a reminder catches up at most ONCE
after downtime instead of firing a burst for every slot it slept through.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

_ID_SEQ = itertools.count()   # keeps ids unique even for same-millisecond adds

ONCE = "once"
DAILY = "daily"
WEEKDAYS = "weekdays"
WEEKLY = "weekly"
INTERVAL = "interval"
KINDS = (ONCE, DAILY, WEEKDAYS, WEEKLY, INTERVAL)

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WORKWEEK = {0, 1, 2, 3, 4}


@dataclass
class Reminder:
    """One reminder and its recurrence rule. Times are local wall-clock."""
    message: str
    kind: str = DAILY
    time: str = "09:00"          # "HH:MM" - daily/weekdays/weekly/once
    date: str = ""               # "YYYY-MM-DD" - once only
    days: List[int] = field(default_factory=list)   # 0=Mon..6=Sun - weekly only
    every_minutes: int = 60      # interval only
    enabled: bool = True
    speak: bool = True
    created: float = 0.0
    last_fired: float = 0.0
    id: str = ""

    def __post_init__(self):
        if not self.created:
            self.created = time.time()
        if not self.id:
            # Stable, unique, human-sortable - no randomness needed.
            self.id = f"r{int(self.created * 1000)}_{next(_ID_SEQ)}"

    # ---- serialisation ----
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Reminder":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


def _hh_mm(reminder: Reminder) -> tuple[int, int]:
    hh, mm = reminder.time.split(":")
    return int(hh), int(mm)


def _next_at_time(after: dt.datetime, hh: int, mm: int,
                  allowed_days: Optional[set]) -> Optional[dt.datetime]:
    """First datetime at hh:mm strictly after `after` whose weekday is allowed
    (allowed_days=None means every day). Scans up to a week ahead."""
    base = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for i in range(0, 8):
        cand = base + dt.timedelta(days=i)
        if cand <= after:
            continue
        if allowed_days is None or cand.weekday() in allowed_days:
            return cand
    return None


def next_fire(reminder: Reminder, after: dt.datetime) -> Optional[dt.datetime]:
    """The first moment this reminder should fire strictly after `after`, or None
    if it never will again (a one-shot already in the past)."""
    if reminder.kind == INTERVAL:
        step = max(1, int(reminder.every_minutes))
        return after + dt.timedelta(minutes=step)
    if reminder.kind == ONCE:
        if not reminder.date:
            return None
        y, mo, d = (int(x) for x in reminder.date.split("-"))
        hh, mm = _hh_mm(reminder)
        target = dt.datetime(y, mo, d, hh, mm)
        return target if target > after else None
    hh, mm = _hh_mm(reminder)
    if reminder.kind == DAILY:
        return _next_at_time(after, hh, mm, None)
    if reminder.kind == WEEKDAYS:
        return _next_at_time(after, hh, mm, _WORKWEEK)
    if reminder.kind == WEEKLY:
        days = set(reminder.days) or _WORKWEEK
        return _next_at_time(after, hh, mm, days)
    return None


def is_due(reminder: Reminder, now: dt.datetime,
           last_fired: Optional[dt.datetime] = None) -> bool:
    """True if `reminder` should fire at `now`. Anchors on the last fire (or, if
    never fired, on creation) so a slept-through schedule catches up only once."""
    if not reminder.enabled:
        return False
    if last_fired is None:
        last_fired = (dt.datetime.fromtimestamp(reminder.last_fired)
                      if reminder.last_fired else
                      dt.datetime.fromtimestamp(reminder.created))
    nxt = next_fire(reminder, last_fired)
    return nxt is not None and nxt <= now


def upcoming(reminder_list, now: dt.datetime, limit: Optional[int] = None):
    """Enabled reminders sorted by their NEXT future firing (soonest first).
    Returns [(reminder, next_datetime)]. Pure - drives 'what's coming up?'."""
    items = []
    for r in reminder_list:
        if not r.enabled:
            continue
        nf = next_fire(r, now)
        if nf is not None:
            items.append((r, nf))
    items.sort(key=lambda pair: pair[1])
    return items[:limit] if limit else items


def _fmt_time(hh_mm: str) -> str:
    """'16:00' -> '4:00 PM'."""
    try:
        return dt.datetime.strptime(hh_mm, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return hh_mm


def describe(reminder: Reminder) -> str:
    """A short human phrase for the recurrence, e.g. 'Every day at 4:00 PM'."""
    if reminder.kind == INTERVAL:
        n = max(1, int(reminder.every_minutes))
        if n % 60 == 0:
            hrs = n // 60
            return f"Every {hrs} hour{'s' if hrs != 1 else ''}"
        return f"Every {n} minute{'s' if n != 1 else ''}"
    t = _fmt_time(reminder.time)
    if reminder.kind == DAILY:
        return f"Every day at {t}"
    if reminder.kind == WEEKDAYS:
        return f"Weekdays at {t}"
    if reminder.kind == WEEKLY:
        days = sorted(set(reminder.days))
        names = ", ".join(WEEKDAY_NAMES[d] for d in days) if days else "weekly"
        return f"{names} at {t}"
    if reminder.kind == ONCE:
        try:
            d = dt.datetime.strptime(reminder.date, "%Y-%m-%d")
            return f"Once on {d.strftime('%b %d').replace(' 0', ' ')} at {t}"
        except ValueError:
            return f"Once at {t}"
    return reminder.kind


# ---------------------------------------------------------------------------
# Natural-language parsing: "remind me to X every day at 4pm"
# ---------------------------------------------------------------------------
_REMIND_RE = re.compile(
    r"^(?:please\s+)?(?:remind me|set a reminder|reminder)"
    r"(?:\s+to|\s+that|\s+about|\s*[:,])?\s+(.*\S)", re.IGNORECASE)

_DOW = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_TIME_RE = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
_IN_RE = re.compile(
    r"\bin\s+(\d+)\s*(min|mins|minute|minutes|hour|hours|hr|hrs)\b", re.IGNORECASE)
_EVERY_RE = re.compile(
    r"\bevery\s+(\d+)?\s*(min|mins|minute|minutes|hour|hours|hr|hrs)\b",
    re.IGNORECASE)


def _parse_clock(text: str) -> Optional[str]:
    """Pull an 'at 4pm' / 'at 16:00' clock out of `text` -> 'HH:MM' or None."""
    m = _TIME_RE.search(text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh != 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def _strip_schedule_words(msg: str) -> str:
    """Remove the timing clause from the action text so the stored message reads
    cleanly ('take out the trash', not 'take out the trash every day at 4pm')."""
    msg = _TIME_RE.sub("", msg)
    msg = _IN_RE.sub("", msg)
    msg = _EVERY_RE.sub("", msg)
    msg = re.sub(r"\bevery\s+(day|weekday|weekdays)\b", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\bevery\s+(\w+day)\b", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\bon\s+(\w+day)s?\b", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\b(today|tomorrow|tonight)\b", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\s{2,}", " ", msg)
    return msg.strip(" ,.:")


def parse_reminder(text: str, now: Optional[dt.datetime] = None) -> Optional[Reminder]:
    """Turn 'remind me to water the plants every day at 6pm' into a Reminder, or
    None if `text` isn't a reminder request. `now` is injectable for testing."""
    m = _REMIND_RE.match((text or "").strip())
    if not m:
        return None
    if now is None:
        now = dt.datetime.now()
    body = m.group(1).strip()
    low = body.lower()
    message = _strip_schedule_words(body) or body

    # "in 20 minutes" / "in 2 hours" -> a one-shot at now + delta
    im = _IN_RE.search(low)
    if im:
        n = int(im.group(1))
        mins = n * 60 if im.group(2).lower().startswith(("hour", "hr")) else n
        target = now + dt.timedelta(minutes=mins)
        return Reminder(message=message, kind=ONCE,
                        date=target.strftime("%Y-%m-%d"),
                        time=target.strftime("%H:%M"))

    # "every 30 minutes" / "every 2 hours" -> interval
    em = _EVERY_RE.search(low)
    if em:
        n = int(em.group(1) or 1)
        mins = n * 60 if em.group(2).lower().startswith(("hour", "hr")) else n
        return Reminder(message=message, kind=INTERVAL, every_minutes=max(1, mins))

    clock = _parse_clock(low) or "09:00"
    recurring = "every" in low

    if re.search(r"\bevery\s+weekday", low) or "on weekdays" in low:
        return Reminder(message=message, kind=WEEKDAYS, time=clock)

    # a named day of week: "every monday" -> weekly; "on monday" -> next monday (once)
    for word, idx in _DOW.items():
        if re.search(rf"\b{word}\b", low):
            if recurring:
                return Reminder(message=message, kind=WEEKLY, time=clock, days=[idx])
            target = _next_at_time(now, *(int(x) for x in clock.split(":")), {idx})
            if target:
                return Reminder(message=message, kind=ONCE,
                                date=target.strftime("%Y-%m-%d"), time=clock)

    if re.search(r"\bevery\s+day\b", low) or "daily" in low or recurring:
        return Reminder(message=message, kind=DAILY, time=clock)

    # "at 4pm" with no 'every' -> a one-shot at the next occurrence of that time
    if _TIME_RE.search(low):
        hh, mm = (int(x) for x in clock.split(":"))
        tomorrow = "tomorrow" in low
        anchor = now + dt.timedelta(days=1) if tomorrow else now
        # for 'tomorrow' start scanning from the start of tomorrow
        if tomorrow:
            anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        target = _next_at_time(anchor - dt.timedelta(seconds=1), hh, mm, None) \
            if tomorrow else _next_at_time(now, hh, mm, None)
        if target:
            return Reminder(message=message, kind=ONCE,
                            date=target.strftime("%Y-%m-%d"), time=clock)
    return None


# ---------------------------------------------------------------------------
# Timers: "set a timer for 10 minutes" -> a one-shot at now + duration.
# ---------------------------------------------------------------------------
_TIMER_RE = re.compile(r"\btimer\b", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"(\d+)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.IGNORECASE)
# Same, but also swallows a "for" that introduces the duration, for label-stripping.
_DURATION_STRIP_RE = re.compile(
    r"\b(?:for\s+)?\d+\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)\b",
    re.IGNORECASE)
# "...for tea", "...called tea", "...for the pasta" - an optional label.
_TIMER_LABEL_RE = re.compile(
    r"\b(?:for|called|named)\s+(?:the\s+)?([a-z][a-z0-9 ]{0,28}?)\s*$",
    re.IGNORECASE)


def _human_duration(total_seconds: int) -> str:
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def parse_timer(text: str, now: Optional[dt.datetime] = None) -> Optional[Reminder]:
    """'set a timer for 10 minutes' / 'timer for 5 min for tea' -> a one-shot
    Reminder. Sub-minute precision is coarse (the scheduler ticks every ~15s).
    Returns None if `text` isn't a timer request."""
    t = (text or "").strip()
    if not _TIMER_RE.search(t):
        return None
    m = _DURATION_RE.search(t)
    if not m:
        return None
    if now is None:
        now = dt.datetime.now()
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith(("hour", "hr")):
        seconds = n * 3600
    elif unit.startswith(("min",)):
        seconds = n * 60
    else:
        seconds = n
    target = now + dt.timedelta(seconds=seconds)

    # An optional label, e.g. "timer for tea" (but not "timer for 5 minutes").
    # Strip the duration *and* any "for" that introduces it, so "for 5 minutes
    # for tea" leaves just "... for tea" and the label reads as "tea".
    tail = _DURATION_STRIP_RE.sub("", t)
    label_m = _TIMER_LABEL_RE.search(tail.strip())
    label = label_m.group(1).strip() if label_m else ""
    dur = _human_duration(seconds)
    message = (f"{label.capitalize()} timer is up ({dur})" if label
               else f"Timer's up - {dur}")
    return Reminder(message=message, kind=ONCE,
                    date=target.strftime("%Y-%m-%d"),
                    time=target.strftime("%H:%M"))


def parse_request(text: str, now: Optional[dt.datetime] = None) -> Optional[Reminder]:
    """Parse either a reminder OR a timer request. Timers are checked first so
    'set a timer for 10 minutes' isn't misread as a reminder. Returns None if
    neither matches. This is the single entry point the engine calls."""
    return parse_timer(text, now) or parse_reminder(text, now)


def confirmation(reminder: Reminder, was_timer: bool = False) -> str:
    """A spoken confirmation for a freshly-set reminder or timer. Pure."""
    if was_timer:
        return "Okay, timer set - I'll ping you when it's up."
    return f"Got it - I'll remind you to {reminder.message}. {describe(reminder)}."


# "what are my reminders", "what's coming up", "what's next", "list my timers"
_UPCOMING_RE = re.compile(
    r"\b(what('?s| is| are)\s+(my\s+)?(reminders?|timers?|coming up|next|"
    r"on my schedule|scheduled)|any reminders|list (my )?(reminders?|timers?)|"
    r"what do i have (coming|scheduled|planned|going on))\b", re.IGNORECASE)


def is_upcoming_query(text: str) -> bool:
    """True if the user is asking what reminders/timers are coming up. Pure."""
    return bool(_UPCOMING_RE.search(text or ""))


def spoken_upcoming(items, now: dt.datetime) -> str:
    """Turn upcoming() results into a spoken sentence. Pure."""
    if not items:
        return "You have nothing scheduled right now."
    lines = [f"{r.message}, {describe(r).lower()}" for r, _ in items]
    n = len(lines)
    lead = f"You have {n} thing{'s' if n != 1 else ''} coming up: "
    return lead + "; ".join(lines) + "."


# ---------------------------------------------------------------------------
# Store: a tiny JSON-backed list, safe to read/modify from the UI at runtime.
# ---------------------------------------------------------------------------
class ReminderStore:
    """Persists reminders to a JSON file. All mutations write through immediately
    so a crash never loses a just-added reminder."""

    def __init__(self, path):
        self.path = Path(path)
        self._items: List[Reminder] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [Reminder.from_dict(d) for d in raw]
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._items = []   # a corrupt file shouldn't crash the app

    def _save(self):
        self.path.write_text(
            json.dumps([r.to_dict() for r in self._items], indent=2),
            encoding="utf-8")

    def all(self) -> List[Reminder]:
        return list(self._items)

    def enabled(self) -> List[Reminder]:
        return [r for r in self._items if r.enabled]

    def get(self, rid: str) -> Optional[Reminder]:
        return next((r for r in self._items if r.id == rid), None)

    def add(self, reminder: Reminder) -> Reminder:
        self._items.append(reminder)
        self._save()
        return reminder

    def remove(self, rid: str) -> bool:
        before = len(self._items)
        self._items = [r for r in self._items if r.id != rid]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def set_enabled(self, rid: str, on: bool) -> bool:
        r = self.get(rid)
        if r is None:
            return False
        r.enabled = on
        self._save()
        return True

    def mark_fired(self, rid: str, when: float) -> None:
        r = self.get(rid)
        if r is None:
            return
        r.last_fired = when
        if r.kind == ONCE:
            r.enabled = False   # a one-shot never repeats
        self._save()
