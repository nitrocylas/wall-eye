"""Multi-camera support - pure helpers for tasks that watch several angles.

A task's `camera:` field may be a single name ("main"), a list of names
(["overhead", "desk"]), or missing (defaults to the first configured
camera). Each camera grabs its own frame, keeps its own reference photo,
and may carry a `description:` of its viewpoint that is prepended to the
vision prompt so the model knows what angle it is looking at. The
per-camera verdicts are merged into one task-level verdict for alerting.

Everything here is pure (no camera/Ollama/disk-write I/O) so it is
unit-testable; app.py owns the side effects.
"""
import re
from pathlib import Path
from typing import Optional

# vision.check() confidence values, weakest first (index = rank)
CONFIDENCE_ORDER = ("low", "medium", "high")

# Characters that must never appear in a filename component (path separators,
# Windows-reserved punctuation, control characters).
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name) -> str:
    """Reduce a task/camera name to a single safe filename component.

    Names come from the local config.yaml, so this guards against footguns
    rather than attackers: a name like "../../x" would otherwise make the
    reference-photo paths escape refs/. Path separators and reserved
    characters become "_", and leading/trailing dots (which could rebuild a
    ".." component) are stripped.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", str(name)).strip(". ")
    return cleaned or "unnamed"


def camera_names(task: dict, cameras: dict) -> list:
    """Resolve which configured cameras a task watches, in order.

    Raises ValueError (not a silent skip) on an unknown name or empty
    config - a typo'd camera name must not quietly disable the watch.
    """
    if not cameras:
        raise ValueError("no cameras configured in config.yaml")
    spec = task.get("camera")
    if spec is None:
        return [next(iter(cameras))]
    names = [spec] if isinstance(spec, str) else list(spec)
    if not names:
        raise ValueError(
            f"task {task.get('name')!r} has an empty camera list")
    unknown = [n for n in names if n not in cameras]
    if unknown:
        raise ValueError(
            f"task {task.get('name')!r} names unknown camera(s) "
            f"{', '.join(map(repr, unknown))} - add them under `cameras:` "
            f"in config.yaml")
    deduped = []
    for n in names:
        if n not in deduped:
            deduped.append(n)
    return deduped


def ref_path(refs_dir, task_name: str, cam_name: str, primary_cam: str) -> Path:
    """Reference photo to READ for a task+camera.

    Per-camera files are `<task>__<cam>.jpg`. The primary (first-listed)
    camera falls back to the legacy single-camera `<task>.jpg` so
    pre-multicam reference photos keep working untouched.
    """
    task_name, cam_name = safe_name(task_name), safe_name(cam_name)
    per_cam = Path(refs_dir) / f"{task_name}__{cam_name}.jpg"
    if per_cam.exists():
        return per_cam
    legacy = Path(refs_dir) / f"{task_name}.jpg"
    if cam_name == safe_name(primary_cam) and legacy.exists():
        return legacy
    return per_cam


def save_ref_path(refs_dir, task_name: str, cam_name: str) -> Path:
    """Where a NEW reference photo is written (always per-camera naming)."""
    return Path(refs_dir) / f"{safe_name(task_name)}__{safe_name(cam_name)}.jpg"


def angle_prompt(prompt: str, cam_cfg: Optional[dict]) -> str:
    """Prepend the camera's viewpoint description (if any) to the vision
    prompt, so the model can interpret the angle (overhead vs desk-level
    frames look very different)."""
    desc = str((cam_cfg or {}).get("description", "") or "").strip()
    if not desc:
        return prompt
    return f"Camera viewpoint: {desc}.\n{prompt}"


def _conf_rank(conf: str) -> int:
    try:
        return CONFIDENCE_ORDER.index(conf)
    except ValueError:
        return 0


def merge_verdicts(per_cam: list) -> dict:
    """Merge `[(cam_name, verdict), ...]` into one task-level verdict.

    - alert: True if ANY camera alerts
    - items: unioned, prefixed "cam: item" when several cameras reported
    - summary: per-camera summaries joined with a "[cam]" tag
    - confidence: the strongest among ALERTING cameras (or among all,
      when nothing alerted)
    - regions: kept only for a single camera - boxes are frame-relative,
      so a merged multi-camera verdict carries none (read them from the
      per-camera verdicts instead, via the "per_camera" key)
    """
    if not per_cam:
        raise ValueError("no verdicts to merge")
    if len(per_cam) == 1:
        return dict(per_cam[0][1])

    merged = {"alert": any(v.get("alert") for _, v in per_cam)}
    items, seen = [], set()
    for cam, v in per_cam:
        for it in v.get("items", []):
            label = f"{cam}: {it}"
            if label not in seen:
                seen.add(label)
                items.append(label)
    merged["items"] = items
    merged["summary"] = " ".join(
        f"[{cam}] {v.get('summary', '').strip()}"
        for cam, v in per_cam if v.get("summary", "").strip())
    pool = ([v for _, v in per_cam if v.get("alert")]
            or [v for _, v in per_cam])
    merged["confidence"] = max(
        (v.get("confidence", "low") for v in pool), key=_conf_rank)
    merged["regions"] = []
    merged["vetoed"] = [x for _, v in per_cam for x in v.get("vetoed", [])]
    merged["per_camera"] = {cam: v for cam, v in per_cam}
    return merged
