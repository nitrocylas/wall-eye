"""Wall-Eye - local AI room camera. Runs in the system tray.

Start with:  pythonw app.py     (no console window)
        or:  python app.py      (with console, for debugging)
"""
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import threading
import time

import yaml
from PIL import Image, ImageDraw
import pystray

import alerts
import camera
import chat
import cleanup
import errors as error_report   # aliased: locals named `errors` exist below
import multicam
import notify
import reminders
import paths
import snapshots
import speech
import vision
from paths import (ROOT, REFS, STATE_FILE, CONFIG_FILE, LOG_FILE,
                   REMINDERS_FILE, SNAP_DIR, CLEANUP_FILE, APP_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("walleye")

DEFAULT_MODEL = "qwen3-vl:8b-instruct"   # smaller option: "qwen2.5vl:3b"


class WallEye:
    def __init__(self):
        self.cfg = {}
        self.state = {}
        self.next_due = {}      # task name -> unix time of next check
        self.cam_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.icon = None
        self.listeners = []        # callbacks fed UI events: f(dict)
        self.frame_provider = None  # optional: f(camera_name, zone) -> jpeg|None
        self._chat_obj = None       # lazy conversational Chat (shares the vision model)
        self._voice_before_mute = None  # remembers the voice choice across tray toggles
        self.reminder_store = reminders.ReminderStore(REMINDERS_FILE)
        self.snapshots = snapshots.SnapshotStore(SNAP_DIR)  # check history
        self.cleanup = cleanup.CleanupStore(CLEANUP_FILE)   # tasks/streak/exclusions
        self.load_config()
        self.load_state()

    def emit(self, kind, task_name, text, **extra):
        event = {"kind": kind, "task": task_name, "text": text,
                 "time": time.time(), **extra}
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                log.exception("event listener failed")

    # ---------- config / state ----------

    def load_config(self):
        paths.ensure_config()   # first run: copy config.example.yaml into place
        loaded = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        self.cfg = loaded if isinstance(loaded, dict) else {}
        # hand-emptied sections ("ollama:" with nothing under it) load as None;
        # coerce so lookups don't crash on a hand-edited config
        for sec, empty in (("cameras", {}), ("ollama", {}), ("alerts", {}),
                           ("voice", {}), ("listen", {}), ("ui", {}),
                           ("tasks", []), ("reminders", [])):
            if self.cfg.get(sec) is None:
                self.cfg[sec] = empty
        for sec in ("cameras", "ollama"):
            if not self.cfg[sec]:
                msg = (f"config.yaml has no '{sec}:' section - restore it "
                       "from config.example.yaml")
                log.warning(msg)
                notify.toast(APP_NAME, msg)
        now = time.time()
        # First check 10s after start, then on each task's own interval
        self.next_due = {t["name"]: now + 10 for t in self.tasks()}
        log.info("config loaded: %d active tasks", len(self.tasks()))

    def tasks(self):
        return [t for t in self.cfg.get("tasks", []) if t.get("enabled", True)]

    def reminders(self):
        return [r for r in self.cfg.get("reminders", []) if r.get("enabled", True)]

    def load_state(self):
        if STATE_FILE.exists():
            # a corrupt file (e.g. power cut mid-write) must not brick startup:
            # set it aside and start fresh
            try:
                loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("state.json is not a JSON object")
                self.state = loaded
            except (ValueError, OSError) as e:
                log.warning("state.json unreadable (%s) - starting fresh", e)
                try:
                    STATE_FILE.replace(STATE_FILE.with_suffix(".json.bad"))
                except OSError:
                    pass
        self.state.setdefault("last_alert", {})
        self.state.setdefault("snooze_until", 0)
        self.state.setdefault("reminders_fired", {})
        self.state.setdefault("alert_streak", {})   # task -> consecutive alert count

    def save_state(self):
        # write-then-rename so a crash mid-write can't leave half a file
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, STATE_FILE)

    # ---------- alert gating ----------

    @staticmethod
    def _qh_time(v, fallback=None):
        """quiet-hours value -> dt.time. YAML turns an unquoted '22:30' into the
        base-60 int 1350, so accept both int and 'HH:MM' string. Hand-edited
        garbage ("8pm", "25:00") gets a warning + fallback instead of killing
        alert delivery."""
        try:
            if isinstance(v, int):
                return dt.time(v // 60, v % 60)
            h, _, m = str(v).partition(":")
            return dt.time(int(h or 0), int(m or 0))
        except ValueError:
            log.warning("bad quiet_hours value %r (want \"HH:MM\") - ignored", v)
            return fallback

    def in_quiet_hours(self):
        qh = self.cfg.get("alerts", {}).get("quiet_hours")
        if not qh or len(qh) < 2:
            return False
        now = dt.datetime.now().time()
        start = self._qh_time(qh[0])
        end = self._qh_time(qh[1])
        if start is None or end is None:
            return False   # unparseable window -> keep alerting
        if start <= end:
            return start <= now < end
        return now >= start or now < end   # window spans midnight

    def snoozed(self):
        return time.time() < self.state.get("snooze_until", 0)

    def snooze(self, minutes):
        if minutes is None:  # until tomorrow morning
            qh = self.cfg.get("alerts", {}).get("quiet_hours") or ["22:30", "08:00"]
            end = (self._qh_time(qh[1], dt.time(8, 0)) if len(qh) > 1
                   else dt.time(8, 0))
            tomorrow = dt.datetime.combine(
                dt.date.today() + dt.timedelta(days=1), end)
            self.state["snooze_until"] = tomorrow.timestamp()
            label = "until tomorrow morning"
        else:
            self.state["snooze_until"] = time.time() + minutes * 60
            label = f"for {minutes} minutes"
        self.save_state()
        log.info("snoozed %s", label)
        notify.toast(APP_NAME, f"Snoozed {label}.")

    def unsnooze(self):
        self.state["snooze_until"] = 0
        self.save_state()
        notify.toast(APP_NAME, "Snooze cancelled - watching again.")

    # ---------- core check ----------

    def grab_cam(self, cam_name, zone=None):
        """Grab one frame from a named camera. A task zone wins; otherwise the
        camera's own `zone:` (an always-on crop for that angle) applies."""
        cam = self.cfg["cameras"][cam_name]
        if zone is None:
            zone = cam.get("zone")
        if self.frame_provider is not None:
            jpeg = self.frame_provider(cam_name, zone)
            if jpeg:
                return jpeg
        with self.cam_lock:
            return camera.grab_frame(
                cam["source"], cam.get("width", 1280), cam.get("height", 720),
                cam.get("warmup_frames", 5), zone)

    def grab(self, task, cam_name=None):
        if cam_name is None:
            cam_name = multicam.camera_names(task, self.cfg["cameras"])[0]
        return self.grab_cam(cam_name, task.get("zone"))

    def run_vision(self, task, frame, cam_name=None):
        """Run the configured vision model on a frame for this task, using the
        task's reference photo when one exists. The single place the Ollama
        options are plumbed through. Raises vision.VisionError."""
        cams = self.cfg["cameras"]
        cam_list = multicam.camera_names(task, cams)
        if cam_name is None:
            cam_name = cam_list[0]
        ref = None
        ref_path = multicam.ref_path(REFS, task["name"], cam_name, cam_list[0])
        if task.get("reference") and ref_path.exists():
            ref = ref_path.read_bytes()
        o = self.cfg["ollama"]
        prompt = multicam.angle_prompt(task["prompt"], cams.get(cam_name))
        # user-marked exclusions: hint the model AND hard-filter afterwards
        exc = self.cleanup.exclusions()
        if exc:
            prompt += ("\nKNOWN PERMANENT ITEMS - the user has marked these as "
                       "fine, NEVER report them: " + "; ".join(exc))
        verdict = vision.check(frame, prompt, ref,
                               o.get("url", "http://127.0.0.1:11434"),
                               o.get("model") or DEFAULT_MODEL,
                               keep_alive=o.get("keep_alive", "30s"),
                               num_ctx=o.get("num_ctx", 2048),
                               max_image_side=o.get("max_image_side", 512),
                               num_gpu=o.get("num_gpu"),
                               verify=o.get("verify_items", True),
                               ground_model=o.get("ground_model"))
        if verdict.get("vetoed"):
            log.info("[%s] close-up check vetoed: %s", task["name"],
                     ", ".join(verdict["vetoed"]))
        removed = self.cleanup.filter_verdict(verdict)
        if removed:
            log.info("[%s] excluded (user-marked fine): %s", task["name"],
                     "; ".join(removed))
        return verdict

    def survey_task(self, task):
        """Grab + vision-check EVERY camera the task watches (one may be a
        different angle of the same room). One camera failing doesn't stop
        the others. Returns ([(cam_name, frame, verdict)], [error dicts]);
        error dicts look like {"camera": name, "kind": "camera"|"vision",
        "message": str}. Raises ValueError on a bad camera config (unknown
        name)."""
        results, errors = [], []
        for cam in multicam.camera_names(task, self.cfg["cameras"]):
            try:
                frame = self.grab(task, cam)
            except camera.CameraError as e:
                log.warning("[%s/%s] camera error: %s", task["name"], cam, e)
                errors.append({"camera": cam, "kind": "camera",
                               "message": str(e)})
                continue
            try:
                verdict = self.run_vision(task, frame, cam)
            except vision.VisionError as e:
                log.error("[%s/%s] vision error: %s", task["name"], cam, e)
                errors.append({"camera": cam, "kind": "vision",
                               "message": str(e)})
                continue
            results.append((cam, frame, verdict))
        return results, errors

    def check_task(self, task, manual=False):
        name = task["name"]
        try:
            results, errors = self.survey_task(task)
        except ValueError as e:                     # unknown camera name etc.
            log.error("[%s] %s", name, e)
            if manual:
                notify.toast(APP_NAME, str(e))
            return
        if not results:
            # An unreachable Ollama server is a setup problem the user can
            # fix - surface the how-to-fix message instead of a bug-ish one.
            unreachable = next(
                (e["message"] for e in errors if e["kind"] == "vision"
                 and error_report.is_ollama_unreachable(e.get("message"))),
                None)
            if unreachable:
                self.emit("error", name, unreachable)
                if manual:
                    notify.toast(f"{APP_NAME}: {name}", unreachable)
            elif any(e["kind"] == "vision" for e in errors):
                self.emit("error", name,
                          "Couldn't read the scene this time (the AI model "
                          "glitched). It will retry on the next check.")
                if manual:
                    notify.toast(f"{APP_NAME}: {name}",
                                 "Couldn't read the scene this time - please "
                                 "try again.")
            else:
                # Show the CameraError text verbatim - it already explains
                # what went wrong and what to do about it.
                cam_msg = "; ".join(
                    e["message"] for e in errors if e.get("message")
                ) or "Camera error: no camera produced a frame."
                self.emit("error", name, cam_msg)
                if manual:
                    notify.toast(f"{APP_NAME}: {name}", cam_msg)
            return

        multi = len(results) > 1 or bool(errors)
        for cam, frame, verdict in results:
            label = f"{name}@{cam}" if multi else name
            log.info("[%s] alert=%s conf=%s %s", label, verdict["alert"],
                     verdict["confidence"], verdict["summary"])
            self.emit("check", name, verdict["summary"], alert=verdict["alert"],
                      items=verdict["items"], regions=verdict.get("regions", []),
                      frame=frame, camera=cam)
            try:   # timeline history - never let a disk hiccup kill the check
                self.snapshots.save(label, frame, verdict["alert"],
                                    verdict["items"], verdict.get("regions", []))
            except OSError:
                log.exception("snapshot save failed")
        (ROOT / "last_frame.jpg").write_bytes(results[-1][1])

        verdict = multicam.merge_verdicts([(c, v) for c, _f, v in results])

        if manual:
            # User explicitly asked - answer this check now, no persistence gating.
            if verdict["alert"]:
                self.deliver_alert(task, verdict, manual=True)
            else:
                notify.toast(f"{APP_NAME}: {name}", f"All clear. {verdict['summary']}")
            return

        # Scheduled check: only alert once the condition holds for several checks
        # in a row, so a momentary mess (someone walking past, setting a mug down)
        # doesn't fire a false alarm.
        confirm = task.get("confirm_checks",
                           self.cfg.get("alerts", {}).get(
                               "confirm_checks", alerts.DEFAULT_CONFIRM_CHECKS))
        prev = self.state["alert_streak"].get(name, 0)
        streak, deliver = alerts.update_alert_streak(prev, verdict["alert"], confirm)
        # Only touch the disk when the streak actually moved. A clean room reports
        # 0 -> 0 every check; persisting that each time is a needless SSD write.
        if streak != prev:
            self.state["alert_streak"][name] = streak
            self.save_state()

        if deliver:
            self.deliver_alert(task, verdict)
        elif verdict["alert"]:
            log.info("[%s] mess seen, awaiting confirmation (%d/%d)",
                     name, streak, confirm)
            self.emit("muted", name, f"confirming {streak}/{confirm}",
                      reason="confirming")

    def deliver_alert(self, task, verdict, manual=False):
        name = task["name"]
        now = time.time()
        cooldown = task.get("cooldown_minutes", 60) * 60
        last = self.state["last_alert"].get(name, 0)

        if not manual:
            if self.snoozed():
                log.info("[%s] suppressed (snoozed)", name)
                self.emit("muted", name, "snoozed", reason="snoozed")
                return
            if self.in_quiet_hours():
                log.info("[%s] suppressed (quiet hours)", name)
                self.emit("muted", name, "quiet hours", reason="quiet")
                return
            if now - last < cooldown:
                left = (cooldown - (now - last)) / 60
                log.info("[%s] suppressed (cooldown, %.0fm left)", name, left)
                self.emit("muted", name, f"cooldown, {left:.0f}m left",
                          reason="cooldown")
                return

        body = verdict["summary"]
        if verdict["items"]:
            body += "\n- " + "\n- ".join(verdict["items"][:4])
        log.info("[%s] ALERT delivered%s: %s", name,
                 " (manual)" if manual else "", verdict["summary"])
        notify.alert(f"{APP_NAME}: {name}", body,
                     self.cfg["alerts"].get("ntfy_topic", ""),
                     task.get("priority", "default"))
        self.emit("alert", name, body, items=verdict["items"])
        # file a cleanup task the user can check off for their streak
        ct = self.cleanup.file_task(verdict["items"])
        self.emit("cleanup", name, "; ".join(ct.items), task_id=ct.id)

        if task.get("speak"):
            line = f"{name}: {verdict['summary']}"
            if verdict["items"]:
                line += " " + ", ".join(verdict["items"][:3]) + "."
            speech.speak(line, self.cfg.get("voice", {}))

        self.state["last_alert"][name] = now
        self.save_state()

    def retake_reference(self, task):
        """Save a fresh 'normal' photo for EVERY camera the task watches."""
        try:
            cams = multicam.camera_names(task, self.cfg["cameras"])
        except ValueError as e:
            notify.toast(APP_NAME, str(e))
            return
        saved, failed = [], []
        for cam in cams:
            try:
                frame = self.grab(task, cam)
            except camera.CameraError as e:
                log.warning("[%s/%s] camera error: %s", task["name"], cam, e)
                failed.append(cam)
                continue
            REFS.mkdir(exist_ok=True)
            multicam.save_ref_path(REFS, task["name"], cam).write_bytes(frame)
            saved.append(cam)
        if not saved:
            notify.toast(APP_NAME, "Camera error: couldn't take a reference "
                         "photo from any camera.")
            return
        log.info("[%s] reference photo updated (%s)", task["name"],
                 ", ".join(saved))
        detail = f" ({', '.join(saved)})" if len(cams) > 1 else ""
        note = (f" Couldn't reach: {', '.join(failed)}." if failed else "")
        notify.toast(APP_NAME, f"New reference saved for '{task['name']}'"
                     f"{detail}. This is now what 'normal' looks like.{note}")

    # ---------- chat ----------

    def _chat(self) -> "chat.Chat":
        if self._chat_obj is None:
            o = self.cfg["ollama"]
            self._chat_obj = chat.Chat(
                o.get("url", "http://127.0.0.1:11434"),
                o.get("chat_model") or o.get("model") or DEFAULT_MODEL,
                o.get("num_ctx", 2048))
        return self._chat_obj

    def grab_current(self, cam_name: str = None) -> bytes:
        """Grab one frame (no task zone) so the chat can 'see' the room when
        the user asks. Defaults to the first configured camera; pass a name
        to look through another angle. Raises camera.CameraError."""
        cams = self.cfg["cameras"]
        if cam_name is None:
            cam_name = next(iter(cams))
        if cam_name not in cams:
            raise camera.CameraError(f"no camera named {cam_name!r}")
        return self.grab_cam(cam_name)

    def find_task(self, name):
        if name:
            for t in self.tasks():
                if t["name"] == name:
                    return t
        return self.tasks()[0] if self.tasks() else None

    # ---------- timed reminders ----------

    def _fire_reminder(self, message: str, speak: bool, reminder_id: str = None):
        """Deliver one reminder: dashboard card + toast + phone push + optional
        voice. Shared by the legacy config reminders and the recurring store."""
        log.info("reminder: %s", message)
        self.emit("reminder", None, message, reminder_id=reminder_id)
        # Reminders the user set explicitly get a 'high' ntfy priority so the
        # phone push cuts through (set alerts.ntfy_topic + subscribe on mobile).
        notify.alert(f"{APP_NAME} reminder", message,
                     self.cfg["alerts"].get("ntfy_topic", ""), priority="high")
        if speak:
            speech.speak(message, self.cfg.get("voice", {}))

    def add_reminder_from_text(self, text: str):
        """Parse a typed 'remind me to ...' or 'set a timer for ...' request
        and store it. Returns a confirmation string, or None if `text` was
        neither."""
        was_timer = bool(reminders._TIMER_RE.search(text or ""))
        r = reminders.parse_request(text)
        if r is None:
            return None
        self.reminder_store.add(r)
        self.emit("reminder_added", None, r.message, reminder_id=r.id)
        return reminders.confirmation(r, was_timer=was_timer)

    def describe_upcoming(self, limit: int = 5) -> str:
        """A plain-language summary of the next few reminders/timers."""
        now = dt.datetime.now()
        items = reminders.upcoming(self.reminder_store.enabled(), now, limit)
        return reminders.spoken_upcoming(items, now)

    def tick_reminders(self):
        # 1) Recurring reminders the user set from the UI.
        now_dt = dt.datetime.now()
        for r in self.reminder_store.enabled():
            if reminders.is_due(r, now_dt):
                self.reminder_store.mark_fired(r.id, now_dt.timestamp())
                self._fire_reminder(r.message, r.speak, reminder_id=r.id)

        # 2) Legacy fixed-time reminders hand-written in config.yaml.
        today = dt.date.today().isoformat()
        now = now_dt.time()
        for r in self.reminders():
            key = f"{r['at']}|{r['message']}"
            if self.state["reminders_fired"].get(key) == today:
                continue
            at = dt.time(*map(int, r["at"].split(":")))
            if now >= at:
                self.state["reminders_fired"][key] = today
                self.save_state()
                if self.snoozed() or self.in_quiet_hours():
                    continue
                self._fire_reminder(r["message"], r.get("speak", False))

    # ---------- scheduler ----------

    def scheduler_loop(self):
        while not self.stop_event.wait(15):
            now = time.time()
            for task in self.tasks():
                if now >= self.next_due.get(task["name"], 0):
                    self.next_due[task["name"]] = (
                        now + task.get("interval_minutes", 10) * 60)
                    threading.Thread(target=self.check_task, args=(task,),
                                     daemon=True).start()
            self.tick_reminders()
            if self.cleanup.expire_stale():
                log.info("cleanup task ignored for %dh - streak reset",
                         cleanup.EXPIRE_HOURS)
                self.emit("cleanup", None, "task expired - streak reset")

    # ---------- tray ----------

    def make_icon_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([4, 16, 60, 48], fill=(30, 144, 255, 255))   # eye outline
        d.ellipse([24, 22, 44, 42], fill=(255, 255, 255, 255))  # sclera
        d.ellipse([30, 27, 38, 37], fill=(20, 20, 40, 255))     # pupil
        return img

    @staticmethod
    def _in_thread(fn, *args):
        """pystray actions must take exactly (icon, item)."""
        def action(icon, item):
            threading.Thread(target=fn, args=args, daemon=True).start()
        return action

    def build_menu(self):
        task_items = []
        for t in self.tasks():
            sub = [pystray.MenuItem("Check now",
                                    self._in_thread(self.check_task, t, True))]
            if t.get("reference"):
                sub.append(pystray.MenuItem("Retake reference photo",
                                            self._in_thread(self.retake_reference, t)))
            task_items.append(pystray.MenuItem(t["name"], pystray.Menu(*sub)))

        def voice_enabled(item=None):
            return self.cfg.get("voice", {}).get("voice", "off") != "off"

        def toggle_voice(icon, item):
            v = self.cfg.setdefault("voice", {})
            if voice_enabled():
                self._voice_before_mute = v.get("voice", "system")
                v["voice"] = "off"
            else:
                v["voice"] = self._voice_before_mute or "system"

        def snooze_action(minutes):
            def action(icon, item):
                self.snooze(minutes)
            return action

        def call(fn):
            def action(icon, item):
                fn()
            return action

        def open_in_notepad(path):
            def action(icon, item):
                subprocess.Popen(["notepad", str(path)])
            return action

        return pystray.Menu(
            pystray.MenuItem(APP_NAME, None, enabled=False),
            pystray.Menu.SEPARATOR,
            *task_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Snooze", pystray.Menu(
                pystray.MenuItem("30 minutes", snooze_action(30)),
                pystray.MenuItem("1 hour", snooze_action(60)),
                pystray.MenuItem("3 hours", snooze_action(180)),
                pystray.MenuItem("Until tomorrow", snooze_action(None)),
                pystray.MenuItem("Cancel snooze", call(self.unsnooze)),
            )),
            pystray.MenuItem("Voice alerts", toggle_voice, checked=voice_enabled),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open config (edit settings)", open_in_notepad(CONFIG_FILE)),
            pystray.MenuItem("Reload config", call(self.reload)),
            pystray.MenuItem("View log", open_in_notepad(LOG_FILE)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", call(self.quit)),
        )

    def reload(self):
        try:
            self.load_config()
            self.icon.menu = self.build_menu()
            notify.toast(APP_NAME, "Config reloaded.")
        except Exception as e:
            notify.toast(APP_NAME, f"Config error: {e}")

    def shutdown(self):
        """Free GPU VRAM by unloading the vision model from Ollama."""
        o = self.cfg.get("ollama", {})
        log.info("unloading %s from VRAM", o.get("model"))
        vision.unload(o.get("model", DEFAULT_MODEL),
                      o.get("url", "http://127.0.0.1:11434"))

    def quit(self):
        self.stop_event.set()
        self.shutdown()
        self.icon.stop()

    def run(self):
        threading.Thread(target=self.scheduler_loop, daemon=True).start()
        # Warm the model in the background so the first check is fast.
        o = self.cfg.get("ollama", {})
        threading.Thread(target=vision.warm,
                         args=(o.get("model", DEFAULT_MODEL),
                               o.get("url", "http://127.0.0.1:11434"),
                               o.get("keep_alive", "24h"),
                               o.get("num_ctx", 2048)),
                         daemon=True).start()
        log.info("%s started - %d tasks, %d reminders", APP_NAME,
                 len(self.tasks()), len(self.reminders()))
        self.icon = pystray.Icon(APP_NAME, self.make_icon_image(),
                                 APP_NAME, self.build_menu())
        self.icon.run()   # blocks until Quit


def selftest():
    """Verify the packaged build: config, toast, speech, Ollama. Exit 0 = OK."""
    ok = True
    try:
        cfg = yaml.safe_load(paths.ensure_config().read_text(encoding="utf-8"))
        log.info("selftest: config OK (%d tasks)", len(cfg.get("tasks", [])))
    except Exception:
        log.exception("selftest: config FAILED")
        return 1
    try:
        notify.toast(APP_NAME, "Self-test: notifications work.")
        log.info("selftest: toast OK")
    except Exception:
        log.exception("selftest: toast FAILED")
        ok = False
    try:
        speech.speak(f"Self test passed. {APP_NAME} is ready.",
                     cfg.get("voice", {}))
        log.info("selftest: speech OK")
    except Exception:
        log.exception("selftest: speech FAILED")
        ok = False
    try:
        import requests
        r = requests.get(cfg["ollama"]["url"] + "/api/version", timeout=5)
        log.info("selftest: Ollama OK (%s)", r.json().get("version"))
    except Exception:
        log.exception("selftest: Ollama FAILED (is it running?)")
        ok = False
    log.info("selftest: %s", "ALL PASSED" if ok else "FAILURES - see above")
    return 0 if ok else 1


if __name__ == "__main__":
    # Headless crash reporting: full traceback to the log, one concise
    # stderr line (the GUI adds its own dialog on top of the same hook).
    error_report.install_excepthook()
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    WallEye().run()
