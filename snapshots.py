"""Rolling on-disk history of room-check snapshots - powers the timeline strip.

Every check saves its frame (jpeg) plus a metadata record (verdict, items,
boxes) to an append-only jsonl index. The store prunes itself to MAX_SNAPSHOTS
so ~4 days of half-hourly checks never grows unbounded. Pure logic (index
parsing, ordering, pruning) is unit-tested with tmp dirs.
"""
import json
import time
from pathlib import Path

MAX_SNAPSHOTS = 200


class SnapshotStore:
    def __init__(self, root, max_items: int = MAX_SNAPSHOTS):
        self.dir = Path(root)
        self.dir.mkdir(exist_ok=True)
        self.index = self.dir / "index.jsonl"
        self.max_items = max_items
        self._items = self._read_index()   # cached in memory; the file is only
                                           # re-read once, at startup

    def _read_index(self):
        """Parse the index file once (skip corrupt lines / missing images).

        Records whose filename resolves outside the snapshot directory are
        rejected: pruning unlinks these paths later, so a doctored index
        line like "../x" must never reach the delete loop.
        """
        if not self.index.exists():
            return []
        recs = []
        root = self.dir.resolve()
        for line in self.index.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = (self.dir / str(rec.get("file", ""))).resolve()
            if path.parent == root and path.exists():
                recs.append(rec)
        recs.sort(key=lambda r: r.get("time", 0))   # chronological in memory
        return recs

    def save(self, task: str, jpeg: bytes, alert: bool, items, regions,
             when: float = None) -> dict:
        ts = when if when is not None else time.time()
        name = f"snap_{int(ts * 1000)}.jpg"
        (self.dir / name).write_bytes(jpeg)
        rec = {"file": name, "time": ts, "task": task, "alert": bool(alert),
               "items": list(items or []), "regions": list(regions or [])}
        with open(self.index, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self._items.append(rec)
        self._prune()
        return rec

    def all(self):
        """Records newest-first (served from the in-memory cache)."""
        return list(reversed(self._items))

    def latest(self, n: int):
        return list(reversed(self._items[-n:]))

    def path(self, rec: dict) -> Path:
        return self.dir / rec["file"]

    def _prune(self):
        if len(self._items) <= self.max_items:
            return
        excess = self._items[:-self.max_items]
        for r in excess:
            (self.dir / r["file"]).unlink(missing_ok=True)
        self._items = self._items[-self.max_items:]
        with open(self.index, "w", encoding="utf-8") as f:   # rewrite compacted
            for r in self._items:
                f.write(json.dumps(r) + "\n")
