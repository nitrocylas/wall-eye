"""Unit tests for cleanup tasks, streaks, and exclusions."""
import cleanup
from cleanup import CleanupStore


def _store(tmp_path):
    return CleanupStore(tmp_path / "cleanup.json")


# ---- exclusion matching ----
def test_matches_exact_and_substring():
    assert cleanup.matches_exclusion("clothes on the bed", "clothes on the bed")
    assert cleanup.matches_exclusion("a pile of clothes on the bed", "clothes on bed")


def test_matches_fuzzy_wording():
    # the model words it differently every check
    assert cleanup.matches_exclusion("cables behind the desk", "cable behind desk") \
        or cleanup.matches_exclusion("cables behind the desk", "cables behind the desk")
    assert cleanup.matches_exclusion("boxes stacked on the shelf", "boxes on shelf")


def test_no_match_for_different_things():
    assert not cleanup.matches_exclusion("clothes on the bed", "cups on the desk")
    assert not cleanup.matches_exclusion("", "anything")
    assert not cleanup.matches_exclusion("anything", "")


def test_filter_verdict_strips_items_and_boxes(tmp_path):
    s = _store(tmp_path)
    s.add_exclusion("cables behind the desk")
    verdict = {"alert": True,
               "items": ["clothes on the bed", "cables behind the desk"],
               "regions": [{"label": "clothes on the bed", "box": [0, 0, 1, 1]},
                           {"label": "cables behind the desk", "box": [0, 0, 1, 1]}]}
    removed = s.filter_verdict(verdict)
    assert removed == ["cables behind the desk"]
    assert verdict["items"] == ["clothes on the bed"]
    assert len(verdict["regions"]) == 1
    assert verdict["alert"] is True          # real mess remains


def test_filter_verdict_clears_alert_when_all_excluded(tmp_path):
    s = _store(tmp_path)
    s.add_exclusion("cables behind the desk")
    verdict = {"alert": True, "items": ["cable behind desk"],
               "regions": [{"label": "cable behind desk", "box": [0, 0, 1, 1]}]}
    s.filter_verdict(verdict)
    assert verdict["alert"] is False and verdict["items"] == [] \
        and verdict["regions"] == []


def test_exclusion_add_dedupe_and_remove(tmp_path):
    s = _store(tmp_path)
    assert s.add_exclusion("boxes on shelf") is True
    assert s.add_exclusion("the boxes on the shelf") is False   # same thing
    assert s.add_exclusion("  ") is False
    assert s.remove_exclusion("boxes on shelf") is True
    assert s.remove_exclusion("boxes on shelf") is False


# ---- tasks + streak ----
def test_file_task_and_complete_builds_streak(tmp_path):
    s = _store(tmp_path)
    t = s.file_task(["clothes on the bed"])
    assert s.resolve(t.id, done=True) == 1
    t2 = s.file_task(["cups on the desk"])
    assert s.resolve(t2.id, done=True) == 2
    assert s.best_streak == 2


def test_skip_resets_streak(tmp_path):
    s = _store(tmp_path)
    t = s.file_task(["a"]); s.resolve(t.id, True)
    t2 = s.file_task(["b"])
    assert s.resolve(t2.id, done=False) == 0
    assert s.best_streak == 1


def test_pending_tasks_merge_not_stack(tmp_path):
    s = _store(tmp_path)
    t1 = s.file_task(["clothes on the bed"])
    t2 = s.file_task(["clothes on bed", "cups on the desk"])   # next alert
    assert t1.id == t2.id                       # merged into the pending one
    assert len([x for x in s.tasks() if x.status == "pending"]) == 1
    assert "cups on the desk" in t2.items
    assert len(t2.items) == 2                   # fuzzy-deduped the clothes

def test_resolve_unknown_or_double_is_none(tmp_path):
    s = _store(tmp_path)
    t = s.file_task(["a"])
    assert s.resolve(t.id, True) == 1
    assert s.resolve(t.id, True) is None        # already resolved
    assert s.resolve("nope", True) is None


def test_expire_resets_streak(tmp_path):
    s = _store(tmp_path)
    t = s.file_task(["a"]); s.resolve(t.id, True)
    t2 = s.file_task(["b"])
    t2.created = 0.0                             # ancient
    assert s.expire_stale(now=cleanup.EXPIRE_HOURS * 3600 + 1) is True
    assert s.streak == 0
    assert s.pending() is None


def test_expire_noop_when_fresh(tmp_path):
    s = _store(tmp_path)
    s.file_task(["a"])
    assert s.expire_stale() is False
    assert s.pending() is not None


def test_persistence_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.add_exclusion("cables")
    t = s.file_task(["a"]); s.resolve(t.id, True)
    re = CleanupStore(tmp_path / "cleanup.json")
    assert re.streak == 1 and re.exclusions() == ["cables"]
    assert re.tasks()[0].status == "done"
