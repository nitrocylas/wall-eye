"""Frame grabbing from USB webcams (device index) or IP cameras (URL)."""
import cv2


class CameraError(Exception):
    pass


def grab_frame(source=0, width=1280, height=720, warmup_frames=5, zone=None):
    """Open the camera, grab one frame, release. Returns JPEG bytes.

    source: int for a USB webcam, or a URL string for an IP camera
            (e.g. "http://192.168.1.50:8080/video" from the IP Webcam app).
    zone:   optional [x, y, w, h] crop as fractions of the frame (0.0-1.0).
    """
    # USB webcams: try several backends. A backend can report "opened" yet
    # stream nothing when another app holds the device exclusively, so we keep
    # falling through until one actually delivers a frame.
    backends = ([cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
                if isinstance(source, int) else [cv2.CAP_ANY])
    opened_but_empty = False

    for backend in backends:
        cap = (cv2.VideoCapture(source, backend) if isinstance(source, int)
               else cv2.VideoCapture(source))
        if not cap.isOpened():
            cap.release()
            continue
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

            # Let auto-exposure/white-balance settle so the model sees a real image
            frame = None
            for _ in range(max(1, warmup_frames)):
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f          # keep the last good frame
            if frame is not None:
                return encode_frame(frame, zone)
            opened_but_empty = True    # try the next backend
        finally:
            cap.release()

    if opened_but_empty:
        raise CameraError(
            f"Camera {source!r} opened but returned no frames - another app is "
            f"probably using it (Windows Camera, Zoom, Discord, OBS, a browser "
            f"tab...). Close that app, then try again.")
    raise CameraError(f"Could not open camera source {source!r}")


def encode_frame(frame, zone=None):
    """Crop to zone (fractional [x,y,w,h]) and encode a BGR frame to JPEG bytes."""
    if zone:
        h, w = frame.shape[:2]
        x0, y0 = int(zone[0] * w), int(zone[1] * h)
        x1, y1 = x0 + int(zone[2] * w), y0 + int(zone[3] * h)
        frame = frame[y0:y1, x0:x1]
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise CameraError("JPEG encoding failed")
    return buf.tobytes()


def list_webcams(max_index=5):
    """Probe USB webcam indices. Returns list of working indices."""
    found = []
    for idx in range(max_index):
        cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                found.append(idx)
        cap.release()
    return found
