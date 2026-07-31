# Wall-Eye

An open-source, fully local AI room watcher - a cheap ESP32 camera or webcam,
your own PC, and a local vision model instead of a $50/month cloud
subscription.

Wall-Eye watches your room through a USB webcam or a Wi-Fi camera, asks a
vision model running on your own machine whether the room is a mess (or
whatever else you tell it to watch for), and alerts you with a desktop toast, a
phone push, and optionally a spoken message. Frames never leave your machine.

## Why

Cloud "AI room camera" products cost $100 or more per camera plus a monthly
fee, and every frame of your home goes to someone else's servers. Wall-Eye
does the same job with hardware you probably already own: a webcam you have
lying around, or an ESP32-S3 camera board that costs about $10, plus the GPU
in your PC running a free local vision model through
[Ollama](https://ollama.com). No cloud, no subscription, no account.

## Features

- **Watch tasks with plain-English prompts** - each task is a prompt checked
  on its own schedule, e.g. "alert if there are two or more out-of-place
  items" or "alert if the 3D print has failed". Per-task camera, interval,
  cooldown, priority, and optional crop zone.
- **Mess detection with per-surface reasoning** - the default room task forces
  the model to give a verdict for every surface (floor, bed, desk, chairs)
  instead of finding the biggest mess and stopping, so medium clutter gets
  caught too.
- **Reference-photo comparison** - compare the current frame against a saved
  "normal" photo of the room to cut false alarms. A close-up verification
  pass re-crops each reported item and confirms it before alerting, and
  `confirm_checks` requires a condition to hold across consecutive checks.
- **Multi-camera tasks** - one task can watch several angles at once, each
  with its own reference photo; it alerts if any camera sees a problem.
- **Alerts everywhere** - Windows toast, phone push via
  [ntfy](https://ntfy.sh) (free, self-hostable), and spoken alerts using
  either your system text-to-speech voice or the built-in "wall-e" robot
  voice (a numpy DSP chain: pitch shift, ring modulation, soft clip). Quiet
  hours supported.
- **Reminders and planner** - timed reminders, to-do and shopping checklists,
  quick notes, daily habits with streaks, a cleanup checklist, and a focus
  timer, all in the desktop dashboard.
- **Chat with the camera** - a chat panel backed by the same vision model, so
  "what do you see right now?" actually works.
- **100% local** - the only network traffic is to your own camera on your LAN,
  your local Ollama server, and (only if you enable it) your ntfy topic.

## Requirements

- Windows, for now (system tray, toast notifications, and audio playback are
  Windows-specific; the core logic is portable and Linux/macOS ports are
  welcome - see CONTRIBUTING.md)
- Python 3.11+
- [Ollama](https://ollama.com) running locally with a vision model
- A camera: any USB webcam, an IP camera that serves a still or stream, a
  phone running an IP-webcam app, or an ESP32-S3 camera board flashed with
  the included firmware (see below)
- A GPU is strongly recommended - see Limitations

## Quick start

```
git clone <this repo>
cd wall-eye
pip install -r requirements.txt
ollama pull qwen3-vl:8b-instruct
```

On first run the app copies `config.example.yaml` to `config.yaml`, which is
your private, gitignored working config (you can also copy it yourself
beforehand). Edit `config.yaml` (at minimum, check the `cameras:` section
points at your camera), then:

```
python gui.py
```

This opens the dashboard and starts the watcher; closing the window leaves the
engine running in the system tray. To run headless with only the tray icon,
use `pythonw app.py` instead. `python check_once.py` runs a single check from
the command line.

If VRAM is tight, `qwen2.5vl:3b` (about 3 GB) also works - set it as
`ollama.model` in `config.yaml`.

## Configuration highlights

`config.yaml` is heavily commented and is the full reference (the tracked
template is `config.example.yaml`; the live `config.yaml` is gitignored
because the app writes your settings, including the ntfy topic, back into
it). The short version:

```yaml
cameras:
  main:
    source: 0                             # first USB webcam
  wallcam:
    source: http://192.168.1.50/capture   # ESP32 camera (see firmware/)
    description: wall-mounted wide view of the room

tasks:
- name: room
  camera: main            # or a list: [main, wallcam]
  prompt: Alert if there are two or more out-of-place items in the room.
  interval_minutes: 30
  reference: true         # compare against a saved "normal" photo
```

Other knobs worth knowing:

- `ollama.keep_alive` - how long the model stays in VRAM after a check
  (`5m` frees the GPU between checks; `24h` keeps it loaded; `0` unloads
  immediately).
- `ollama.verify_items` - the close-up second pass that filters false alarms.
- `alerts.confirm_checks` - only alert once a condition holds N checks in a
  row.
- `alerts.quiet_hours` - no alerts in this window (checks still run).
- `voice.voice` - `system`, `wall-e`, or `off`.
- Per-camera or per-task `zone` - crop to a region of the frame.

After editing, right-click the tray icon and choose "Reload config".

## ESP32 Wi-Fi camera

`firmware/roomcam/` is a small LAN-only firmware for ESP32-S3 camera boards
(for example the Seeed XIAO ESP32S3 Sense, roughly $10-15 with camera
included). It serves JPEG stills and an MJPEG stream over plain HTTP with no
cloud, no OTA, and no outbound connections of any kind. See
[firmware/FIRMWARE.md](firmware/FIRMWARE.md) for the build and flash guide,
then point a camera `source` at `http://<camera-ip>/capture`.

If your camera module ships without an IR-cut filter (hazy, magenta-tinted
image), run `tools/enhance_proxy.py` between the camera and Wall-Eye - it
serves a corrected image at `http://127.0.0.1:8090/capture`.

## Phone notifications

Wall-Eye pushes alerts through [ntfy](https://ntfy.sh), which is free and
self-hostable. Set `alerts.ntfy_topic` in `config.yaml` to a unique,
hard-to-guess topic name (e.g. `my-room-x7q2k`), then subscribe to that topic
in the ntfy app on your phone. Task `priority` maps to ntfy priority, so a
failed 3D print can buzz through Do Not Disturb while routine mess alerts stay
quiet.

**The topic name is effectively a password.** ntfy has no other access
control on public topics: anyone who learns the topic can subscribe and read
every alert (which describes the state of your room), and can also publish
arbitrary messages that show up on your phone under the Wall-Eye title. Use a
long random topic (e.g. `walleye-<20 random characters>`), never reuse it
elsewhere, and treat it like a credential. Also be aware of what leaves your
machine when this feature is on: the alert title and body (the model's
summary plus the item list — e.g. "clothes on the bed") are sent to the
ntfy.sh server. Frames and images are never sent, but if even that text is
too much, self-host a ntfy server on your LAN or use ntfy's access tokens,
or leave `ntfy_topic` empty — with it empty, nothing leaves your machine at
all.

## Security notes

- **Everything is LAN-only by design.** The desktop app listens on nothing;
  its only network traffic is your camera, your local Ollama server, and the
  opt-in ntfy push. The ESP32 firmware never makes outbound connections - no
  cloud, no NTP, no OTA, nothing to phone home to.
- **The camera firmware has no authentication.** Anyone on your Wi-Fi can
  view `/capture` and `/stream` and change sensor settings via `/control`.
  Run it on a network you trust, ideally an isolated IoT VLAN or guest SSID
  that untrusted devices cannot reach. See
  [firmware/FIRMWARE.md](firmware/FIRMWARE.md#security-model) for the full
  trust model.
- **Choose a long random ntfy topic.** The topic name is effectively a
  password - anyone who knows it can read your alerts and push messages to
  your phone. See "Phone notifications" above for details and self-hosting
  options.
- **The enhancement proxy serves room imagery.** `tools/enhance_proxy.py`
  binds to `127.0.0.1` by default; passing `--listen 0.0.0.0` exposes the
  corrected camera feed to every device on the LAN. Only do that on a
  trusted network.
- **Camera URLs are fetched and parsed.** A camera `source:` URL makes
  Wall-Eye download and decode whatever that URL serves (via OpenCV/ffmpeg).
  Point it only at cameras you own. Keep `opencv-python` and `Pillow`
  up to date - they are the parsers handling that untrusted image data.

## Responsible use

Wall-Eye is a camera pointed at living space; use it accordingly.

- Point cameras only at your own space, and tell the people who share it.
- Recording and consent laws vary by country and state - it is your
  responsibility to comply with the laws that apply where the camera is.
- This software has no cloud component by design: frames, alerts, and chat
  stay on your machine (the only exception is the opt-in ntfy alert text).
  Contributors must keep it that way - see CONTRIBUTING.md.

## Tests

```
python -m pytest tests -q
```

## Limitations

- **Windows-first.** Tray, toasts, and audio playback currently assume
  Windows. The vision, alerting, and planner logic is plain Python and
  portable; contributions for Linux/macOS are welcome.
- **A GPU makes it comfortable.** An 8B vision model on CPU takes minutes per
  check; on a mid-range GPU a check takes a few seconds. `qwen2.5vl:3b` and
  `ollama.num_gpu` give you some room to trade quality for resources.
- **It is a vision model, not a security system.** Wall-Eye is built for
  convenience alerts (mess, failed prints, a dog on the couch), not for
  safety-critical monitoring. Expect the occasional false positive or miss,
  and tune with reference photos, `verify_items`, and `confirm_checks`.

## License

MIT - see [LICENSE](LICENSE).
