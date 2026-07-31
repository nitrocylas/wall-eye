"""Unit tests for multicam.py - the pure multi-camera helpers."""
import pytest

import multicam


CAMS = {"main": {"source": 0}, "side": {"source": 1, "description": "desk view"}}


def v(alert=False, items=None, summary="", confidence="low",
      regions=None, vetoed=None):
    return {"alert": alert, "items": items or [], "summary": summary,
            "confidence": confidence, "regions": regions or [],
            "vetoed": vetoed or []}


# ---------- camera_names ----------

def test_camera_names_default_is_first_configured():
    assert multicam.camera_names({"name": "room"}, CAMS) == ["main"]


def test_camera_names_string():
    assert multicam.camera_names({"camera": "side"}, CAMS) == ["side"]


def test_camera_names_list_keeps_order():
    task = {"camera": ["side", "main"]}
    assert multicam.camera_names(task, CAMS) == ["side", "main"]


def test_camera_names_dedupes():
    task = {"camera": ["main", "main", "side"]}
    assert multicam.camera_names(task, CAMS) == ["main", "side"]


def test_camera_names_unknown_raises():
    with pytest.raises(ValueError, match="deskcam"):
        multicam.camera_names({"name": "room", "camera": "deskcam"}, CAMS)


def test_camera_names_empty_list_raises():
    with pytest.raises(ValueError, match="empty camera list"):
        multicam.camera_names({"name": "room", "camera": []}, CAMS)


def test_camera_names_no_cameras_configured_raises():
    with pytest.raises(ValueError, match="no cameras"):
        multicam.camera_names({"name": "room"}, {})


# ---------- ref_path / save_ref_path ----------

def test_ref_path_prefers_per_camera_file(tmp_path):
    (tmp_path / "room__main.jpg").write_bytes(b"x")
    (tmp_path / "room.jpg").write_bytes(b"y")
    p = multicam.ref_path(tmp_path, "room", "main", "main")
    assert p.name == "room__main.jpg"


def test_ref_path_primary_falls_back_to_legacy(tmp_path):
    (tmp_path / "room.jpg").write_bytes(b"y")
    p = multicam.ref_path(tmp_path, "room", "main", "main")
    assert p.name == "room.jpg"


def test_ref_path_secondary_never_uses_legacy(tmp_path):
    (tmp_path / "room.jpg").write_bytes(b"y")
    p = multicam.ref_path(tmp_path, "room", "side", "main")
    assert p.name == "room__side.jpg"          # doesn't exist -> per-cam path
    assert not p.exists()


def test_save_ref_path_is_always_per_camera(tmp_path):
    p = multicam.save_ref_path(tmp_path, "room", "side")
    assert p.name == "room__side.jpg"


# ---------- safe_name / path containment ----------

def test_safe_name_passes_normal_names_through():
    assert multicam.safe_name("room") == "room"
    assert multicam.safe_name("dog-couch") == "dog-couch"
    assert multicam.safe_name("desk cam 2") == "desk cam 2"


def test_safe_name_neutralises_path_separators_and_dots():
    assert "/" not in multicam.safe_name("../../x")
    assert "\\" not in multicam.safe_name("..\\..\\x")
    assert not multicam.safe_name("../../x").startswith(".")
    assert multicam.safe_name("...") == "unnamed"
    assert multicam.safe_name("") == "unnamed"


def test_safe_name_strips_windows_reserved_characters():
    assert multicam.safe_name('a:b*c?"d<e>f|g') == "a_b_c__d_e_f_g"


def test_ref_paths_stay_inside_refs_dir(tmp_path):
    for task, cam in [("../../evil", "main"), ("room", "..\\..\\cam"),
                      ("..", "..")]:
        for p in (multicam.ref_path(tmp_path, task, cam, cam),
                  multicam.save_ref_path(tmp_path, task, cam)):
            assert p.resolve().parent == tmp_path.resolve()


# ---------- angle_prompt ----------

def test_angle_prompt_prepends_description():
    out = multicam.angle_prompt("Check the room.", {"description": "overhead"})
    assert out.startswith("Camera viewpoint: overhead.")
    assert out.endswith("Check the room.")


def test_angle_prompt_without_description_is_unchanged():
    assert multicam.angle_prompt("Check.", {"source": 0}) == "Check."
    assert multicam.angle_prompt("Check.", None) == "Check."
    assert multicam.angle_prompt("Check.", {"description": "  "}) == "Check."


# ---------- merge_verdicts ----------

def test_merge_empty_raises():
    with pytest.raises(ValueError):
        multicam.merge_verdicts([])


def test_merge_single_camera_passes_through_untouched():
    verdict = v(alert=True, items=["clothes on bed"], summary="Messy.",
                confidence="high", regions=[{"box": [1, 2, 3, 4]}])
    merged = multicam.merge_verdicts([("main", verdict)])
    assert merged == verdict
    assert merged is not verdict           # a copy, not the same dict


def test_merge_alert_if_any_camera_alerts():
    merged = multicam.merge_verdicts([
        ("main", v(alert=False)), ("side", v(alert=True))])
    assert merged["alert"] is True


def test_merge_all_clear():
    merged = multicam.merge_verdicts([
        ("main", v()), ("side", v())])
    assert merged["alert"] is False
    assert merged["items"] == []


def test_merge_items_get_camera_prefix_and_dedupe():
    merged = multicam.merge_verdicts([
        ("main", v(items=["clothes on bed", "clothes on bed"])),
        ("side", v(items=["boxes on chair"]))])
    assert merged["items"] == ["main: clothes on bed", "side: boxes on chair"]


def test_merge_summary_tags_cameras_and_skips_blank():
    merged = multicam.merge_verdicts([
        ("main", v(summary="Bed is messy.")),
        ("side", v(summary="  "))])
    assert merged["summary"] == "[main] Bed is messy."


def test_merge_confidence_prefers_alerting_cameras():
    merged = multicam.merge_verdicts([
        ("main", v(alert=False, confidence="high")),
        ("side", v(alert=True, confidence="medium"))])
    assert merged["confidence"] == "medium"    # side's, not the clear cam's


def test_merge_confidence_max_when_all_clear():
    merged = multicam.merge_verdicts([
        ("main", v(confidence="low")), ("side", v(confidence="medium"))])
    assert merged["confidence"] == "medium"


def test_merge_unknown_confidence_treated_as_low():
    merged = multicam.merge_verdicts([
        ("main", v(alert=True, confidence="banana")),
        ("side", v(alert=True, confidence="medium"))])
    assert merged["confidence"] == "medium"


def test_merge_drops_regions_but_keeps_per_camera():
    verdict = v(alert=True, regions=[{"box": [1, 2, 3, 4]}])
    merged = multicam.merge_verdicts([("main", verdict), ("side", v())])
    assert merged["regions"] == []
    assert merged["per_camera"]["main"]["regions"] == [{"box": [1, 2, 3, 4]}]


def test_merge_collects_vetoed():
    merged = multicam.merge_verdicts([
        ("main", v(vetoed=["rug"])), ("side", v(vetoed=["curtain"]))])
    assert merged["vetoed"] == ["rug", "curtain"]
