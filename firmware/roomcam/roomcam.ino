// Room camera firmware - serves JPEG stills and an MJPEG stream on the LAN.
//
// Deliberately LAN-only: no cloud endpoints, no OTA, no outbound connections of
// any kind. The device answers requests and does nothing else.
//
// Endpoints:
//   GET /            human-readable status page
//   GET /capture     one JPEG (what Wall-Eye polls)
//   GET /stream      MJPEG stream for live viewing
//   GET /status      JSON status for programmatic checks
//   GET /control?framesize=N   change resolution (see FRAMESIZE_* in sensor.h)

#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>

#include "esp_camera.h"
#include "secrets.h"

// Default board: Seeed XIAO ESP32S3 (Sense) with an OV5640 sensor. For a
// different board, select its model here - see camera_pins.h for the list.
#define CAMERA_MODEL_XIAO_ESP32S3
#include "camera_pins.h"

static const int STATUS_LED_GPIO = 21;
static const bool STATUS_LED_ACTIVE_LOW = true;

static const int HTTP_PORT = 80;
// 20MHz keeps the framerate up; lowering the pixel clock buys only a small
// brightness gain on the stream and none on stills.
static const int XCLK_FREQ_HZ = 20000000;
// Quality below ~10 makes the encoder overflow its buffer at high resolutions,
// which surfaces as esp_camera_fb_get() returning null rather than a clear error.
static const int JPEG_QUALITY = 12;         // Lower is better quality, 10..63.
// One buffer, captured on demand. With two buffers and GRAB_LATEST the driver
// reads out and encodes frames continuously so a fresh one is always waiting -
// which for an on-demand camera means running the sensor and JPEG encoder flat
// out 24/7 for nothing, and the board runs noticeably hot.
static const int FRAME_BUFFER_COUNT = 1;

// Stills and video want opposite things: a 5MP frame is ~1MB and the sensor can
// only push a few per second, while a smooth stream needs small frames. So the
// two endpoints run the sensor at different resolutions and swap on demand.
static const framesize_t DEFAULT_CAPTURE_FRAMESIZE = FRAMESIZE_QSXGA;  // 2592x1944, OV5640 max
static const framesize_t STREAM_FRAMESIZE = FRAMESIZE_SVGA;            // 800x600, for framerate

// Runtime-adjustable so /control?framesize= actually sticks: /capture restores
// this before grabbing, rather than a fixed compile-time constant.
static framesize_t g_captureFramesize = DEFAULT_CAPTURE_FRAMESIZE;

// Changing framesize resets the sensor's exposure registers. Auto-exposure needs
// real time to re-converge - especially in a dim room, where it is hunting for a
// long exposure. Too short a settle leaves every subsequent frame black.
static const int FRAMESIZE_STEP_MS = 120;         // pause between settle frames
static const int FRAMESIZE_CONFIRM_ATTEMPTS = 14; // ~1.7s worst case
static const int MIN_SETTLE_FRAMES = 5;           // frames for AEC to re-converge

// Field of view is fixed by the lens. Narrowing it via sensor windowing
// (set_res_raw) produces unusable frames on the OV5640, because the correct
// blanking/offset geometry is not derivable from the public headers. Crop on
// the client instead - Wall-Eye supports a per-camera `zone:` crop.

// OV5640 manual white-balance gains. Auto white balance cannot correct this
// sensor's magenta cast (it reads the whole scene as neutral and leaves the
// red/blue excess in place), so the gains can be set explicitly instead.
static const int REG_AWB_R_GAIN_HIGH = 0x3400;  // 0x3400/01 = R, 02/03 = G, 04/05 = B
static const int REG_AWB_G_GAIN_HIGH = 0x3402;
static const int REG_AWB_B_GAIN_HIGH = 0x3404;
static const int REG_AWB_MANUAL = 0x3406;       // bit0: 1 = manual gains
static const int AWB_GAIN_UNITY = 0x400;        // 1024 = 1.0x
static const int AWB_GAIN_MAX = 0xFFF;
static const int REG_WRITE_MASK = 0xFF;

// Low-light tuning. Defaults are conservative and tuned for bright scenes.
static const int AE_LEVEL = 2;              // -2..2, bias exposure brighter
static const int BRIGHTNESS = 2;            // -2..2
static const int CONTRAST = 0;              // -2..2
static const int SATURATION = 0;            // -2..2

static const unsigned long WIFI_TIMEOUT_MS = 30000;
static const unsigned long WIFI_RETRY_DELAY_MS = 500;
static const unsigned long STREAM_IDLE_DELAY_MS = 5;

static const char *STREAM_BOUNDARY = "roomcamframe";

// esp_camera_fb_get() blocks forever if the sensor stops delivering frames, and
// a sensor register change can provoke exactly that - wedging the whole server
// while WiFi stays up, so the board pings but answers nothing.
//
// The ESP-IDF task watchdog cannot be used for this: the Arduino core already
// initialises it ("TWDT already initialized"), so subscribing this task
// silently fails and the board stays hung. An independent monitor task watching
// a heartbeat is not subject to that, and still runs while the loop is blocked.
static const uint32_t HEARTBEAT_TIMEOUT_MS = 30000;
static const uint32_t HEARTBEAT_CHECK_MS = 5000;
static const uint32_t MONITOR_STACK_BYTES = 2048;

static volatile uint32_t g_lastLoopMs = 0;

static void heartbeatMonitor(void *) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_CHECK_MS));
    if (millis() - g_lastLoopMs > HEARTBEAT_TIMEOUT_MS) {
      Serial.println("main loop stalled - restarting");
      Serial.flush();
      esp_restart();
    }
  }
}

static WebServer server(HTTP_PORT);

static void setStatusLed(bool on) {
  digitalWrite(STATUS_LED_GPIO, (on == STATUS_LED_ACTIVE_LOW) ? LOW : HIGH);
}

// Write one 12-bit white-balance gain across its high/low register pair.
static bool writeAwbGain(sensor_t *sensor, int highRegister, int gain) {
  if (sensor == nullptr || sensor->set_reg == nullptr) {
    return false;
  }
  gain = constrain(gain, 0, AWB_GAIN_MAX);
  bool ok = sensor->set_reg(sensor, highRegister, REG_WRITE_MASK, (gain >> 8) & 0x0F) >= 0;
  ok = ok && sensor->set_reg(sensor, highRegister + 1, REG_WRITE_MASK, gain & 0xFF) >= 0;
  return ok;
}

// Switch between the sensor's own auto white balance and explicit gains.
static bool setManualWhiteBalance(sensor_t *sensor, bool manual) {
  if (sensor == nullptr || sensor->set_reg == nullptr) {
    return false;
  }
  return sensor->set_reg(sensor, REG_AWB_MANUAL, REG_WRITE_MASK, manual ? 1 : 0) >= 0;
}

// Re-applied after every framesize change: set_framesize resets much of this on
// the OV5640, which is why a mode switch otherwise leaves the image black.
static void applySensorTuning(sensor_t *sensor) {
  if (sensor == nullptr) {
    return;
  }
  sensor->set_exposure_ctrl(sensor, 1);   // auto exposure on
  sensor->set_aec2(sensor, 1);            // DSP auto-exposure - big low-light win
  sensor->set_ae_level(sensor, AE_LEVEL);
  sensor->set_gain_ctrl(sensor, 1);       // auto gain on
  sensor->set_gainceiling(sensor, GAINCEILING_16X);   // higher ceilings make exposure hunt
  sensor->set_whitebal(sensor, 1);
  sensor->set_awb_gain(sensor, 1);
  sensor->set_lenc(sensor, 1);            // lens shading correction (corner falloff)
  sensor->set_bpc(sensor, 1);             // bad pixel correction
  sensor->set_wpc(sensor, 1);             // white pixel correction
  sensor->set_brightness(sensor, BRIGHTNESS);
  sensor->set_contrast(sensor, CONTRAST);
  sensor->set_saturation(sensor, SATURATION);
}

static bool startCamera() {
  camera_config_t config = {};
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.xclk_freq_hz = XCLK_FREQ_HZ;
  config.ledc_timer = LEDC_TIMER_0;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = g_captureFramesize;
  config.jpeg_quality = JPEG_QUALITY;
  config.fb_count = FRAME_BUFFER_COUNT;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  // WHEN_EMPTY: capture only once the previous frame has been consumed, so an
  // idle camera is genuinely idle. Freshness is handled in handleCapture by
  // discarding the buffered frame first.
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%04x\n", err);
    return false;
  }

  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor != nullptr) {
    Serial.printf("Sensor PID 0x%04x\n", sensor->id.PID);
    applySensorTuning(sensor);
  }
  return true;
}

static bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // Sleep adds seconds of latency to on-demand captures.
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long deadline = millis() + WIFI_TIMEOUT_MS;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(WIFI_RETRY_DELAY_MS);
    Serial.print(".");
  }
  Serial.println();
  return WiFi.status() == WL_CONNECTED;
}

// Switch sensor resolution, discarding the torn frames a mode change produces.
static bool useFramesize(framesize_t size) {
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    return false;
  }
  if (sensor->status.framesize == size) {
    return true;
  }
  if (sensor->set_framesize(sensor, size) != 0) {
    Serial.printf("set_framesize(%d) failed\n", (int)size);
    return false;
  }
  applySensorTuning(sensor);  // the mode change wiped these

  // Pull frames until one actually comes back at the requested width. Waiting a
  // fixed delay is not enough: the pipeline keeps delivering old-size frames for
  // a while, and grabbing one mid-change also yields the pre-change exposure.
  const uint16_t expectedWidth = resolution[size].width;
  int matches = 0;
  for (int attempt = 0; attempt < FRAMESIZE_CONFIRM_ATTEMPTS; attempt++) {
    delay(FRAMESIZE_STEP_MS);
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale == nullptr) {
      continue;
    }
    if (stale->width == expectedWidth) {
      matches++;
    } else {
      matches = 0;  // still flushing old-size frames
    }
    esp_camera_fb_return(stale);
    // Keep going past the first match so auto-exposure re-converges too.
    if (matches >= MIN_SETTLE_FRAMES) {
      return true;
    }
  }
  Serial.printf("framesize %d did not settle (wanted width %u)\n", (int)size,
                (unsigned)expectedWidth);
  return true;  // best effort: a wrong-size frame beats no frame
}

static void handleCapture() {
  useFramesize(g_captureFramesize);
  // On-demand capture leaves one frame sitting in the buffer from whenever the
  // last request happened - possibly half an hour ago. Discard it so a
  // monitoring camera never reports a stale view as current.
  camera_fb_t *stale = esp_camera_fb_get();
  if (stale != nullptr) {
    esp_camera_fb_return(stale);
  }
  camera_fb_t *frame = esp_camera_fb_get();
  if (frame == nullptr) {
    // Log it: a null frame is the driver's only signal for several distinct
    // failures, and without this the endpoint fails silently as a bare 500.
    Serial.println("capture failed: esp_camera_fb_get() returned null");
    server.send(500, "text/plain", "capture failed");
    return;
  }
  server.setContentLength(frame->len);
  server.send(200, "image/jpeg", "");
  server.sendContent(reinterpret_cast<const char *>(frame->buf), frame->len);
  esp_camera_fb_return(frame);
}

static void handleStream() {
  WiFiClient client = server.client();
  useFramesize(STREAM_FRAMESIZE);
  client.printf(
      "HTTP/1.1 200 OK\r\n"
      "Content-Type: multipart/x-mixed-replace; boundary=%s\r\n"
      "Cache-Control: no-cache\r\n\r\n",
      STREAM_BOUNDARY);

  while (client.connected()) {
    camera_fb_t *frame = esp_camera_fb_get();
    if (frame == nullptr) {
      break;
    }
    client.printf("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                  STREAM_BOUNDARY, static_cast<unsigned>(frame->len));
    client.write(frame->buf, frame->len);
    client.print("\r\n");
    esp_camera_fb_return(frame);
    g_lastLoopMs = millis();  // a long-lived stream must not look like a hang
    delay(STREAM_IDLE_DELAY_MS);
  }
  // Leave the sensor back at stills resolution so /capture stays full quality.
  useFramesize(g_captureFramesize);
}

static void handleStatus() {
  sensor_t *sensor = esp_camera_sensor_get();
  char body[320];
  snprintf(body, sizeof(body),
           "{\"hostname\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,\"sensorPid\":\"0x%04x\","
           "\"framesize\":%d,\"freePsram\":%u,\"uptimeMs\":%lu,\"tempC\":%.1f}",
           CAMERA_HOSTNAME, WiFi.localIP().toString().c_str(), WiFi.RSSI(),
           sensor ? sensor->id.PID : 0, sensor ? sensor->status.framesize : -1,
           static_cast<unsigned>(ESP.getFreePsram()), millis(),
           temperatureRead());  // ESP32-S3 internal die sensor, degrees C
  server.send(200, "application/json", body);
}

// Applies any subset of the tuning knobs, so brightness can be dialled in live
// from a browser instead of needing a reflash for every experiment.
//   /control?ae_level=2&brightness=1&gainceiling=6&framesize=21
static void handleControl() {
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    server.send(500, "text/plain", "no sensor");
    return;
  }

  int applied = 0;
  String report;

  if (server.hasArg("framesize")) {
    int value = server.arg("framesize").toInt();
    if (value < 0 || value > FRAMESIZE_QSXGA) {
      server.send(400, "text/plain", "framesize out of range (0..21)");
      return;
    }
    if (!useFramesize(static_cast<framesize_t>(value))) {
      server.send(500, "text/plain", "set_framesize failed");
      return;
    }
    g_captureFramesize = static_cast<framesize_t>(value);  // persist for /capture
    report += "framesize=" + String(value) + " ";
    applied++;
  }
  // White balance: any explicit gain switches the sensor into manual mode.
  if (server.hasArg("awb")) {
    bool automatic = server.arg("awb").toInt() != 0;
    sensor->set_whitebal(sensor, automatic ? 1 : 0);
    sensor->set_awb_gain(sensor, automatic ? 1 : 0);
    setManualWhiteBalance(sensor, !automatic);
    report += String("awb=") + (automatic ? "auto" : "manual") + " ";
    applied++;
  }
  if (server.hasArg("wb_mode")) {
    int value = constrain(server.arg("wb_mode").toInt(), 0, 4);
    sensor->set_wb_mode(sensor, value);
    report += "wb_mode=" + String(value) + " ";
    applied++;
  }
  struct GainArg { const char *name; int reg; };
  static const GainArg GAIN_ARGS[] = {
      {"rgain", REG_AWB_R_GAIN_HIGH},
      {"ggain", REG_AWB_G_GAIN_HIGH},
      {"bgain", REG_AWB_B_GAIN_HIGH},
  };
  for (const GainArg &arg : GAIN_ARGS) {
    if (!server.hasArg(arg.name)) {
      continue;
    }
    setManualWhiteBalance(sensor, true);
    int gain = server.arg(arg.name).toInt();
    if (!writeAwbGain(sensor, arg.reg, gain)) {
      server.send(500, "text/plain", String("failed writing ") + arg.name);
      return;
    }
    report += String(arg.name) + "=" + String(gain) + " ";
    applied++;
  }
  if (server.hasArg("ae_level")) {
    int value = constrain(server.arg("ae_level").toInt(), -2, 2);
    sensor->set_ae_level(sensor, value);
    report += "ae_level=" + String(value) + " ";
    applied++;
  }
  if (server.hasArg("brightness")) {
    int value = constrain(server.arg("brightness").toInt(), -2, 2);
    sensor->set_brightness(sensor, value);
    report += "brightness=" + String(value) + " ";
    applied++;
  }
  if (server.hasArg("contrast")) {
    int value = constrain(server.arg("contrast").toInt(), -2, 2);
    sensor->set_contrast(sensor, value);
    report += "contrast=" + String(value) + " ";
    applied++;
  }
  if (server.hasArg("saturation")) {
    int value = constrain(server.arg("saturation").toInt(), -2, 2);
    sensor->set_saturation(sensor, value);
    report += "saturation=" + String(value) + " ";
    applied++;
  }
  if (server.hasArg("gainceiling")) {
    // 0..6 maps to GAINCEILING_2X..GAINCEILING_128X; higher = brighter but noisier.
    int value = constrain(server.arg("gainceiling").toInt(), 0, 6);
    sensor->set_gainceiling(sensor, static_cast<gainceiling_t>(value));
    report += "gainceiling=" + String(value) + " ";
    applied++;
  }

  if (applied == 0) {
    server.send(400, "text/plain",
                "expected one of: framesize ae_level brightness contrast "
                "saturation gainceiling");
    return;
  }
  server.send(200, "text/plain", "ok " + report);
}

static void handleRoot() {
  char body[512];
  snprintf(body, sizeof(body),
           "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
           "<h1>%s</h1><p>IP %s &middot; RSSI %d dBm</p>"
           "<p><a href='/capture'>/capture</a> &middot; <a href='/stream'>/stream</a>"
           " &middot; <a href='/status'>/status</a></p>"
           "<img src='/stream' style='max-width:100%%'>",
           CAMERA_HOSTNAME, WiFi.localIP().toString().c_str(), WiFi.RSSI());
  server.send(200, "text/html", body);
}

// Camera readout and JPEG encoding are DMA and hardware blocks, so the CPU is
// mostly idle-spinning at the default 240MHz while burning power as heat.
// Running at 160MHz keeps the board much cooler with no visible cost.
static const uint32_t CPU_FREQ_MHZ = 160;

void setup() {
  Serial.begin(115200);
  delay(2000);  // Native USB CDC needs a moment before output is visible.
  setCpuFrequencyMhz(CPU_FREQ_MHZ);
  pinMode(STATUS_LED_GPIO, OUTPUT);
  setStatusLed(false);

  Serial.println("\n=== roomcam ===");
  if (!startCamera()) {
    Serial.println("FATAL: camera unavailable, halting.");
    return;  // Leave the LED off so a dead camera is visible at a glance.
  }

  if (!connectWifi()) {
    Serial.println("FATAL: wifi connection failed, halting.");
    return;
  }
  Serial.printf("Connected. IP: %s\n", WiFi.localIP().toString().c_str());

  if (MDNS.begin(CAMERA_HOSTNAME)) {
    MDNS.addService("http", "tcp", HTTP_PORT);
    Serial.printf("Also reachable at http://%s.local/\n", CAMERA_HOSTNAME);
  }

  server.on("/", handleRoot);
  server.on("/capture", handleCapture);
  server.on("/stream", handleStream);
  server.on("/status", handleStatus);
  server.on("/control", handleControl);
  server.begin();

  g_lastLoopMs = millis();
  // Pinned to core 0: the Arduino loop runs on core 1, so a blocked loop cannot
  // starve the monitor.
  xTaskCreatePinnedToCore(heartbeatMonitor, "heartbeat", MONITOR_STACK_BYTES,
                          nullptr, tskIDLE_PRIORITY + 1, nullptr, 0);

  Serial.println("Ready.");
  setStatusLed(true);
}

void loop() {
  g_lastLoopMs = millis();
  server.handleClient();
}
