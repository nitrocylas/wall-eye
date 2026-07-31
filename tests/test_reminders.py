"""Unit tests for the reminder recurrence math, NL parser, and JSON store."""
import datetime as dt

import reminders
from reminders import Reminder


def _at(y, mo, d, hh, mm):
    return dt.datetime(y, mo, d, hh, mm)


# ---------------------------------------------------------------------------
# next_fire / is_due
# ---------------------------------------------------------------------------
def test_daily_next_fire_is_today_when_time_still_ahead():
    r = Reminder(message="x", kind=reminders.DAILY, time="16:00")
    now = _at(2026, 7, 8, 10, 0)         # 10am, before 4pm
    assert reminders.next_fire(r, now) == _at(2026, 7, 8, 16, 0)


def test_daily_next_fire_rolls_to_tomorrow_after_time_passed():
    r = Reminder(message="x", kind=reminders.DAILY, time="16:00")
    now = _at(2026, 7, 8, 17, 0)         # 5pm, already past 4pm
    assert reminders.next_fire(r, now) == _at(2026, 7, 9, 16, 0)


def test_daily_is_due_exactly_at_time():
    r = Reminder(message="x", kind=reminders.DAILY, time="16:00")
    r.last_fired = _at(2026, 7, 8, 16, 0).timestamp()   # fired yesterday's slot
    # next day, 4pm reached
    assert reminders.is_due(r, _at(2026, 7, 9, 16, 0)) is True
    # one minute before -> not yet
    assert reminders.is_due(r, _at(2026, 7, 9, 15, 59)) is False


def test_daily_does_not_double_fire_same_day():
    r = Reminder(message="x", kind=reminders.DAILY, time="16:00")
    fired = _at(2026, 7, 8, 16, 0)
    r.last_fired = fired.timestamp()
    # ten seconds later it must NOT be due again
    assert reminders.is_due(r, fired + dt.timedelta(seconds=10)) is False


def test_interval_fires_one_step_after_last():
    r = Reminder(message="x", kind=reminders.INTERVAL, every_minutes=30)
    last = _at(2026, 7, 8, 12, 0)
    r.last_fired = last.timestamp()
    assert reminders.is_due(r, _at(2026, 7, 8, 12, 29)) is False
    assert reminders.is_due(r, _at(2026, 7, 8, 12, 30)) is True


def test_interval_first_fire_measured_from_creation():
    created = _at(2026, 7, 8, 12, 0)
    r = Reminder(message="x", kind=reminders.INTERVAL, every_minutes=15,
                 created=created.timestamp())
    assert reminders.is_due(r, _at(2026, 7, 8, 12, 14)) is False
    assert reminders.is_due(r, _at(2026, 7, 8, 12, 15)) is True


def test_weekdays_skips_the_weekend():
    r = Reminder(message="x", kind=reminders.WEEKDAYS, time="09:00")
    friday_6pm = _at(2026, 7, 10, 18, 0)     # Fri 2026-07-10
    nxt = reminders.next_fire(r, friday_6pm)
    assert nxt == _at(2026, 7, 13, 9, 0)      # Monday, not Sat/Sun


def test_weekly_picks_next_listed_day():
    # days = Wed(2), Fri(4); from a Tuesday, next is Wednesday
    r = Reminder(message="x", kind=reminders.WEEKLY, time="18:00", days=[2, 4])
    tuesday = _at(2026, 7, 7, 12, 0)          # 2026-07-07 is a Tuesday
    assert reminders.next_fire(r, tuesday) == _at(2026, 7, 8, 18, 0)  # Wed


def test_once_fires_then_never_again():
    # created is pinned BEFORE the target so the test doesn't depend on the
    # real wall clock.
    r = Reminder(message="x", kind=reminders.ONCE, date="2026-07-08", time="16:00",
                 created=_at(2026, 7, 8, 10, 0).timestamp())
    before = _at(2026, 7, 8, 15, 0)
    assert reminders.is_due(r, before) is False
    assert reminders.is_due(r, _at(2026, 7, 8, 16, 0)) is True
    # after it has fired, next_fire from that point is None
    assert reminders.next_fire(r, _at(2026, 7, 8, 16, 0)) is None


def test_disabled_reminder_never_due():
    r = Reminder(message="x", kind=reminders.DAILY, time="00:00", enabled=False)
    assert reminders.is_due(r, _at(2026, 7, 8, 12, 0)) is False


def test_catches_up_only_once_after_downtime():
    # daily at 9am, last fired 3 days ago -> due now, but only a single fire
    r = Reminder(message="x", kind=reminders.DAILY, time="09:00")
    r.last_fired = _at(2026, 7, 5, 9, 0).timestamp()
    now = _at(2026, 7, 8, 10, 0)
    assert reminders.is_due(r, now) is True
    # simulate the fire, then it should be quiet until tomorrow 9am
    r.last_fired = now.timestamp()
    assert reminders.is_due(r, now + dt.timedelta(minutes=1)) is False


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------
def test_describe_daily():
    assert reminders.describe(
        Reminder(message="x", kind=reminders.DAILY, time="16:00")) == "Every day at 4:00 PM"


def test_describe_interval_minutes_and_hours():
    assert reminders.describe(
        Reminder(message="x", kind=reminders.INTERVAL, every_minutes=30)) == "Every 30 minutes"
    assert reminders.describe(
        Reminder(message="x", kind=reminders.INTERVAL, every_minutes=120)) == "Every 2 hours"


def test_describe_weekdays_and_weekly():
    assert reminders.describe(
        Reminder(message="x", kind=reminders.WEEKDAYS, time="09:00")) == "Weekdays at 9:00 AM"
    assert reminders.describe(
        Reminder(message="x", kind=reminders.WEEKLY, time="18:00", days=[0, 2, 4])
    ) == "Mon, Wed, Fri at 6:00 PM"


# ---------------------------------------------------------------------------
# parse_reminder
# ---------------------------------------------------------------------------
NOW = _at(2026, 7, 8, 12, 0)     # Wed 2026-07-08, noon


def test_parse_daily_at_time():
    r = reminders.parse_reminder("remind me to water the plants every day at 6pm", NOW)
    assert r is not None
    assert r.kind == reminders.DAILY and r.time == "18:00"
    assert r.message == "water the plants"


def test_parse_interval():
    r = reminders.parse_reminder("remind me to stretch every 30 minutes", NOW)
    assert r.kind == reminders.INTERVAL and r.every_minutes == 30
    assert r.message == "stretch"


def test_parse_interval_hours():
    r = reminders.parse_reminder("remind me to drink water every 2 hours", NOW)
    assert r.kind == reminders.INTERVAL and r.every_minutes == 120


def test_parse_in_minutes_is_one_shot():
    r = reminders.parse_reminder("remind me to check the oven in 20 minutes", NOW)
    assert r.kind == reminders.ONCE
    assert r.date == "2026-07-08" and r.time == "12:20"
    assert r.message == "check the oven"


def test_parse_weekdays():
    r = reminders.parse_reminder("remind me to take meds every weekday at 9am", NOW)
    assert r.kind == reminders.WEEKDAYS and r.time == "09:00"


def test_parse_every_named_day_is_weekly():
    r = reminders.parse_reminder("remind me to take out trash every monday at 8am", NOW)
    assert r.kind == reminders.WEEKLY and r.days == [0] and r.time == "08:00"


def test_parse_bare_at_time_is_one_shot_next_occurrence():
    # noon now, "at 4pm" -> today 4pm one-shot
    r = reminders.parse_reminder("remind me to call mom at 4pm", NOW)
    assert r.kind == reminders.ONCE and r.date == "2026-07-08" and r.time == "16:00"


def test_parse_at_time_already_passed_rolls_to_tomorrow():
    r = reminders.parse_reminder("remind me to call mom at 9am", NOW)  # 9am < noon
    assert r.kind == reminders.ONCE and r.date == "2026-07-09"


def test_parse_tomorrow():
    r = reminders.parse_reminder("remind me to submit the form tomorrow at 10am", NOW)
    assert r.kind == reminders.ONCE and r.date == "2026-07-09" and r.time == "10:00"


def test_non_reminder_returns_none():
    assert reminders.parse_reminder("what's the weather like", NOW) is None
    assert reminders.parse_reminder("remember my name is sam", NOW) is None


# ---------------------------------------------------------------------------
# timers
# ---------------------------------------------------------------------------
def test_parse_timer_minutes():
    r = reminders.parse_timer("set a timer for 10 minutes", NOW)
    assert r is not None and r.kind == reminders.ONCE
    assert r.date == "2026-07-08" and r.time == "12:10"


def test_parse_timer_hours():
    r = reminders.parse_timer("timer for 2 hours", NOW)
    assert r.date == "2026-07-08" and r.time == "14:00"


def test_parse_timer_with_label():
    r = reminders.parse_timer("set a timer for 5 minutes for tea", NOW)
    assert "Tea" in r.message and r.time == "12:05"


def test_parse_timer_seconds():
    r = reminders.parse_timer("set a timer for 90 seconds", NOW)
    assert r.time == "12:01"     # 90s -> 12:01:30 truncates to the minute field


def test_parse_timer_rejects_non_timer():
    assert reminders.parse_timer("remind me to nap", NOW) is None
    assert reminders.parse_timer("set a timer", NOW) is None   # no duration


def test_parse_request_prefers_timer():
    r = reminders.parse_request("set a timer for 10 minutes", NOW)
    assert r.kind == reminders.ONCE and r.time == "12:10"


def test_parse_request_falls_back_to_reminder():
    r = reminders.parse_request("remind me to stretch every 30 minutes", NOW)
    assert r.kind == reminders.INTERVAL


def test_confirmation_timer_vs_reminder():
    r = reminders.parse_reminder("remind me to nap every day at 2pm", NOW)
    assert "remind you to nap" in reminders.confirmation(r, was_timer=False)
    assert "timer set" in reminders.confirmation(r, was_timer=True).lower()


# ---------------------------------------------------------------------------
# upcoming / schedule read-back
# ---------------------------------------------------------------------------
def test_upcoming_sorted_by_next_fire():
    a = Reminder(message="soon", kind=reminders.DAILY, time="13:00")     # 1pm
    b = Reminder(message="later", kind=reminders.DAILY, time="18:00")    # 6pm
    c = Reminder(message="off", kind=reminders.DAILY, time="14:00", enabled=False)
    items = reminders.upcoming([b, a, c], NOW)      # noon
    assert [r.message for r, _ in items] == ["soon", "later"]   # disabled dropped


def test_upcoming_limit():
    rs = [Reminder(message=f"r{i}", kind=reminders.DAILY, time=f"{13+i:02d}:00")
          for i in range(4)]
    assert len(reminders.upcoming(rs, NOW, limit=2)) == 2


def test_is_upcoming_query():
    assert reminders.is_upcoming_query("what are my reminders?") is True
    assert reminders.is_upcoming_query("what's coming up") is True
    assert reminders.is_upcoming_query("list my timers") is True
    assert reminders.is_upcoming_query("remind me to nap at 4pm") is False


def test_spoken_upcoming():
    a = Reminder(message="water the plants", kind=reminders.DAILY, time="18:00")
    out = reminders.spoken_upcoming(reminders.upcoming([a], NOW), NOW)
    assert "water the plants" in out and "1 thing" in out
    assert reminders.spoken_upcoming([], NOW).startswith("You have nothing")


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def test_store_add_persists(tmp_path):
    p = tmp_path / "reminders.json"
    store = reminders.ReminderStore(p)
    r = store.add(Reminder(message="feed cat", kind=reminders.DAILY, time="08:00"))
    assert p.exists()
    reloaded = reminders.ReminderStore(p)
    assert len(reloaded.all()) == 1
    assert reloaded.get(r.id).message == "feed cat"


def test_store_remove(tmp_path):
    store = reminders.ReminderStore(tmp_path / "r.json")
    r = store.add(Reminder(message="x"))
    assert store.remove(r.id) is True
    assert store.all() == []
    assert store.remove("nope") is False


def test_store_mark_fired_disables_one_shot(tmp_path):
    store = reminders.ReminderStore(tmp_path / "r.json")
    r = store.add(Reminder(message="x", kind=reminders.ONCE,
                           date="2026-07-08", time="16:00"))
    store.mark_fired(r.id, NOW.timestamp())
    got = store.get(r.id)
    assert got.last_fired == NOW.timestamp()
    assert got.enabled is False   # one-shots retire themselves


def test_store_mark_fired_keeps_recurring_enabled(tmp_path):
    store = reminders.ReminderStore(tmp_path / "r.json")
    r = store.add(Reminder(message="x", kind=reminders.DAILY, time="08:00"))
    store.mark_fired(r.id, NOW.timestamp())
    assert store.get(r.id).enabled is True


def test_store_corrupt_file_is_ignored(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{ not json", encoding="utf-8")
    store = reminders.ReminderStore(p)   # must not raise
    assert store.all() == []


def test_unique_ids(tmp_path):
    store = reminders.ReminderStore(tmp_path / "r.json")
    a = store.add(Reminder(message="a", created=1000.0))
    b = store.add(Reminder(message="b", created=2000.0))
    assert a.id != b.id
