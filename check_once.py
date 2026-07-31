"""CLI smoke test: grab a frame from every camera a task watches, run
the vision check on each, merge, fire a toast.

Usage: python check_once.py [task-name]   (default: room)
"""
import sys
import time
from pathlib import Path

import yaml

import camera
import multicam
import notify
import paths
import vision

ROOT = Path(__file__).parent


def main():
    cfg = yaml.safe_load(paths.ensure_config().read_text(encoding="utf-8"))
    task_name = sys.argv[1] if len(sys.argv) > 1 else "room"
    task = next(t for t in cfg["tasks"] if t["name"] == task_name)
    cams = cfg["cameras"]
    cam_names = multicam.camera_names(task, cams)

    per_cam = []
    for cam_name in cam_names:
        cam = cams[cam_name]
        print(f"[1/3] Grabbing frame from '{cam_name}' "
              f"(source {cam['source']!r}) ...")
        try:
            frame = camera.grab_frame(
                cam["source"], cam.get("width", 1280), cam.get("height", 720),
                cam.get("warmup_frames", 5),
                task.get("zone") or cam.get("zone"),
            )
        except camera.CameraError as e:
            print(f"      SKIPPED: {e}")
            continue
        (ROOT / "last_frame.jpg").write_bytes(frame)
        print(f"      got {len(frame)/1024:.0f} KB -> last_frame.jpg")

        ref = None
        ref_path = multicam.ref_path(ROOT / "refs", task_name, cam_name,
                                     cam_names[0])
        if task.get("reference") and ref_path.exists():
            ref = ref_path.read_bytes()
            print(f"      using reference {ref_path.name}")
        elif task.get("reference"):
            print(f"      no reference yet ({ref_path}) - judging without "
                  "comparison")

        prompt = multicam.angle_prompt(task["prompt"], cam)
        o = cfg["ollama"]
        print(f"[2/3] Asking {o['model']} about '{cam_name}' ...")
        t0 = time.time()
        # Same option plumbing as app.run_vision - otherwise the smoke test
        # would judge with different settings than the real app (e.g. the
        # close-up verify pass).
        verdict = vision.check(frame, prompt, ref, o["url"], o["model"],
                               keep_alive=o.get("keep_alive", "30s"),
                               num_ctx=o.get("num_ctx", 2048),
                               max_image_side=o.get("max_image_side", 512),
                               num_gpu=o.get("num_gpu"),
                               verify=o.get("verify_items", True),
                               ground_model=o.get("ground_model"))
        print(f"      {time.time()-t0:.1f}s  alert={verdict['alert']} "
              f"confidence={verdict['confidence']}")
        print(f"      summary: {verdict['summary']}")
        for item in verdict["items"]:
            print(f"        - {item}")
        per_cam.append((cam_name, verdict))

    if not per_cam:
        print("No camera produced a frame - nothing to judge.")
        return
    verdict = multicam.merge_verdicts(per_cam)

    print("[3/3] Toast ...")
    if verdict["alert"]:
        body = verdict["summary"]
        if verdict["items"]:
            body += "\n\u2022 " + "\n\u2022 ".join(verdict["items"][:4])
        notify.alert(f"Wall-Eye: {task_name}", body,
                     cfg["alerts"].get("ntfy_topic", ""))
    else:
        notify.toast("Wall-Eye test", f"All clear: {verdict['summary']}")
    print("Done.")


if __name__ == "__main__":
    main()
