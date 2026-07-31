"""Cleanup tasks, the cleanup streak, and the exclusion list.

When the watcher delivers a mess alert it files ONE pending cleanup task (new
items merge into it rather than stacking duplicates every check). The user
answers on the Cleanup tab:

    cleaned it  -> streak + 1
    not yet     -> streak resets to 0
    (ignored for EXPIRE_HOURS) -> streak resets to 0, task auto-dismissed

Exclusions are things the watcher keeps flagging that can't (or shouldn't) be
cleaned - a permanent cable run, a decor pile. Excluded items are stripped from
verdicts deterministically here AND appended to the vision prompt as an ignore
hint. Matching is fuzzy (token overlap) because the model never words an item
exactly the same way twice.
"""
import itertools
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

EXPIRE_HOURS = 24
_ID_SEQ = itertools.count()

# words too generic to identify WHAT the mess is ("clothes ON THE bed")
_STOPWORDS = {"a", "an", "the", "on", "in", "of", "and", "some", "items",
              "item", "stuff", "pile", "piled", "resting", "lying", "left",
              "with", "at", "near", "by", "to"}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _STOPWORDS}


def matches_exclusion(item: str, exclusion: str) -> bool:
    """True if a detected item is 'the same thing' as an exclusion. Pure.

    Match = one phrase contains the other (normalized), or their meaningful
    words overlap strongly (>= 60% of the smaller side)."""
    a, b = str(item or "").lower().strip(), str(exclusion or "").lower().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return overlap >= 0.6


def is_excluded(item: str, exclusions) -> bool:
    return any(matches_exclusion(item, e) for e in exclusions or [])


@dataclass
class CleanupTask:
    items: List[str]
    created: float = 0.0
    status: str = "pending"          # pending | done | skipped | expired
    resolved: float = 0.0
    id: str = ""

    def __post_init__(self):
        if not self.created:
            self.created = time.time()
        if not self.id:
            self.id = f"c{int(self.created * 1000)}_{next(_ID_SEQ)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CleanupTask":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class CleanupStore:
    """Persists tasks + exclusions + the streak in one JSON file."""

    def __init__(self, path):
        self.path = Path(path)
        self.streak = 0
        self.best_streak = 0
        self._exclusions: List[str] = []
        self._tasks: List[CleanupTask] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.streak = int(raw.get("streak", 0))
            self.best_streak = int(raw.get("best_streak", 0))
            self._exclusions = [str(x) for x in raw.get("exclusions", [])]
            self._tasks = [CleanupTask.from_dict(d) for d in raw.get("tasks", [])]
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            pass   # corrupt file -> fresh state, never crash the app

    def _save(self):
        self.path.write_text(json.dumps({
            "streak": self.streak, "best_streak": self.best_streak,
            "exclusions": self._exclusions,
            "tasks": [t.to_dict() for t in self._tasks[-100:]],  # keep history light
        }, indent=2), encoding="utf-8")

    # ---- exclusions ----
    def exclusions(self) -> List[str]:
        return list(self._exclusions)

    def add_exclusion(self, text: str) -> bool:
        text = (text or "").strip()
        if not text or is_excluded(text, self._exclusions):
            return False
        self._exclusions.append(text)
        self._save()
        return True

    def remove_exclusion(self, text: str) -> bool:
        if text in self._exclusions:
            self._exclusions.remove(text)
            self._save()
            return True
        return False

    def filter_verdict(self, verdict: dict) -> List[str]:
        """Strip excluded items (and their boxes) out of a vision verdict, in
        place. If nothing actionable remains the alert is cleared. Returns the
        removed item texts."""
        items = verdict.get("items") or []
        removed = [i for i in items if is_excluded(i, self._exclusions)]
        if not removed:
            return []
        verdict["items"] = [i for i in items if i not in removed]
        verdict["regions"] = [
            r for r in (verdict.get("regions") or [])
            if not is_excluded(r.get("label", ""), self._exclusions)]
        if not verdict["items"]:
            verdict["alert"] = False
            verdict["regions"] = []
        return removed

    # ---- tasks ----
    def pending(self) -> Optional[CleanupTask]:
        return next((t for t in self._tasks if t.status == "pending"), None)

    def tasks(self) -> List[CleanupTask]:
        return list(self._tasks)

    def file_task(self, items) -> CleanupTask:
        """File a cleanup task for an alert. If one is already pending, merge
        the new items into it instead of stacking duplicates."""
        items = [i for i in (items or []) if str(i).strip()]
        cur = self.pending()
        if cur is not None:
            for i in items:
                if not any(matches_exclusion(i, have) for have in cur.items):
                    cur.items.append(i)
            self._save()
            return cur
        t = CleanupTask(items=items or ["tidy the room"])
        self._tasks.append(t)
        self._save()
        return t

    def resolve(self, task_id: str, done: bool) -> Optional[int]:
        """Mark a task cleaned or not. Returns the new streak, or None if it wasn't
        pending (double-clicks are harmless)."""
        t = next((x for x in self._tasks
                  if x.id == task_id and x.status == "pending"), None)
        if t is None:
            return None
        t.status = "done" if done else "skipped"
        t.resolved = time.time()
        if done:
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
        else:
            self.streak = 0
        self._save()
        return self.streak

    def expire_stale(self, now: float = None) -> bool:
        """A pending task ignored for EXPIRE_HOURS counts as 'didn't clean' -
        the task expires and the streak resets. Returns True if that happened."""
        now = now if now is not None else time.time()
        t = self.pending()
        if t is None or (now - t.created) < EXPIRE_HOURS * 3600:
            return False
        t.status = "expired"
        t.resolved = now
        self.streak = 0
        self._save()
        return True
