# Wall-Eye camera firmware (roomcam)

`firmware/roomcam/` is a small LAN-only firmware for ESP32-S3 camera boards. It
serves JPEG stills, an MJPEG stream, and a JSON status endpoint over plain HTTP
on your local network. No cloud, no OTA, no outbound connections of any kind.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `/` | Human-readable status page with a live preview |
| `/capture` | One full-resolution JPEG (what Wall-Eye polls) |
| `/stream` | MJPEG stream for live viewing |
| `/status` | JSON status (IP, RSSI, framesize, temperature) |
| `/control` | Runtime tuning (framesize, brightness, white balance, ...) |

## Security model

The firmware trusts the local network. It has **no authentication**: any
device that can reach the camera's IP can view `/capture` and `/stream`
(live video of the room) and reconfigure the sensor via `/control`,
including switching it to a tiny framesize that quietly degrades
monitoring. This is a deliberate simplicity trade-off, so plan the network
accordingly:

- Run the camera on a network segment you trust. The strong option is an
  isolated IoT VLAN or guest SSID that guests and other smart devices
  cannot reach; at minimum, remember that every phone, laptop, and IoT
  gadget on the same Wi-Fi can watch the stream.
- Never port-forward the camera to the internet or expose it through a
  router DMZ. If you need remote access, use a VPN into your LAN
  (e.g. WireGuard on the router).
- The firmware makes **no outbound connections of any kind** - no cloud, no
  NTP, no OTA, no telemetry. Its only network activity is joining your
  Wi-Fi and answering HTTP/mDNS requests on the LAN. Anything else on the
  wire is not this firmware.

One operational caveat: the HTTP server handles one client at a time, so a
client sitting on `/stream` (for example a forgotten browser tab showing
the preview page) blocks `/capture` until it disconnects - Wall-Eye's
checks will report the camera as unavailable for as long as the stream is
held open. Close live views when you are done with them.

## Hardware

You only need this page if you want a cheap dedicated Wi-Fi camera. If you
have any USB webcam, skip all of this - plug it in and point `config.yaml`
at device index 0.

Any ESP32-S3 board with a camera connector and PSRAM works. Recommended
options:

- **Seeed XIAO ESP32S3 Sense** (recommended, roughly 10-15 USD, camera and
  antenna included, thumb-sized):
  https://www.seeedstudio.com/XIAO-ESP32S3-Sense-p-5639.html
- **Freenove ESP32-S3-WROOM CAM board** (a little larger, breadboard
  friendly, usually 15-20 USD): search "Freenove ESP32-S3 WROOM" on Amazon
  or at freenove.com - the kit ships with the camera module.
- An **OV5640 camera module** as an upgrade (about 5-8 USD): the stock
  OV2640 works fine, but the OV5640 is a 5-megapixel sensor with noticeably
  better detail - search your parts store for "OV5640 24-pin camera module"
  and check the ribbon connector matches your board.

Note the plain ESP32 (non-S3) "AI Thinker ESP32-CAM" boards are slower,
short on RAM for large stills, and lack native USB - the pinout header
supports them, but an S3 board is worth the couple of extra dollars.

The pinout header (`camera_pins.h`) covers the common boards: XIAO ESP32S3,
ESP32S3-EYE, AI Thinker ESP32-CAM, M5Stack units, DFRobot FireBeetle 2, and
others. Select your board's model in `roomcam.ino` where
`CAMERA_MODEL_XIAO_ESP32S3` is defined.

Both OV2640 and OV5640 sensors work; the firmware defaults to the OV5640's
maximum still resolution (2592x1944) and falls back gracefully on smaller
sensors via the `/control?framesize=N` endpoint.

## The whole process in plain English

1. Buy a board (see above). While you wait, install Arduino CLI on your PC.
2. Plug the board into your PC over USB. It shows up as a serial (COM) port.
3. Copy `secrets.h.example` to `secrets.h` and type in your Wi-Fi name and
   password. The build refuses to compile until you do this, so you cannot
   accidentally flash placeholder credentials.
4. Compile the firmware and flash it onto the board with the two commands in
   the next section. This takes a minute or two.
5. Watch the serial monitor: the camera joins your Wi-Fi and prints its IP
   address (for example 192.168.1.50). Open `http://<that IP>/` in a browser
   and you should see a live preview of the room.
6. In Wall-Eye's `config.yaml`, set the camera `source:` to
   `http://<that IP>/capture`. Done - the watcher now uses the Wi-Fi camera.
7. Optional: give the camera a fixed IP in your router's DHCP settings so
   the address never changes, and mount the board on the wall (a dab of
   mounting putty works; 3D-printable cases for the XIAO Sense are easy to
   find on printables.com by searching the board name).

## Toolchain setup

Install [Arduino CLI](https://arduino.github.io/arduino-cli/) and the
arduino-esp32 core:

```
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
```

## Configure Wi-Fi credentials

Copy the example secrets file and edit it:

```
cd firmware/roomcam
cp secrets.h.example secrets.h
```

Open `secrets.h`, set `WIFI_SSID` and `WIFI_PASSWORD` to your network, and
change `SECRETS_CONFIGURED` to `1`. The build deliberately fails until you do,
so placeholder credentials can never be flashed by accident. `secrets.h` is
gitignored - never commit real credentials.

## Compile and flash

For the Seeed XIAO ESP32S3 (PSRAM must be enabled for the camera frame
buffers):

```
arduino-cli compile --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" firmware/roomcam
arduino-cli upload --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" -p COM5 firmware/roomcam
```

Replace `COM5` with your board's serial port (`arduino-cli board list` shows
attached boards; on Linux/macOS it will look like `/dev/ttyACM0`).

For a generic ESP32-S3 camera board, use the generic S3 FQBN and enable PSRAM,
for example:

```
arduino-cli compile --fqbn "esp32:esp32:esp32s3:PSRAM=opi" firmware/roomcam
```

Some boards use quad PSRAM instead of octal; if the camera fails to initialise,
try `PSRAM=enabled` (quad) instead of `PSRAM=opi`. Also make sure the
`CAMERA_MODEL_*` define in `roomcam.ino` matches your board.

After flashing, open the serial monitor at 115200 baud to see the assigned IP
address. The camera is also reachable at `http://roomcam.local/` on networks
with working mDNS.

## Point Wall-Eye at the camera

In Wall-Eye's `config.yaml`, add a camera whose source is the board's
still-image endpoint:

```yaml
cameras:
  wallcam:
    source: http://<camera-ip>/capture
    warmup_frames: 1
    description: wall-mounted wide view of the room
```

Give the board a DHCP reservation in your router so the IP does not change.

## Color-cast or washed-out image?

Some cheap camera modules ship without an IR-cut filter, which produces a
hazy, magenta-tinted image that no sensor setting can fix. Wall-Eye includes a
software correction proxy for this: see `tools/enhance_proxy.py`. Run it on
the machine that consumes the camera and point Wall-Eye at the proxy instead:

```
python tools/enhance_proxy.py --camera http://<camera-ip>/capture
```

Then set Wall-Eye's camera URL to `http://127.0.0.1:8090/capture`.
