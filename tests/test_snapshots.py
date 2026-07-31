"""Unit tests for snapshots.py - index parsing, pruning, and path containment."""
import json

from snapshots import SnapshotStore


def test_save_and_read_roundtrip(tmp_path):
    store = SnapshotStore(tmp_path)
    rec = store.save("room", b"jpegbytes", True, ["clothes on bed"], [])
    assert (tmp_path / rec["file"]).read_bytes() == b"jpegbytes"

    reopened = SnapshotStore(tmp_path)
    assert [r["file"] for r in reopened.all()] == [rec["file"]]
    assert reopened.all()[0]["items"] == ["clothes on bed"]


def test_read_index_skips_corrupt_lines_and_missing_files(tmp_path):
    (tmp_path / "ok.jpg").write_bytes(b"x")
    index = tmp_path / "index.jsonl"
    index.write_text(
        "not json\n"
        + json.dumps({"file": "gone.jpg", "time": 1}) + "\n"
        + json.dumps({"file": "ok.jpg", "time": 2}) + "\n",
        encoding="utf-8")
    store = SnapshotStore(tmp_path)
    assert [r["file"] for r in store.all()] == ["ok.jpg"]


def test_read_index_rejects_records_escaping_snapshot_dir(tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do not touch")
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    (snap_dir / "index.jsonl").write_text(
        json.dumps({"file": "../outside.jpg", "time": 1}) + "\n"
        + json.dumps({"file": "", "time": 2}) + "\n",
        encoding="utf-8")
    store = SnapshotStore(snap_dir)
    assert store.all() == []


def test_prune_never_deletes_outside_snapshot_dir(tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do not touch")
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    (snap_dir / "index.jsonl").write_text(
        json.dumps({"file": "../outside.jpg", "time": 1}) + "\n",
        encoding="utf-8")
    store = SnapshotStore(snap_dir, max_items=2)
    for i in range(5):   # force pruning well past max_items
        store.save("room", b"x", False, [], [], when=100 + i)
    assert outside.exists()
    assert len(store.all()) == 2


def test_prune_caps_on_disk_files(tmp_path):
    store = SnapshotStore(tmp_path, max_items=3)
    for i in range(6):
        store.save("room", b"x", False, [], [], when=100 + i)
    jpgs = list(tmp_path.glob("*.jpg"))
    assert len(jpgs) == 3
    assert len(store.all()) == 3
