"""Unit tests for the checklist store and briefing helpers."""
import datetime as dt

import briefing
from checklist import ChecklistStore, Item


def test_add_and_persist(tmp_path):
    s = ChecklistStore(tmp_path / "todo.json")
    it = s.add("buy milk")
    assert it is not None and it.text == "buy milk" and it.done is False
    assert ChecklistStore(tmp_path / "todo.json").all()[0].text == "buy milk"


def test_add_blank_ignored(tmp_path):
    s = ChecklistStore(tmp_path / "t.json")
    assert s.add("   ") is None
    assert s.all() == []


def test_toggle_and_ordering(tmp_path):
    s = ChecklistStore(tmp_path / "t.json")
    a = s.add("a"); b = s.add("b"); c = s.add("c")
    s.toggle(a.id)                       # a done -> moves to bottom
    order = [i.text for i in s.all()]
    assert order == ["b", "c", "a"]
    assert s.all()[-1].done is True


def test_remove_and_clear_done(tmp_path):
    s = ChecklistStore(tmp_path / "t.json")
    a = s.add("a"); b = s.add("b")
    assert s.remove(a.id) is True
    assert s.remove("nope") is False
    s.toggle(b.id)
    assert s.clear_done() == 1
    assert s.all() == []


def test_counts(tmp_path):
    s = ChecklistStore(tmp_path / "t.json")
    a = s.add("a"); s.add("b"); s.add("c")
    s.toggle(a.id)
    assert s.counts() == (2, 1)          # (open, done)


def test_corrupt_file_ignored(tmp_path):
    p = tmp_path / "t.json"
    p.write_text("{bad", encoding="utf-8")
    assert ChecklistStore(p).all() == []


# ---- briefing ----
def test_greeting_by_hour():
    assert briefing.greeting_for_hour(8) == "Good morning"
    assert briefing.greeting_for_hour(14) == "Good afternoon"
    assert briefing.greeting_for_hour(19) == "Good evening"
    assert briefing.greeting_for_hour(2) == "Good evening"


def test_date_and_clock_lines():
    when = dt.datetime(2026, 7, 8, 8, 42)
    assert briefing.date_line(when) == "Wednesday, July 8"
    assert briefing.clock_line(when) == "8:42"
    assert briefing.ampm(when) == "AM"


def test_summarize_day():
    assert "2 reminders" in briefing.summarize_day(2, 0, None)
    assert "3 to-dos" in briefing.summarize_day(0, 3, None)
    assert "tidy" in briefing.summarize_day(0, 0, False)
    assert briefing.summarize_day(0, 0, None).startswith("Nothing")
