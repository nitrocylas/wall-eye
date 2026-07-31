"""Quick-capture notes - a timestamped scratchpad, newest first.

Deliberately dumber than chat memory: memory is what Wall-Eye should *know*;
notes are what YOU jotted down. Same crash-safe JSON pattern as the checklists.
"""
import itertools
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

_ID_SEQ = itertools.count()


@dataclass
class Note:
    text: str
    created: float = 0.0
    id: str = ""

    def __post_init__(self):
        if not self.created:
            self.created = time.time()
        if not self.id:
            self.id = f"n{int(self.created * 1000)}_{next(_ID_SEQ)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class NoteStore:
    def __init__(self, path):
        self.path = Path(path)
        self._items: List[Note] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [Note.from_dict(d) for d in raw]
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._items = []

    def _save(self):
        self.path.write_text(
            json.dumps([n.to_dict() for n in self._items], indent=2),
            encoding="utf-8")

    def all(self) -> List[Note]:
        """Newest first."""
        return sorted(self._items, key=lambda n: n.created, reverse=True)

    def add(self, text: str) -> Optional[Note]:
        text = (text or "").strip()
        if not text:
            return None
        n = Note(text=text)
        self._items.append(n)
        self._save()
        return n

    def remove(self, note_id: str) -> bool:
        before = len(self._items)
        self._items = [n for n in self._items if n.id != note_id]
        if len(self._items) != before:
            self._save()
            return True
        return False
