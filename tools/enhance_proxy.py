"""Serve corrected frames from a room camera with a missing IR-cut filter.

Such a camera's output is veiled and colour-cast, and no sensor setting fixes
that. This proxy sits between the camera and anything that consumes it -
Wall-Eye, a browser, a phone - and serves the recovered image instead, so
nothing downstream needs to know.

    python enhance_proxy.py --camera http://192.168.1.50/capture

Then point consumers (e.g. Wall-Eye's camera url) at
http://127.0.0.1:8090/capture
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import cv2
import numpy as np

from enhance import EnhanceSettings, colour_cast_of, contrast_of, process

# The camera's still-image endpoint (the roomcam firmware serves /capture).
DEFAULT_CAMERA_URL = "http://192.168.1.50/capture"
DEFAULT_PORT = 8090
# Loopback by default; pass --listen 0.0.0.0 to expose the proxy on the LAN.
DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_WIDTH = 1280
FETCH_TIMEOUT_SECONDS = 30
JPEG_QUALITY = 90
# Upper bound on one camera response; a full-resolution OV5640 JPEG is well
# under 2 MB, so anything larger means a misbehaving upstream, not a frame.
MAX_FRAME_BYTES = 16 * 1024 * 1024
# The camera URL is operator-supplied, but restrict it to web schemes anyway
# so a stray file:// or ftp:// URL fails loudly at startup.
ALLOWED_CAMERA_SCHEMES = ("http://", "https://")

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_BAD_GATEWAY = 502

log = logging.getLogger("enhance_proxy")


class CameraUnavailable(RuntimeError):
    """The upstream camera did not return a usable frame."""


def fetch_frame(camera_url: str, timeout: int = FETCH_TIMEOUT_SECONDS) -> np.ndarray:
    """Fetch one JPEG from the camera's still-image URL and decode it."""
    try:
        raw = urllib.request.urlopen(camera_url, timeout=timeout).read(
            MAX_FRAME_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise CameraUnavailable(f"camera fetch failed: {exc}") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise CameraUnavailable(
            f"camera response exceeds {MAX_FRAME_BYTES} bytes; refusing to buffer it")

    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CameraUnavailable(f"camera returned {len(raw)} undecodable bytes")
    return image


def encode_jpeg(image: np.ndarray, quality: int = JPEG_QUALITY) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise CameraUnavailable("JPEG encoding failed")
    return buffer.tobytes()


def make_handler(camera_url: str, width: int, settings: EnhanceSettings) -> type:
    """Build a request handler bound to this proxy's configuration."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "walleye-enhance-proxy"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            route = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if route in ("/", "/capture"):
                    frame = process(fetch_frame(camera_url), width, settings)
                    self._send(HTTP_OK, encode_jpeg(frame), "image/jpeg")
                elif route == "/status":
                    frame = fetch_frame(camera_url)
                    corrected = process(frame, width, settings)
                    body = json.dumps({
                        "camera": camera_url,
                        "outputWidth": corrected.shape[1],
                        "contrastBefore": round(contrast_of(frame), 1),
                        "contrastAfter": round(contrast_of(corrected), 1),
                        "castBefore": round(colour_cast_of(frame), 1),
                        "castAfter": round(colour_cast_of(corrected), 1),
                    }).encode()
                    self._send(HTTP_OK, body, "application/json")
                else:
                    self._send(HTTP_NOT_FOUND, b"not found", "text/plain")
            except CameraUnavailable as exc:
                log.warning("%s", exc)
                self._send(HTTP_BAD_GATEWAY, str(exc).encode(), "text/plain")

        def log_message(self, fmt: str, *args) -> None:
            log.debug(fmt, *args)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", default=DEFAULT_CAMERA_URL,
                        help="camera still-image URL, e.g. http://192.168.1.50/capture")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--listen", default=DEFAULT_LISTEN,
                        help="bind address; 0.0.0.0 exposes the proxy on the LAN")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--crop", type=float, default=1.0,
                        help="fraction of frame to keep, e.g. 0.65 to narrow the fisheye view")
    parser.add_argument("--barrel", type=float, default=0.0,
                        help="barrel distortion correction, e.g. -0.25 to straighten fisheye")
    parser.add_argument("--brightness", type=float, default=1.0,
                        help="brightness multiplier applied after veil removal")
    parser.add_argument("--denoise", type=int, default=0,
                        help="grain reduction, applied last (0 disables); 4-8 is useful")
    parser.add_argument("--sharpen", type=float, default=None,
                        help="unsharp amount; lower it when the image is noisy")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not args.camera.startswith(ALLOWED_CAMERA_SCHEMES):
        parser.error(f"--camera must start with one of {ALLOWED_CAMERA_SCHEMES}")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    defaults = EnhanceSettings()
    settings = EnhanceSettings(
        keep_fraction=args.crop,
        barrel_k1=args.barrel,
        brightness_gain=args.brightness,
        denoise_strength=args.denoise,
        sharpen_amount=defaults.sharpen_amount if args.sharpen is None else args.sharpen,
    )
    handler = make_handler(args.camera, args.width, settings)
    server = ThreadingHTTPServer((args.listen, args.port), handler)
    log.info("proxying %s -> http://%s:%d/capture (width %d)",
             args.camera, args.listen, args.port, args.width)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
