"""A tiny JSON-backed checklist - powers the To-Do and Shopping tabs.

One ChecklistStore = one named list (its own file). Items are dicts with text,
a done flag and a created timestamp. All mutations write through immediately so
nothing is lost on a crash. Pure/deterministic apart from the file I/O, so the
ordering and toggling logic is unit-tested.
"""
import itertools
import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Optional

_ID_SEQ = itertools.count()   # guarantees unique ids even within the same ms


@dataclass
class Item:
    text: str
    done: bool = False
    created: float = 0.0
    id: str = ""

    def __post_init__(self):
        if not self.created:
            self.created = time.time()
        if not self.id:
            self.id = f"i{int(self.created * 1000)}_{next(_ID_SEQ)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class ChecklistStore:
    def __init__(self, path):
        self.path = Path(path)
        self._items: List[Item] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [Item.from_dict(d) for d in raw]
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._items = []

    def _save(self):
        self.path.write_text(
            json.dumps([i.to_dict() for i in self._items], indent=2),
            encoding="utf-8")

    def all(self) -> List[Item]:
        """Open items first (oldest first), then completed ones."""
        active = [i for i in self._items if not i.done]
        done = [i for i in self._items if i.done]
        return active + done

    def add(self, text: str) -> Optional[Item]:
        text = (text or "").strip()
        if not text:
            return None
        item = Item(text=text)
        self._items.append(item)
        self._save()
        return item

    def toggle(self, item_id: str) -> bool:
        for i in self._items:
            if i.id == item_id:
                i.done = not i.done
                self._save()
                return i.done
        return False

    def remove(self, item_id: str) -> bool:
        before = len(self._items)
        self._items = [i for i in self._items if i.id != item_id]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def clear_done(self) -> int:
        before = len(self._items)
        self._items = [i for i in self._items if not i.done]
        removed = before - len(self._items)
        if removed:
            self._save()
        return removed

    def counts(self) -> tuple:
        """(open, done) counts - for the briefing / tab badges."""
        done = sum(1 for i in self._items if i.done)
        return (len(self._items) - done, done)
