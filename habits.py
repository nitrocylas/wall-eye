"""Daily habits with streaks - check off each day, keep the flame alive.

A habit stores the ISO dates it was completed. The streak counts consecutive
days ending today (or yesterday - missing *part* of today doesn't break it
until the day is actually over). Pure streak math is unit-tested.
"""
import datetime as dt
import itertools
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

_ID_SEQ = itertools.count()


@dataclass
class Habit:
    name: str
    days: List[str] = field(default_factory=list)   # ISO dates completed
    created: float = 0.0
    id: str = ""

    def __post_init__(self):
        if not self.created:
            self.created = time.time()
        if not self.id:
            self.id = f"h{int(self.created * 1000)}_{next(_ID_SEQ)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Habit":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def streak(days, today: dt.date) -> int:
    """Consecutive completed days ending today or yesterday. Pure."""
    ds = set(days)
    if today.isoformat() in ds:
        cur = today
    elif (today - dt.timedelta(days=1)).isoformat() in ds:
        cur = today - dt.timedelta(days=1)
    else:
        return 0
    n = 0
    while cur.isoformat() in ds:
        n += 1
        cur -= dt.timedelta(days=1)
    return n


class HabitStore:
    def __init__(self, path):
        self.path = Path(path)
        self._items: List[Habit] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [Habit.from_dict(d) for d in raw]
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._items = []

    def _save(self):
        self.path.write_text(
            json.dumps([h.to_dict() for h in self._items], indent=2),
            encoding="utf-8")

    def all(self) -> List[Habit]:
        return list(self._items)

    def add(self, name: str) -> Optional[Habit]:
        name = (name or "").strip()
        if not name:
            return None
        h = Habit(name=name)
        self._items.append(h)
        self._save()
        return h

    def remove(self, habit_id: str) -> bool:
        before = len(self._items)
        self._items = [h for h in self._items if h.id != habit_id]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def set_done(self, habit_id: str, day: dt.date, done: bool) -> bool:
        """Mark/unmark a habit for `day`. Returns the new done-state."""
        iso = day.isoformat()
        for h in self._items:
            if h.id == habit_id:
                if done and iso not in h.days:
                    h.days.append(iso)
                elif not done and iso in h.days:
                    h.days.remove(iso)
                self._save()
                return iso in h.days
        return False

    def done_today(self, habit_id: str, today: dt.date) -> bool:
        h = next((x for x in self._items if x.id == habit_id), None)
        return bool(h and today.isoformat() in h.days)
