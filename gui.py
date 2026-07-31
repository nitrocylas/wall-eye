"""Wall-Eye dashboard - the desktop window over the Wall-Eye engine.

Tabs: Home briefing, Room watch (monitors / camera / cleanup / timeline),
Planner (reminders, to-do, shopping, notes, habits, focus), Chat, Activity
log and Settings. The engine keeps running in the tray when the window is
closed.
"""
import datetime as dt
import sys
import threading
import time

import cv2
import numpy as np
import math

from PySide6.QtCore import (Qt, QTimer, Signal, QObject, QThread, QTime, QDate,
                            QPointF, QRectF, QPropertyAnimation, QEasingCurve)
from PySide6.QtGui import (QImage, QPixmap, QIcon, QAction, QPainter,
                           QPainterPath, QColor, QPen, QRadialGradient, QFont)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QScrollArea, QFrame, QComboBox, QSpinBox, QCheckBox,
    QTextEdit, QLineEdit, QSizePolicy, QSystemTrayIcon, QMenu, QGridLayout,
    QMessageBox, QGraphicsDropShadowEffect, QTimeEdit, QDateEdit, QProgressBar,
    QStackedWidget, QButtonGroup, QTextBrowser, QGraphicsOpacityEffect,
    QDialog, QPlainTextEdit,
)

import alerts
import briefing
import camera
import chat
import checklist
import errors
import focus
import habits
import notes
import reminders
import speech
import sysmon
import vision
from app import WallEye
from paths import (REFS, CONFIG_FILE, APP_NAME, VERSION, TODO_FILE,
                   SHOPPING_FILE, NOTES_FILE, HABITS_FILE)


def _esc(text):
    """Escape HTML so user speech / LLM replies render literally in the feed."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _fmt_next(when):
    """Short label for a reminder's next fire: '8:00 PM' today, 'Wed 8:00 PM' later."""
    now = dt.datetime.now()
    t = when.strftime("%I:%M %p").lstrip("0")
    return t if when.date() == now.date() else f"{when.strftime('%a')} {t}"


def _parse_hhmm(v):
    """(hours, minutes) from a quiet-hours value. YAML parses an unquoted '22:30'
    as the base-60 int 1350, so accept both the int and the 'HH:MM' string."""
    if isinstance(v, int):
        return v // 60, v % 60
    h, _, m = str(v).partition(":")
    return int(h or 0), int(m or 0)

# ---- themes ----------------------------------------------------------------
# Each theme is a palette. The color NAMES below are also module globals (set by
# _install_theme) so QSS and the paint-time widgets (AnimatedBackground,
# FocusRing) all read the live theme. TEXT/MUTED/ALERT/OK/WARN are shared.
MONO = "'JetBrains Mono', 'Cascadia Mono', Consolas, 'Courier New'"
PREVIEW_W = 460   # room preview render width
ANIMATIONS_ON = True   # toggled from Settings; gates the aurora + transitions

# Colors shared by every theme: text, alert/ok states, and a cool blue
# secondary accent used sparingly for small highlights.
_SHARED = dict(TEXT="#F2EFE9", MUTED="#A08D75", ALERT="#D9534F",
               OKGREEN="#8FBF4D", WARN="#E8B434", EVE="#35C4D7",
               HILITE="#E8B434")


def _theme(accent, accent2, accentdk, bg0, bg1, panel, card, card2, ink,
           line, line2, blobs):
    d = dict(ACCENT=accent, ACCENT2=accent2, ACCENTDK=accentdk, GLOW=accent,
             BG0=bg0, BG1=bg1, PANEL=panel, CARD=card, CARD2=card2, INK=ink,
             LINE=line, LINE2=line2, BLOBS=blobs)
    d.update(_SHARED)
    return d


# BLOBS = the Home-tab aurora colours (r,g,b) for each theme.
# "Wall-Eye" is the default: warm dark browns with a rust-orange accent,
# yellow highlights and a sparing cool-blue secondary.
THEMES = {
    "Wall-Eye": _theme(
        "#D97E28", "#E8B434", "#46290F", "#17110B", "#1D150D", "#221910",
        "#281D11", "#20170D", "#140F09", "#3A2B1A", "#54401F",
        [(0xB0, 0x66, 0x1B), (0x5E, 0x36, 0x0C), (0x14, 0x4E, 0x57)]),
    "Ember": _theme(
        "#ff8a3c", "#f97316", "#3a1e0a", "#0a0806", "#120c07", "#1a1109",
        "#20160c", "#160f08", "#0c0905", "#362313", "#52371c",
        [(0x8a, 0x4c, 0x1c), (0x45, 0x28, 0x10), (0x6f, 0x3e, 0x18)]),
    "Crimson": _theme(
        "#ff6b74", "#e11d48", "#3a0f16", "#0a0708", "#120a0c", "#1a0f11",
        "#201215", "#160d0f", "#0c0809", "#361a1f", "#52262d",
        [(0x8a, 0x1c, 0x28), (0x45, 0x12, 0x18), (0x6f, 0x1c, 0x24)]),
}
DEFAULT_THEME = "Wall-Eye"


def _install_theme(name):
    """Set the module-level color globals from a theme (creates them at import)."""
    pal = {k: v for k, v in THEMES.get(name, THEMES[DEFAULT_THEME]).items()
           if k != "BLOBS"}
    globals().update(pal)
    globals()["THEME_BLOBS"] = THEMES.get(name, THEMES[DEFAULT_THEME])["BLOBS"]


_install_theme(DEFAULT_THEME)


def build_qss():
    """Build the stylesheet from the currently-installed theme globals."""
    return f"""
* {{ color: {TEXT}; font-family: 'Segoe UI Variable Display', 'Segoe UI';
     font-size: 13px; }}
QMainWindow, QWidget#root {{
    background: qlineargradient(x1:0, y1:0, x2:0.4, y2:1,
                stop:0 {BG1}, stop:1 {BG0}); }}
QWidget#panel {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 {CARD}, stop:1 {PANEL});
                 border: 1px solid {LINE}; border-radius: 20px; }}
QWidget#header {{ background: transparent; }}
QLabel#brand {{ font-size: 21px; font-weight: 800; color: {ACCENT};
                letter-spacing: 3px; }}
QLabel#brandsub {{ color: {EVE}; font-size: 11px; letter-spacing: 3px; }}
QLabel#h1 {{ font-size: 13px; font-weight: 800; color: {ACCENT};
             letter-spacing: 2px; }}
QLabel#h2 {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
QLabel#muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#alert {{ color: {ALERT}; font-weight: 600; font-size: 12px; }}
QLabel#ok {{ color: {OKGREEN}; font-weight: 600; font-size: 12px; }}
QLabel#field {{ color: {MUTED}; font-size: 12px; }}
/* status pills in the header, e.g. "8:42 PM", "GPU 6.5 GB" */
QLabel#pill {{ background: {CARD}; border: 1px solid {LINE};
               border-radius: 13px; padding: 5px 12px; color: {TEXT};
               font-size: 12px; font-family: {MONO}; }}
QLabel#pillaccent {{ background: {ACCENTDK}; border: 1px solid {ACCENT2};
               border-radius: 13px; padding: 5px 12px; color: {ACCENT};
               font-size: 12px; font-weight: 600; font-family: {MONO}; }}
QFrame#card {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
               stop:0 {CARD2}, stop:1 {CARD});
               border: 1px solid {LINE}; border-radius: 16px; }}
QFrame#card:hover {{ border-color: {LINE2}; }}
QFrame#card[alert="true"] {{ border: 1px solid {ALERT}; background: #241412; }}
QFrame#mono {{ background: {CARD2}; border: 1px solid {LINE};
               border-radius: 12px; }}
QPushButton {{ background: {CARD2}; border: 1px solid {LINE};
               border-radius: 11px; padding: 9px 14px; color: {TEXT}; }}
QPushButton:hover {{ background: {CARD}; border-color: {LINE2}; }}
QPushButton:pressed {{ background: {INK}; }}
QPushButton#primary {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                       stop:0 {ACCENT2}, stop:1 {ACCENT});
                       border: none; color: {BG0}; font-weight: 700; }}
QPushButton#primary:hover {{ background: {ACCENT2}; }}
QPushButton#ghost {{ background: transparent; border: 1px solid {LINE};
                     color: {MUTED}; }}
QPushButton#ghost:hover {{ color: {ACCENT}; border-color: {ACCENT2}; }}
/* segmented toggle tabs (Live / Cleanup / Timeline, ...) */
QPushButton#seg {{ background: {CARD}; border: 1px solid {LINE};
                   border-radius: 12px; padding: 8px 16px; color: {MUTED}; }}
QPushButton#seg:hover {{ color: {TEXT}; border-color: {LINE2}; }}
QPushButton#seg:checked {{ background: {ACCENTDK}; border-color: {ACCENT2};
                   color: {ACCENT}; font-weight: 700; }}
/* left navigation rail items */
QPushButton#nav {{ background: transparent; border: 1px solid transparent;
                   border-radius: 12px; padding: 12px 15px; color: {MUTED};
                   text-align: left; font-size: 13px; font-weight: 600;
                   letter-spacing: 0.3px; }}
QPushButton#nav:hover {{ background: {CARD}; color: {TEXT};
                   border-color: {LINE}; }}
QPushButton#nav:checked {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                   stop:0 {ACCENTDK}, stop:1 {CARD2});
                   border-color: {ACCENT2}; color: {ACCENT}; }}
QComboBox, QSpinBox, QTimeEdit, QDateEdit {{ background: {INK};
                       border: 1px solid {LINE};
                       border-radius: 10px; padding: 7px 10px; color: {TEXT};
                       min-height: 17px; }}
QComboBox:hover, QSpinBox:hover, QTimeEdit:hover, QDateEdit:hover {{
                       border-color: {LINE2}; }}
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus, QDateEdit:focus {{
                       border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {LINE};
        border-radius: 10px; padding: 5px; outline: none;
        selection-background-color: {ACCENT2}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none;
        background: transparent; }}
QTextEdit {{ background: {INK}; border: 1px solid {LINE}; border-radius: 14px;
             padding: 10px; selection-background-color: {ACCENT2}; }}
QLineEdit {{ background: {INK}; border: 1px solid {LINE}; border-radius: 12px;
             padding: 8px 12px; color: {TEXT};
             selection-background-color: {ACCENT2}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QCheckBox::indicator {{ width: 20px; height: 20px; border-radius: 7px;
        border: 1px solid {LINE}; background: {INK}; }}
QCheckBox::indicator:hover {{ border-color: {LINE2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
/* thin progress bars for the system monitor */
QProgressBar {{ background: {INK}; border: 1px solid {LINE};
        border-radius: 6px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {ACCENT2}, stop:1 {ACCENT}); border-radius: 5px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent;
        border: none; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {LINE2}; border-radius: 4px;
        min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT2}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {ACCENT2};
            border-radius: 8px; padding: 6px 8px; }}
"""


QSS = build_qss()


def apply_theme(name):
    """Switch the active theme: reinstall the palette and rebuild the stylesheet.
    Paint-time widgets (aurora/ring) pick up the new accent on their next repaint;
    a couple of construction-time inline accents settle fully on restart."""
    global QSS
    _install_theme(name)
    QSS = build_qss()
    return QSS


def round_pixmap(pm, radius=12):
    """Clip a pixmap to rounded corners for the sleek card/preview look."""
    if pm.isNull():
        return pm
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pm.width(), pm.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def add_shadow(widget, blur=34, y=6, alpha=150):
    """Soft drop shadow for depth (panels/cards)."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, y)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


def add_glow(widget, color=ACCENT, blur=24, alpha=150):
    """Accent-coloured glow for titles and primary elements."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, 0)
    c = QColor(color)
    c.setAlpha(alpha)
    eff.setColor(c)
    widget.setGraphicsEffect(eff)


def draw_regions(pm, regions):
    """Overlay red rounded rectangles around detected messy items.

    Box coords are fractions (0-1) of the image, so they map straight onto the
    scaled pixmap. People/pets are excluded by the model, not here. No text
    labels - they covered the very thing the box points at; the item names are
    already in the alert/summary.
    """
    if not regions or pm.isNull():
        return pm
    W, H = pm.width(), pm.height()
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    for r in regions:
        box = r.get("box") or []
        if len(box) != 4:
            continue
        try:
            x, y, bw, bh = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        # clamp to the frame
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        bw = min(max(bw, 0.0), 1.0 - x)
        bh = min(max(bh, 0.0), 1.0 - y)
        rx, ry, rw, rh = x * W, y * H, bw * W, bh * H
        if rw < 2 or rh < 2:
            continue
        p.setPen(QPen(QColor(ALERT), 2.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rx, ry, rw, rh, 6, 6)
    p.end()
    return pm


def bgr_to_pixmap(frame, w, regions=None):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h0, w0 = rgb.shape[:2]
    img = QImage(rgb.data, w0, h0, 3 * w0, QImage.Format_RGB888)
    pm = QPixmap.fromImage(img).scaledToWidth(w, Qt.SmoothTransformation)
    return draw_regions(round_pixmap(pm, 12), regions)


def jpeg_to_pixmap(jpeg, w, regions=None):
    arr = np.frombuffer(jpeg, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return bgr_to_pixmap(frame, w, regions)


class ClickableLabel(QLabel):
    """A QLabel that emits `clicked` - used to make the room preview open a
    full-size zoom view (with the mess circled) when tapped."""
    clicked = Signal()

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class ZoomDialog(QWidget):
    """A big, dark, borderless overlay showing one frame large with the messy
    regions circled. Click anywhere or press Esc to dismiss."""

    def __init__(self, jpeg, regions, when_text=""):
        super().__init__(None, Qt.Dialog | Qt.FramelessWindowHint)
        self.setWindowModality(Qt.ApplicationModal)
        self.setStyleSheet(f"background:{BG0};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        screen = QApplication.primaryScreen().availableGeometry()
        target_w = min(1100, int(screen.width() * 0.8))
        img = ClickableLabel()
        img.setAlignment(Qt.AlignCenter)
        img.setPixmap(jpeg_to_pixmap(jpeg, target_w, regions))
        img.clicked.connect(self.close)
        lay.addWidget(img)

        cap = QLabel(("mess circled - " + when_text) if regions else when_text)
        cap.setObjectName("muted")
        cap.setAlignment(Qt.AlignCenter)
        lay.addWidget(cap)
        hint = QLabel("click anywhere or press Esc to close")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        self.adjustSize()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()

    def mousePressEvent(self, e):
        self.close()


# ---- calming animated background (smart-display style) --------------------
class AnimatedBackground(QWidget):
    """A dark panel with a few large, soft accent 'auroras' that drift slowly -
    a calm, ambient backdrop for the Home tab. Cheap: 3 radial-gradient blobs,
    ~20 fps, and the timer only runs while the widget is actually visible (so it
    never costs anything when you're on another tab)."""

    # geometry only (radius fraction, base x, base y, speed); colour comes from
    # the active theme's THEME_BLOBS so the aurora recolours with the theme.
    _GEOM = [
        (0.60, 0.26, 0.30, 0.9),
        (0.68, 0.74, 0.68, 0.7),
        (0.50, 0.58, 0.18, 1.15),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("animbg")
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def _advance(self):
        self._phase += 0.012
        self.update()

    def showEvent(self, e):
        if ANIMATIONS_ON and not self._timer.isActive():
            self._timer.start(50)   # ~20 fps
        super().showEvent(e)

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(BG0))
        p.setPen(Qt.NoPen)
        m = max(w, h)
        blobs = globals().get("THEME_BLOBS", [(0x1b, 0x74, 0xb0)] * 3)
        for (rad, bx, by, spd), (r, g, b) in zip(self._GEOM, blobs):
            # frozen (animations off) -> static positions; still a pretty gradient
            ph = self._phase if ANIMATIONS_ON else 0.0
            cx = (bx + 0.06 * math.sin(ph * spd)) * w
            cy = (by + 0.05 * math.cos(ph * spd * 0.8)) * h
            radius = rad * m
            grad = QRadialGradient(cx, cy, radius)
            c0 = QColor(r, g, b); c0.setAlpha(120)
            c1 = QColor(r, g, b); c1.setAlpha(0)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, c1)
            p.setBrush(grad)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
        p.end()


# ---- focus / pomodoro ring ------------------------------------------------
class FocusRing(QWidget):
    """A circular countdown: a faint full ring with an accent arc sweeping down
    as time elapses, big MM:SS in the middle and the phase label beneath."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(250, 250)
        self._frac = 0.0
        self._text = "25:00"
        self._sub = "FOCUS"

    def set_state(self, frac, text, sub):
        self._frac = max(0.0, min(1.0, frac))
        self._text, self._sub = text, sub
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h) - 24
        rect = QRectF((w - side) / 2, (h - side) / 2, side, side)
        p.setPen(QPen(QColor(LINE), 12))
        p.drawArc(rect, 0, 360 * 16)
        pen = QPen(QColor(ACCENT), 12)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 90 * 16, -int(self._frac * 360 * 16))
        p.setPen(QColor(TEXT))
        f = QFont(); f.setPointSize(38); f.setBold(True); p.setFont(f)
        p.drawText(rect, Qt.AlignCenter, self._text)
        p.setPen(QColor(ACCENT))
        f2 = QFont(); f2.setPointSize(11); f2.setBold(True)
        f2.setLetterSpacing(QFont.AbsoluteSpacing, 2); p.setFont(f2)
        p.drawText(QRectF(rect.x(), rect.y() + side * 0.63, side, 30),
                   Qt.AlignHCenter | Qt.AlignTop, self._sub)
        p.end()


# ---- background camera preview --------------------------------------------
class PreviewWorker(QThread):
    frame_ready = Signal(object)   # emits BGR ndarray
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.source = 0
        self._run = True

    def set_source(self, source):
        self.source = source

    def run(self):
        cap = None
        cur = object()
        while self._run:
            if cur != self.source:
                if cap:
                    cap.release()
                cur = self.source
                src = cur
                cap = (cv2.VideoCapture(src, cv2.CAP_MSMF)
                       if isinstance(src, int) else cv2.VideoCapture(src))
                if not cap.isOpened():
                    self.error.emit(f"Can't open camera {src!r}")
                    cap = None
            if cap:
                ok, frame = cap.read()
                if ok and frame is not None:
                    self.frame_ready.emit(frame)
            self.msleep(60)   # ~16 fps preview
        if cap:
            cap.release()

    def stop(self):
        self._run = False
        self.wait(1500)


# ---- background system monitor --------------------------------------------
class SysMonWorker(QThread):
    """Samples CPU / RAM / GPU-VRAM off the UI thread and emits the result.

    The GPU reading hits Ollama's HTTP API, which used to run on the UI thread
    and stalled the event loop (making window drags stutter). Doing it here keeps
    the interface perfectly smooth."""
    sample = Signal(dict)
    INTERVAL_MS = 2000

    def __init__(self, url):
        super().__init__()
        self.url = url
        self._run = True
        self._paused = False

    def run(self):
        while self._run:
            if not self._paused:
                try:
                    data = sysmon.snapshot(self.url)
                except Exception:
                    data = None
                if data is not None:
                    self.sample.emit(data)
            # sleep in small slices so stop()/resume() are responsive. While
            # paused we idle 1s at a time (no Ollama polling with nobody watching).
            slices = 10 if self._paused else self.INTERVAL_MS // 100
            for _ in range(slices):
                if not self._run:
                    return
                self.msleep(100)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._run = False
        self.wait(1500)


# ---- bridge engine events onto the Qt thread ------------------------------
class EventBridge(QObject):
    event = Signal(dict)


# ---- chat window ----------------------------------------------------------
class ChatSignals(QObject):
    reply = Signal(str)
    error = Signal(str)


class ChatWindow(QWidget):
    """A typed chat with Wall-Eye that can see the room through the camera."""

    def __init__(self, engine, frame_getter, speak_fn):
        super().__init__()
        self.engine = engine
        self.frame_getter = frame_getter     # () -> jpeg bytes | None
        self.speak_fn = speak_fn             # (text) -> None
        self._busy = False
        o = engine.cfg.get("ollama", {})
        self.chat = chat.Chat(o.get("url", "http://127.0.0.1:11434"),
                              o.get("chat_model",
                                    o.get("model", "qwen3-vl:8b-instruct")),
                              o.get("num_ctx", 2048))

        self.setWindowTitle(f"Chat with {APP_NAME}")
        self.setObjectName("root")
        self.setStyleSheet(QSS)
        self.resize(440, 600)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        bar = QLabel(); bar.setFixedSize(4, 16)
        bar.setStyleSheet(f"background:{ACCENT};border-radius:2px;")
        head.addWidget(bar)
        title = QLabel(f"CHAT WITH {APP_NAME.upper()}")
        title.setObjectName("h1")
        head.addWidget(title); head.addStretch()
        clear = QPushButton("Clear"); clear.setObjectName("ghost")
        clear.clicked.connect(self._clear)
        head.addWidget(clear)
        lay.addLayout(head)

        # QTextBrowser (not QTextEdit) so the per-message play links are clickable.
        self.transcript = QTextBrowser(); self.transcript.setReadOnly(True)
        self.transcript.setOpenLinks(False)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.anchorClicked.connect(self._on_anchor)
        self._spoken = []              # texts, indexed by the play links
        lay.addWidget(self.transcript, 1)

        self.status = QLabel(""); self.status.setObjectName("muted")
        lay.addWidget(self.status)

        opts = QHBoxLayout()
        self.see_chk = QCheckBox("See my room"); self.see_chk.setChecked(True)
        self.see_chk.setToolTip("Attach a current camera frame so Wall-Eye can see")
        # Off by default so long replies don't talk over the user; each reply
        # gets its own per-message play link instead.
        self.speak_chk = QCheckBox("Auto-speak replies"); self.speak_chk.setChecked(False)
        self.speak_chk.setToolTip("Off by default. Use the play link on any reply "
                                  "to hear just that one.")
        opts.addWidget(self.see_chk)
        opts.addWidget(self.speak_chk); opts.addStretch()
        lay.addLayout(opts)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Say something to Wall-Eye...")
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        send = QPushButton("Send"); send.setObjectName("primary")
        send.clicked.connect(self._send)
        row.addWidget(send)
        lay.addLayout(row)

        self.sig = ChatSignals()
        self.sig.reply.connect(self._on_reply)
        self.sig.error.connect(self._on_error)

        self._bubble(APP_NAME, "Hello. I'm watching the room - ask me anything, "
                     "or ask what I can see.", user=False)

    # ----- transcript -----
    def _bubble(self, who, text, user):
        color = ACCENT if not user else OKGREEN
        align = "right" if user else "left"
        safe = (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))
        # Wall-Eye's own messages get a small link that speaks just that line.
        play = ""
        if not user:
            idx = len(self._spoken)
            self._spoken.append(text)
            play = (f"&nbsp;&nbsp;<a href='speak:{idx}' "
                    f"style='text-decoration:none; color:{ACCENT}; font-size:11px;'>"
                    f"play</a>")
        self.transcript.append(
            f"<div style='margin:6px 0; text-align:{align};'>"
            f"<span style='color:{color}; font-size:11px; font-weight:600;'>{who}</span>"
            f"{play}"
            f"<br><span style='color:{TEXT};'>{safe}</span></div>")
        sb = self.transcript.verticalScrollBar(); sb.setValue(sb.maximum())

    def _on_anchor(self, url):
        """A play link was clicked - speak that stored message (off the UI thread)."""
        s = url.toString()
        if not s.startswith("speak:") or self.speak_fn is None:
            return
        try:
            idx = int(s.split(":", 1)[1])
            text = self._spoken[idx]
        except (ValueError, IndexError):
            return
        threading.Thread(target=lambda: self.speak_fn(text), daemon=True).start()

    def _set_status(self, text):
        self.status.setText(text)

    # ----- send / receive -----
    def _send(self):
        text = self.input.text().strip()
        if not text or self._busy:
            return
        self.input.clear()
        see = self.see_chk.isChecked()
        self._bubble("You", text, user=True)
        self._busy = True
        self._set_status("Wall-Eye is looking..." if see
                         else "Wall-Eye is thinking...")
        threading.Thread(target=self._worker, args=(text, see),
                         daemon=True).start()

    def _worker(self, text, see):
        """Send the message straight to the chat model (optionally with a
        current room frame attached)."""
        img = None
        o = self.engine.cfg.get("ollama", {})
        side = o.get("max_image_side", 512)
        if see:
            try:
                img = self.frame_getter()
            except Exception:
                img = None
        try:
            keep = o.get("keep_alive", "24h")
            reply = self.chat.send(text, img, keep_alive=keep,
                                   max_image_side=side)
            self.sig.reply.emit(reply)
        except Exception as e:
            self.sig.error.emit(str(e))

    def _on_reply(self, reply):
        self._busy = False
        self._set_status("")
        self._bubble(APP_NAME, reply, user=False)
        if self.speak_chk.isChecked() and self.speak_fn:
            try:
                self.speak_fn(reply)
            except Exception:
                pass

    def _on_error(self, msg):
        self._busy = False
        self._set_status("")
        if errors.is_ollama_unreachable(msg):
            # A setup problem, not a model hiccup - show the fix as-is.
            self._bubble(APP_NAME, msg, user=False)
        else:
            self._bubble(APP_NAME, f"(sorry - I couldn't answer: {msg})",
                         user=False)

    def _clear(self):
        self.chat.reset()
        self.transcript.clear()
        self._spoken = []
        self._bubble(APP_NAME, "Fresh start. What's up?", user=False)

    def closeEvent(self, e):
        # keep the conversation alive in the background
        e.ignore()
        self.hide()


class TaskCard(QFrame):
    def __init__(self, task, on_check, on_retake):
        super().__init__()
        self.setObjectName("card")
        self.task = task
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(9)

        self.snapshot = QLabel("No snapshot yet")
        self.snapshot.setObjectName("muted")
        self.snapshot.setFixedHeight(148)
        self.snapshot.setAlignment(Qt.AlignCenter)
        self.snapshot.setStyleSheet(
            f"background:{INK};border-radius:12px;border:1px solid {LINE};")
        lay.addWidget(self.snapshot)

        header = QHBoxLayout()
        header.setSpacing(8)
        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color:{OKGREEN};font-size:11px;")
        self.dot = dot
        header.addWidget(dot)
        name = QLabel(task["name"])
        name.setObjectName("h2")
        header.addWidget(name)
        header.addStretch()
        lay.addLayout(header)

        self.status = QLabel("Waiting for first check...")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        check = QPushButton("Check now")
        check.setObjectName("primary")
        check.clicked.connect(lambda: on_check(task))
        row.addWidget(check)
        if task.get("reference"):
            retake = QPushButton("Retake")
            retake.setObjectName("ghost")
            retake.setToolTip("Save the current view as the 'clean' reference")
            retake.clicked.connect(lambda: on_retake(task))
            row.addWidget(retake)
        lay.addLayout(row)
        self._load_existing_ref()

    def _load_existing_ref(self):
        p = REFS / f"{self.task['name']}.jpg"
        if p.exists():
            self.snapshot.setPixmap(jpeg_to_pixmap(p.read_bytes(), 240))
            self.snapshot.setText("")

    def update_from_event(self, ev):
        if ev.get("frame"):
            regions = ev.get("regions") if ev.get("alert") else None
            self.snapshot.setPixmap(jpeg_to_pixmap(ev["frame"], 240, regions))
            self.snapshot.setText("")
        when = dt.datetime.fromtimestamp(ev["time"]).strftime("%H:%M")
        if ev.get("alert"):
            txt = "; ".join(ev.get("items") or [ev["text"]])
            self.status.setText(f"{when}  \u00b7  {txt}")
            self.status.setObjectName("alert")
            self.setProperty("alert", "true")
            self.dot.setStyleSheet(f"color:{ALERT};font-size:11px;")
        else:
            self.status.setText(f"{when}  \u00b7  {ev['text']}")
            self.status.setObjectName("ok")
            self.setProperty("alert", "false")
            self.dot.setStyleSheet(f"color:{OKGREEN};font-size:11px;")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.style().unpolish(self)
        self.style().polish(self)


class ReminderToast(QWidget):
    """A small frameless card that slides in when a reminder comes due. Auto-
    dismisses after a while; the user can dismiss now or snooze for 10 minutes.
    Lives as its own top-level so it shows even when the dashboard is in the tray."""

    LIFETIME_MS = 15000

    def __init__(self, text, on_closed, on_snooze):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool |
                         Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._on_closed = on_closed
        self._on_snooze = on_snooze
        self._text = text

        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(
            f"QFrame#card {{ background: {CARD}; border: 1px solid {ACCENT};"
            f" border-radius: 14px; }}")
        add_shadow(card, blur=40, y=10, alpha=180)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("REMINDER")
        title.setObjectName("h1")
        title.setStyleSheet(f"color:{ACCENT}; font-size: 12px; font-weight:700;"
                            " letter-spacing: 1px;")
        head.addWidget(title)
        head.addStretch()
        lay.addLayout(head)

        msg = QLabel(_esc(text))
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{TEXT}; font-size: 14px;")
        msg.setMinimumWidth(230)
        lay.addWidget(msg)

        row = QHBoxLayout()
        row.setSpacing(8)
        snooze = QPushButton("Snooze 10m")
        snooze.setObjectName("ghost")
        snooze.clicked.connect(self._snooze)
        row.addWidget(snooze)
        row.addStretch()
        ok = QPushButton("Dismiss")
        ok.setObjectName("primary")
        ok.clicked.connect(self.close)
        row.addWidget(ok)
        lay.addLayout(row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)   # room for the shadow
        outer.addWidget(card)
        self.adjustSize()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.close)
        self._timer.start(self.LIFETIME_MS)

    def _snooze(self):
        if self._on_snooze:
            self._on_snooze(self._text)
        self.close()

    def closeEvent(self, e):
        self._timer.stop()
        if self._on_closed:
            self._on_closed(self)
        super().closeEvent(e)


class RemindersWindow(QWidget):
    """Add, list, enable/disable and delete recurring reminders."""

    KINDS = [
        ("Every day", reminders.DAILY),
        ("Weekdays (Mon-Fri)", reminders.WEEKDAYS),
        ("Weekly (pick days)", reminders.WEEKLY),
        ("Every N minutes", reminders.INTERVAL),
        ("Once", reminders.ONCE),
    ]

    def __init__(self, engine, log_fn):
        super().__init__()
        self.engine = engine
        self.store = engine.reminder_store
        self.log_fn = log_fn
        self.setWindowTitle(f"{APP_NAME} - Reminders")
        self.setObjectName("root")
        self.setStyleSheet(QSS)
        self.resize(470, 640)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        head = QHBoxLayout()
        bar = QLabel(); bar.setFixedSize(4, 16)
        bar.setStyleSheet(f"background:{ACCENT};border-radius:2px;")
        head.addWidget(bar)
        h = QLabel("REMINDERS"); h.setObjectName("h1")
        head.addWidget(h); head.addStretch()
        lay.addLayout(head)

        # --- quick natural-language add ---
        quick_lbl = QLabel("Quick add - just say it:")
        quick_lbl.setObjectName("muted")
        lay.addWidget(quick_lbl)
        qrow = QHBoxLayout()
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("e.g. remind me to stretch every 30 minutes")
        self.quick.returnPressed.connect(self._quick_add)
        qrow.addWidget(self.quick, 1)
        qbtn = QPushButton("Add"); qbtn.setObjectName("primary")
        qbtn.clicked.connect(self._quick_add)
        qrow.addWidget(qbtn)
        lay.addLayout(qrow)

        # --- detailed builder ---
        builder = QFrame(); builder.setObjectName("card")
        b = QGridLayout(builder)
        b.setContentsMargins(12, 12, 12, 12)
        b.setVerticalSpacing(9); b.setHorizontalSpacing(9)
        b.setColumnStretch(1, 1)

        def fl(t):
            l = QLabel(t); l.setObjectName("field"); return l

        b.addWidget(fl("Remind me to"), 0, 0)
        self.msg = QLineEdit(); self.msg.setPlaceholderText("take out the trash")
        b.addWidget(self.msg, 0, 1, 1, 3)

        b.addWidget(fl("Repeat"), 1, 0)
        self.kind = QComboBox()
        for label, _ in self.KINDS:
            self.kind.addItem(label)
        self.kind.currentIndexChanged.connect(self._sync_fields)
        b.addWidget(self.kind, 1, 1, 1, 3)

        b.addWidget(fl("At"), 2, 0)
        self.time_edit = QTimeEdit(QTime(9, 0))
        self.time_edit.setDisplayFormat("h:mm AP")
        b.addWidget(self.time_edit, 2, 1)

        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("MMM d, yyyy")
        b.addWidget(self.date_edit, 2, 2, 1, 2)

        b.addWidget(fl("Every"), 3, 0)
        self.every = QSpinBox(); self.every.setRange(1, 1440)
        self.every.setValue(30); self.every.setSuffix(" min")
        b.addWidget(self.every, 3, 1)

        self.days_row = QWidget()
        drl = QHBoxLayout(self.days_row)
        drl.setContentsMargins(0, 0, 0, 0); drl.setSpacing(4)
        self.day_boxes = []
        for i, nm in enumerate(reminders.WEEKDAY_NAMES):
            cb = QCheckBox(nm)
            cb.setStyleSheet("QCheckBox{font-size:11px;} "
                             "QCheckBox::indicator{width:14px;height:14px;}")
            self.day_boxes.append(cb)
            drl.addWidget(cb)
        b.addWidget(fl("On"), 4, 0)
        b.addWidget(self.days_row, 4, 1, 1, 3)

        self.speak_chk = QCheckBox("Say it out loud"); self.speak_chk.setChecked(True)
        b.addWidget(self.speak_chk, 5, 1, 1, 3)

        add = QPushButton("Add reminder"); add.setObjectName("primary")
        add.clicked.connect(self._add_detailed)
        b.addWidget(add, 6, 1, 1, 3)
        lay.addWidget(builder)

        # --- existing list ---
        lst_lbl = QLabel("Your reminders"); lst_lbl.setObjectName("h2")
        lay.addWidget(lst_lbl)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.holder = QWidget()
        self.list_lay = QVBoxLayout(self.holder)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch()
        self.scroll.setWidget(self.holder)
        lay.addWidget(self.scroll, 1)

        self._sync_fields()
        self._refresh()

    def _sync_fields(self):
        kind = self.KINDS[self.kind.currentIndex()][1]
        self.time_edit.setVisible(kind in (reminders.DAILY, reminders.WEEKDAYS,
                                           reminders.WEEKLY, reminders.ONCE))
        self.date_edit.setVisible(kind == reminders.ONCE)
        self.every.setVisible(kind == reminders.INTERVAL)
        self.days_row.setVisible(kind == reminders.WEEKLY)

    def _quick_add(self):
        text = self.quick.text().strip()
        if not text:
            return
        if not reminders._REMIND_RE.match(text):
            text = "remind me to " + text
        r = reminders.parse_reminder(text)
        if r is None:
            QMessageBox.information(self, "Reminders",
                "I couldn't read a time out of that. Try e.g. "
                "'every day at 6pm' or 'every 30 minutes'.")
            return
        self.store.add(r)
        self.quick.clear()
        self.log_fn(f"Reminder set: {r.message} - {reminders.describe(r)}")
        self._refresh()

    def _add_detailed(self):
        message = self.msg.text().strip()
        if not message:
            QMessageBox.information(self, "Reminders", "What should I remind you to do?")
            return
        kind = self.KINDS[self.kind.currentIndex()][1]
        t = self.time_edit.time()
        clock = f"{t.hour():02d}:{t.minute():02d}"
        r = reminders.Reminder(message=message, kind=kind, time=clock,
                               speak=self.speak_chk.isChecked())
        if kind == reminders.INTERVAL:
            r.every_minutes = self.every.value()
        elif kind == reminders.WEEKLY:
            r.days = [i for i, cb in enumerate(self.day_boxes) if cb.isChecked()]
            if not r.days:
                QMessageBox.information(self, "Reminders", "Pick at least one day.")
                return
        elif kind == reminders.ONCE:
            r.date = self.date_edit.date().toString("yyyy-MM-dd")
        self.store.add(r)
        self.msg.clear()
        self.log_fn(f"Reminder set: {r.message} - {reminders.describe(r)}")
        self._refresh()

    def _refresh(self):
        # clear existing rows (keep the trailing stretch)
        while self.list_lay.count() > 1:
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        items = self.store.all()
        if not items:
            empty = QLabel("No reminders yet. Add one above.")
            empty.setObjectName("muted")
            self.list_lay.insertWidget(0, empty)
            return
        for r in items:
            self.list_lay.insertWidget(self.list_lay.count() - 1, self._row(r))

    def _row(self, r):
        card = QFrame(); card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 12, 10); row.setSpacing(8)

        on = QCheckBox(); on.setChecked(r.enabled)
        on.setToolTip("Enable / pause this reminder")
        on.stateChanged.connect(lambda _s, rid=r.id, box=on:
                                self._toggle(rid, box.isChecked()))
        row.addWidget(on)

        txt = QVBoxLayout(); txt.setSpacing(2)
        m = QLabel(_esc(r.message)); m.setObjectName("h2"); m.setWordWrap(True)
        if not r.enabled:
            m.setStyleSheet(f"color:{MUTED};")
        txt.addWidget(m)
        sub = QLabel(reminders.describe(r)); sub.setObjectName("muted")
        txt.addWidget(sub)
        row.addLayout(txt, 1)

        rm = QPushButton("x"); rm.setObjectName("ghost"); rm.setFixedWidth(34)
        rm.setToolTip("Delete this reminder")
        rm.clicked.connect(lambda _c, rid=r.id: self._delete(rid))
        row.addWidget(rm)
        return card

    def _toggle(self, rid, on):
        self.store.set_enabled(rid, on)

    def _delete(self, rid):
        self.store.remove(rid)
        self._refresh()

    def closeEvent(self, e):
        e.ignore()
        self.hide()


class Dashboard(QMainWindow):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.cards = {}
        self.todo = checklist.ChecklistStore(TODO_FILE)
        self.shopping = checklist.ChecklistStore(SHOPPING_FILE)
        self.notes = notes.NoteStore(NOTES_FILE)
        self.habits = habits.HabitStore(HABITS_FILE)
        self._room_clear = None       # last room-check verdict (for the briefing)
        self._room_text = ""
        # apply the saved theme + animation preference BEFORE any widget is built
        ui = engine.cfg.get("ui", {})
        global ANIMATIONS_ON
        ANIMATIONS_ON = bool(ui.get("animations", True))
        apply_theme(ui.get("theme", DEFAULT_THEME))
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(1200, 740)
        self.setMinimumSize(980, 620)
        self.setStyleSheet(build_qss())

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setSpacing(14)
        shell.setContentsMargins(16, 14, 16, 16)
        shell.addWidget(self._build_header())

        # left nav rail + stacked pages - one tab per feature, no more clutter.
        self._page_index = {}
        self.stack = QStackedWidget()
        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_nav())
        body.addWidget(self.stack, 1)
        shell.addLayout(body, 1)
        # smooth fade when switching tabs (one-shot effect, removed on finish so
        # it never leaves a persistent graphics effect to slow dragging)
        self.stack.currentChanged.connect(self._animate_page)

        # engine -> GUI events
        self.bridge = EventBridge()
        self.bridge.event.connect(self.on_event)
        engine.listeners.append(self.bridge.event.emit)
        engine.frame_provider = self._provide_frame

        # Preview is SNAPSHOT mode by default: the camera is only opened briefly
        # during each scheduled check, then released. Live view is opt-in so the
        # webcam/GPU aren't busy continuously (good for gaming alongside it).
        self.preview = PreviewWorker()
        cam_cfg = engine.cfg.get("cameras", {})
        self._preview_cam = next(iter(cam_cfg)) if cam_cfg else None
        self.preview.set_source(self._current_source())
        self.preview.frame_ready.connect(self._on_preview)
        self.preview.error.connect(lambda m: self.preview_label.setText(m))
        self.live_on = False
        self._last_preview = None       # latest live frame (only while live_on)
        self._last_snapshot = None      # latest check snapshot jpeg (snapshot mode)
        self._last_regions = None       # boxes from the latest messy verdict
        # chat_window / reminders_window are created as embedded tab pages by
        # _build_nav(); don't reset them to None here.
        self._toasts = []               # live reminder popups (for stacking)

        self._build_tray()
        self.log_line(f"{APP_NAME} online. Watching "
                      f"{len(engine.tasks())} task(s).")

        # Clock ticks on the UI thread (cheap: one setText). The heavier
        # CPU/RAM/GPU sample - which makes an HTTP call to Ollama - runs on a
        # background thread so dragging the window never stutters.
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(1000)
        self._tick_clock()

        url = self.engine.cfg.get("ollama", {}).get("url",
                                                    "http://127.0.0.1:11434")
        self.sysmon_worker = SysMonWorker(url)
        self.sysmon_worker.sample.connect(self._on_stats)
        self.sysmon_worker.start()

        # gentle "breathing" glow on the live status dot (cheap: restyles one
        # tiny label). Runs only while animations are enabled.
        self._pulse_t = 0.0
        self.pulse_timer = QTimer(self)
        self.pulse_timer.timeout.connect(self._pulse_dot)
        self.pulse_timer.start(130)

    # ---------- header ----------
    def _pill(self, text, accent=False):
        lb = QLabel(text)
        lb.setObjectName("pillaccent" if accent else "pill")
        return lb

    def _build_header(self):
        w = QWidget()
        w.setObjectName("header")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(10)

        # status dot + wordmark (no drop-shadow effects here - they force a slow
        # software repaint of the whole window on every drag; the accent colour
        # carries the look on its own).
        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color:{ACCENT}; font-size:14px;")
        self._dot = dot
        lay.addWidget(dot)
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        brand = QLabel("WALL-EYE")
        brand.setObjectName("brand")
        brand_box.addWidget(brand)
        sub = QLabel("LOCAL AI - ROOM WATCH")
        sub.setObjectName("brandsub")
        brand_box.addWidget(sub)
        lay.addLayout(brand_box)
        lay.addStretch()

        # live status pills
        self.clock_pill = self._pill("-", accent=True)
        self.cpu_pill = self._pill("CPU -")
        self.ram_pill = self._pill("RAM -")
        self.gpu_pill = self._pill("GPU -")
        for p in (self.clock_pill, self.cpu_pill, self.ram_pill, self.gpu_pill):
            lay.addWidget(p)
        return w

    def _build_monitor_card(self):
        """A compact CPU / RAM / GPU-VRAM readout with slim bars (System tile)."""
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        title = QLabel("SYSTEM")
        title.setObjectName("h1")
        lay.addWidget(title)

        def bar_row(label):
            row = QVBoxLayout()
            row.setSpacing(3)
            top = QHBoxLayout()
            name = QLabel(label)
            name.setObjectName("field")
            top.addWidget(name)
            top.addStretch()
            row.addLayout(top)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            row.addWidget(bar)
            return row, bar

        cpu_row, self.cpu_bar = bar_row("CPU")
        ram_row, self.ram_bar = bar_row("Memory")
        lay.addLayout(cpu_row)
        self.ram_detail = QLabel("-")
        self.ram_detail.setObjectName("muted")
        lay.addLayout(ram_row)
        lay.addWidget(self.ram_detail)

        gpu_lbl = QLabel("GPU / VRAM")
        gpu_lbl.setObjectName("field")
        lay.addWidget(gpu_lbl)
        self.gpu_detail = QLabel("-")
        self.gpu_detail.setObjectName("muted")
        self.gpu_detail.setWordWrap(True)
        lay.addWidget(self.gpu_detail)
        return card

    # ---------- panels ----------
    def _panel(self, title):
        w = QWidget()
        w.setObjectName("panel")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(9)
        bar = QLabel()
        bar.setFixedSize(4, 16)
        bar.setStyleSheet(f"background:{ACCENT};border-radius:2px;")
        head.addWidget(bar)
        h = QLabel(title.upper())
        h.setObjectName("h1")
        head.addWidget(h)
        head.addStretch()
        lay.addLayout(head)
        return w, lay

    # ---------- navigation ----------
    def _build_nav(self):
        rail = QWidget()
        rail.setObjectName("panel")
        rail.setFixedWidth(200)
        outer = QVBoxLayout(rail)
        outer.setContentsMargins(12, 16, 12, 12)
        outer.setSpacing(8)
        hdr = QLabel("MENU")
        hdr.setObjectName("h1")
        outer.addWidget(hdr)

        # scrollable button list (many tabs now - must never overflow the window)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(5)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self._groups = {}   # group name -> (substack, {key: idx})
        # Six top-level tabs; related screens are grouped behind segmented
        # sub-tabs so the rail isn't a mile long.
        pages = [
            ("home", "Home", self._build_home_page),
            ("room", "Room", lambda: self._grouped("room", [
                ("Live", "watch", self._build_watch_page),
                ("Cleanup", "cleanup", self._build_cleanup_page),
                ("Timeline", "timeline", self._build_timeline_page)])),
            ("planner", "Planner", lambda: self._grouped("planner", [
                ("Reminders", "reminders", self._build_reminders_page),
                ("To-Do", "todo", self._build_todo_page),
                ("Shopping", "shopping", self._build_shopping_page),
                ("Notes", "notes", self._build_notes_page),
                ("Habits", "habits", self._build_habits_page),
                ("Focus", "focus", self._build_focus_page)])),
            ("chat", "Chat", self._build_chat_page),
            ("activity", "Activity", self._build_activity_page),
            ("settings", "Settings", self._build_settings_page),
        ]
        for i, (key, label, builder) in enumerate(pages):
            page = builder()
            self.stack.addWidget(page)
            self._page_index[key] = i
            btn = QPushButton(label)
            btn.setObjectName("nav")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c, idx=i: self.stack.setCurrentIndex(idx))
            self.nav_group.addButton(btn, i)
            lay.addWidget(btn)
        lay.addStretch()
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        quit_btn = QPushButton("Quit")
        quit_btn.setObjectName("nav")
        quit_btn.clicked.connect(self._quit)
        outer.addWidget(quit_btn)

        self.nav_group.button(0).setChecked(True)
        return rail

    def _animate_page(self, idx):
        """Fade the newly-shown page in. The opacity effect is torn down when the
        animation ends, so no page keeps a graphics effect (which would slow the
        window drag). Skipped entirely when animations are off."""
        if not ANIMATIONS_ON:
            return
        page = self.stack.widget(idx)
        if page is None:
            return
        eff = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(170)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda: page.setGraphicsEffect(None))
        anim.start()
        self._page_anim = anim   # keep a reference so it isn't GC'd mid-run

    def _grouped(self, name, sections):
        """A page whose sub-screens are switched by a segmented row (keeps the
        top-level nav short). sections: [(label, key, builder)]."""
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        seg_row = QHBoxLayout()
        seg_row.setSpacing(8)
        substack = QStackedWidget()
        grp = QButtonGroup(wrap)
        grp.setExclusive(True)
        idx_by_key = {}
        for i, (label, key, builder) in enumerate(sections):
            substack.addWidget(builder())
            idx_by_key[key] = i
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.clicked.connect(lambda _c, s=substack, ix=i: s.setCurrentIndex(ix))
            grp.addButton(b, i)
            seg_row.addWidget(b)
        seg_row.addStretch()
        grp.button(0).setChecked(True)
        v.addLayout(seg_row)
        v.addWidget(substack, 1)
        self._groups[name] = (substack, idx_by_key, grp)
        return wrap

    def _select_page(self, key):
        idx = self._page_index.get(key)
        if idx is None:
            return
        btn = self.nav_group.button(idx)
        if btn:
            btn.setChecked(True)
        self.stack.setCurrentIndex(idx)

    def _select_sub(self, group, key):
        """Switch a grouped page to one of its segmented sub-screens."""
        g = self._groups.get(group)
        if not g:
            return
        substack, idx_by_key, grp = g
        i = idx_by_key.get(key)
        if i is not None:
            substack.setCurrentIndex(i)
            btn = grp.button(i)
            if btn:
                btn.setChecked(True)

    # ---------- Home / Daily Briefing ----------
    def _build_home_page(self):
        page = AnimatedBackground()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(46, 40, 46, 40)
        outer.setSpacing(6)

        self.brief_greet = QLabel("Hello")
        self.brief_greet.setStyleSheet(
            f"color:{TEXT}; font-size:30px; font-weight:800; background:transparent;")
        outer.addWidget(self.brief_greet)
        self.brief_date = QLabel("")
        self.brief_date.setStyleSheet(
            f"color:{MUTED}; font-size:15px; letter-spacing:1px; background:transparent;")
        outer.addWidget(self.brief_date)

        outer.addSpacing(6)
        clock_row = QHBoxLayout()
        clock_row.setSpacing(8)
        self.brief_clock = QLabel("-")
        self.brief_clock.setStyleSheet(
            f"color:{ACCENT}; font-size:88px; font-weight:800; background:transparent;")
        clock_row.addWidget(self.brief_clock)
        self.brief_ampm = QLabel("")
        self.brief_ampm.setStyleSheet(
            f"color:{ACCENT}; font-size:22px; font-weight:700; background:transparent;")
        self.brief_ampm.setAlignment(Qt.AlignBottom)
        clock_row.addWidget(self.brief_ampm)
        clock_row.addStretch()
        outer.addLayout(clock_row)

        self.brief_summary = QLabel("")
        self.brief_summary.setWordWrap(True)
        self.brief_summary.setStyleSheet(
            f"color:{TEXT}; font-size:15px; background:transparent;")
        outer.addWidget(self.brief_summary)

        outer.addSpacing(10)
        cards = QHBoxLayout()
        cards.setSpacing(14)
        # upcoming reminders card
        self.brief_rem_box = self._glass_card("UP NEXT")
        cards.addWidget(self.brief_rem_box["frame"], 1)
        # room status card
        self.brief_room_box = self._glass_card("ROOM")
        cards.addWidget(self.brief_room_box["frame"], 1)
        outer.addLayout(cards)
        outer.addStretch()

        self._refresh_briefing()
        return page

    def _glass_card(self, title):
        """A translucent card that sits over the animated background."""
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background: rgba(23,17,11,0.55); border:1px solid "
            f"{LINE}; border-radius:16px; }}")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setStyleSheet(
            f"color:{ACCENT}; font-size:12px; font-weight:800; letter-spacing:2px;"
            " background:transparent;")
        lay.addWidget(head)
        body = QLabel("-")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setStyleSheet(f"color:{TEXT}; font-size:14px; background:transparent;")
        lay.addWidget(body)
        lay.addStretch()
        return {"frame": frame, "body": body}

    def _refresh_briefing(self):
        if not hasattr(self, "brief_greet"):
            return
        now = dt.datetime.now()
        self.brief_greet.setText(briefing.greeting_for_hour(now.hour))
        self.brief_date.setText(briefing.date_line(now).upper())
        self.brief_clock.setText(briefing.clock_line(now))
        self.brief_ampm.setText(briefing.ampm(now))
        upcoming = reminders.upcoming(self.engine.reminder_store.enabled(), now, 4)
        open_todo = self.todo.counts()[0]
        self.brief_summary.setText(
            briefing.summarize_day(len(upcoming), open_todo, self._room_clear))
        if upcoming:
            rows = "<br>".join(
                f"<span style='color:{ACCENT};'>{_fmt_next(nf)}</span>"
                f"&nbsp;&nbsp;{_esc(r.message)}" for r, nf in upcoming)
        else:
            rows = f"<span style='color:{MUTED};'>Nothing scheduled.</span>"
        self.brief_rem_box["body"].setText(rows)
        streak = self.engine.cleanup.streak
        flame = (f"<br><span style='color:{HILITE}; font-weight:700;'>"
                 f"cleanup streak: {streak}</span>" if streak else "")
        if self._room_clear is True:
            self.brief_room_box["body"].setText(
                f"<span style='color:{OKGREEN};'>All clear</span><br>"
                f"<span style='color:{MUTED};'>{_esc(self._room_text)}</span>{flame}")
        elif self._room_clear is False:
            self.brief_room_box["body"].setText(
                f"<span style='color:{ALERT};'>Needs a tidy</span><br>"
                f"<span style='color:{MUTED};'>{_esc(self._room_text)}</span>{flame}")
        else:
            self.brief_room_box["body"].setText(
                f"<span style='color:{MUTED};'>No check yet - hit 'Check now'.</span>"
                + flame)

    # ---------- checklist tabs (To-Do / Shopping) ----------
    def _build_todo_page(self):
        return self._build_checklist_page("To-Do", self.todo, "todo",
                                          "Add a task and press Enter...")

    def _build_shopping_page(self):
        return self._build_checklist_page("Shopping", self.shopping, "shopping",
                                          "Add an item and press Enter...")

    def _build_checklist_page(self, title, store, key, placeholder):
        w, lay = self._panel(title)
        add_row = QHBoxLayout()
        inp = QLineEdit()
        inp.setPlaceholderText(placeholder)
        add_btn = QPushButton("Add")
        add_btn.setObjectName("primary")
        add_row.addWidget(inp, 1)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        list_lay = QVBoxLayout(holder)
        list_lay.setSpacing(7)
        list_lay.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)

        clear = QPushButton("Clear completed")
        clear.setObjectName("ghost")
        lay.addWidget(clear)

        # stash refs so _refresh_checklist can rebuild this page
        state = {"store": store, "list_lay": list_lay}
        setattr(self, f"_cl_{key}", state)

        def add_item():
            if store.add(inp.text()):
                inp.clear()
                self._refresh_checklist(key)
        inp.returnPressed.connect(add_item)
        add_btn.clicked.connect(add_item)
        clear.clicked.connect(lambda: (store.clear_done(),
                                       self._refresh_checklist(key)))
        self._refresh_checklist(key)
        return w

    def _refresh_checklist(self, key):
        state = getattr(self, f"_cl_{key}", None)
        if not state:
            return
        list_lay, store = state["list_lay"], state["store"]
        while list_lay.count() > 1:
            it = list_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        items = store.all()
        if not items:
            empty = QLabel("Nothing here yet - add something above.")
            empty.setObjectName("muted")
            list_lay.insertWidget(0, empty)
            return
        for item in items:
            list_lay.insertWidget(list_lay.count() - 1,
                                  self._checklist_row(key, store, item))

    def _checklist_row(self, key, store, item):
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)
        cb = QCheckBox()
        cb.setChecked(item.done)
        cb.stateChanged.connect(lambda _s, i=item.id: (store.toggle(i),
                                                       self._refresh_checklist(key)))
        row.addWidget(cb)
        text = QLabel(_esc(item.text))
        text.setWordWrap(True)
        if item.done:
            text.setStyleSheet(
                f"color:{MUTED}; text-decoration: line-through; background:transparent;")
        else:
            text.setStyleSheet(f"color:{TEXT}; background:transparent;")
        row.addWidget(text, 1)
        rm = QPushButton("x")
        rm.setObjectName("ghost")
        rm.setFixedWidth(34)
        rm.clicked.connect(lambda _c, i=item.id: (store.remove(i),
                                                  self._refresh_checklist(key)))
        row.addWidget(rm)
        return card

    # ---------- Cleanup (tasks + streak + exclusions) ----------
    def _build_cleanup_page(self):
        w, lay = self._panel("Cleanup")
        self._cleanup_streak_lbl = QLabel("")
        self._cleanup_streak_lbl.setStyleSheet(
            f"color:{HILITE}; font-size:22px; font-weight:800; background:transparent;")
        lay.addWidget(self._cleanup_streak_lbl)
        sub = QLabel("When the watcher spots mess it files a task here. "
                     "'Cleaned it' keeps your streak alive; 'Not yet' (or ignoring "
                     "it for a day) resets it.")
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self._cleanup_lay = QVBoxLayout(holder)
        self._cleanup_lay.setSpacing(9)
        self._cleanup_lay.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)

        exc_hdr = QLabel("EXCLUSIONS - things Wall-Eye should never flag")
        exc_hdr.setObjectName("h1")
        lay.addWidget(exc_hdr)
        exc_row = QHBoxLayout()
        self._exc_input = QLineEdit()
        self._exc_input.setPlaceholderText(
            "e.g. cables behind the desk - or press Exclude on any flagged item above")
        exc_add = QPushButton("Add")
        exc_add.setObjectName("ghost")
        exc_row.addWidget(self._exc_input, 1)
        exc_row.addWidget(exc_add)
        lay.addLayout(exc_row)
        self._exc_lay = QVBoxLayout()
        self._exc_lay.setSpacing(5)
        lay.addLayout(self._exc_lay)

        def add_exc():
            if self.engine.cleanup.add_exclusion(self._exc_input.text()):
                self._exc_input.clear()
                self._refresh_cleanup()
        exc_add.clicked.connect(add_exc)
        self._exc_input.returnPressed.connect(add_exc)
        self._refresh_cleanup()
        return w

    def _refresh_cleanup(self):
        store = self.engine.cleanup
        n = store.streak
        best = f"   \u00b7   best {store.best_streak}" if store.best_streak > n else ""
        self._cleanup_streak_lbl.setText(f"Cleanup streak: {n}{best}")
        # pending + recent tasks
        lay = self._cleanup_lay
        while lay.count() > 1:
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        shown = [t for t in reversed(store.tasks())
                 if t.status == "pending"][:1] + \
                [t for t in reversed(store.tasks())
                 if t.status != "pending"][:5]
        if not shown:
            e = QLabel("No cleanup tasks yet - the watcher files one when it "
                       "spots mess.")
            e.setObjectName("muted")
            lay.insertWidget(0, e)
        for t in shown:
            lay.insertWidget(lay.count() - 1, self._cleanup_card(t))
        # exclusions list
        while self._exc_lay.count():
            it = self._exc_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for text in store.exclusions():
            row = QFrame()
            row.setObjectName("mono")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            lbl = QLabel(_esc(text))
            lbl.setStyleSheet(f"color:{MUTED}; background:transparent;")
            h.addWidget(lbl, 1)
            rm = QPushButton("x")
            rm.setObjectName("ghost")
            rm.setFixedWidth(30)
            rm.setToolTip("Remove - Wall-Eye will flag this again")
            rm.clicked.connect(lambda _c, x=text: (
                self.engine.cleanup.remove_exclusion(x), self._refresh_cleanup()))
            h.addWidget(rm)
            self._exc_lay.addWidget(row)

    def _cleanup_card(self, t):
        card = QFrame()
        card.setObjectName("card")
        if t.status == "pending":
            card.setProperty("alert", "true")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 11, 14, 11)
        v.setSpacing(7)
        when = dt.datetime.fromtimestamp(t.created).strftime("%a %I:%M %p")
        badge = {"pending": ("NEEDS CLEANING", ALERT), "done": ("CLEANED", OKGREEN),
                 "skipped": ("SKIPPED", MUTED), "expired": ("EXPIRED", MUTED)}[t.status]
        head = QLabel(f"{badge[0]}  \u00b7  {when}")
        head.setStyleSheet(f"color:{badge[1]}; font-weight:700; font-size:11px;"
                           " letter-spacing:1px; background:transparent;")
        v.addWidget(head)
        for item in t.items:
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel("\u2022 " + _esc(item))
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{TEXT}; background:transparent;")
            row.addWidget(lbl, 1)
            if t.status == "pending":
                ex = QPushButton("Exclude")
                ex.setObjectName("ghost")
                ex.setToolTip("Can't/shouldn't clean this - never flag it again")
                ex.clicked.connect(lambda _c, x=item: self._exclude_item(x))
                row.addWidget(ex)
            v.addLayout(row)
        if t.status == "pending":
            btns = QHBoxLayout()
            done = QPushButton("Cleaned it")
            done.setObjectName("primary")
            done.clicked.connect(lambda _c, i=t.id: self._resolve_cleanup(i, True))
            nope = QPushButton("Not yet")
            nope.setObjectName("ghost")
            nope.setToolTip("Honest, but it resets your streak!")
            nope.clicked.connect(lambda _c, i=t.id: self._resolve_cleanup(i, False))
            btns.addWidget(done)
            btns.addWidget(nope)
            btns.addStretch()
            v.addLayout(btns)
        return card

    def _resolve_cleanup(self, task_id, done):
        streak = self.engine.cleanup.resolve(task_id, done)
        if streak is None:
            self._refresh_cleanup()
            return
        if done:
            self.log_line(f"Task cleaned - streak is now <b>{streak}</b>.")
        else:
            self.log_line("Marked not-cleaned - streak reset. Fresh start!")
        self._refresh_cleanup()
        self._refresh_briefing()

    def _exclude_item(self, item):
        if self.engine.cleanup.add_exclusion(item):
            self.log_line(f"Excluded: <i>{_esc(item)}</i> - won't be flagged again.")
        self._refresh_cleanup()

    # ---------- Timeline ----------
    def _build_timeline_page(self):
        w, lay = self._panel("Timeline")
        sub = QLabel("Recent room checks - click any photo to enlarge.")
        sub.setObjectName("muted")
        lay.addWidget(sub)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self._timeline_lay = QVBoxLayout(holder)
        self._timeline_lay.setSpacing(10)
        self._timeline_lay.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)
        self._refresh_timeline()
        return w

    def _refresh_timeline(self):
        if not hasattr(self, "_timeline_lay"):
            return
        # Rebuilding decodes ~24 JPEG thumbnails - skip it while the dashboard is
        # hidden in the tray (the common case); just mark it stale and rebuild
        # when the window is next shown.
        if not self.isVisible():
            self._timeline_dirty = True
            return
        self._timeline_dirty = False
        lay = self._timeline_lay
        while lay.count() > 1:
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        recs = self.engine.snapshots.latest(24)
        if not recs:
            e = QLabel("No checks recorded yet. Hit 'Check now' on the Watch tab.")
            e.setObjectName("muted")
            lay.insertWidget(0, e)
            return
        for rec in recs:
            lay.insertWidget(lay.count() - 1, self._timeline_row(rec))

    def _timeline_row(self, rec):
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 10, 10, 10)
        row.setSpacing(12)
        thumb = ClickableLabel()
        thumb.setFixedSize(170, 96)
        thumb.setAlignment(Qt.AlignCenter)
        regions = rec.get("regions") if rec.get("alert") else None
        try:
            jpeg = self.engine.snapshots.path(rec).read_bytes()
        except OSError:
            jpeg = None
        when = dt.datetime.fromtimestamp(rec["time"])
        if jpeg:
            thumb.setPixmap(jpeg_to_pixmap(jpeg, 170, regions))
            wtxt = when.strftime("%b %d, %I:%M %p")
            thumb.clicked.connect(
                lambda _=None, j=jpeg, r=regions, t=wtxt: self._open_zoom(j, r, t))
        row.addWidget(thumb)
        info = QVBoxLayout()
        info.setSpacing(3)
        wl = QLabel(when.strftime("%a %b %d \u00b7 %I:%M %p"))
        wl.setObjectName("h2")
        info.addWidget(wl)
        if rec.get("alert"):
            items = "; ".join(rec.get("items") or []) or "see photo"
            st = QLabel(f"\u25cf messy - {_esc(items)}")
            st.setStyleSheet(f"color:{ALERT}; background:transparent;")
            st.setWordWrap(True)
        else:
            st = QLabel("\u25cf all clear")
            st.setStyleSheet(f"color:{OKGREEN}; background:transparent;")
        info.addWidget(st)
        info.addStretch()
        row.addLayout(info, 1)
        return card

    def _open_zoom(self, jpeg, regions, when):
        self._zoom = ZoomDialog(jpeg, regions, when)
        self._zoom.showFullScreen()

    # ---------- Notes ----------
    def _build_notes_page(self):
        w, lay = self._panel("Notes")
        add_row = QHBoxLayout()
        inp = QLineEdit()
        inp.setPlaceholderText("Jot a note and press Enter...")
        add = QPushButton("Add")
        add.setObjectName("primary")
        add_row.addWidget(inp, 1)
        add_row.addWidget(add)
        lay.addLayout(add_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self._notes_lay = QVBoxLayout(holder)
        self._notes_lay.setSpacing(7)
        self._notes_lay.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)

        def add_note():
            if self.notes.add(inp.text()):
                inp.clear()
                self._refresh_notes()
        inp.returnPressed.connect(add_note)
        add.clicked.connect(add_note)
        self._refresh_notes()
        return w

    def _refresh_notes(self):
        lay = self._notes_lay
        while lay.count() > 1:
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        items = self.notes.all()
        if not items:
            e = QLabel("No notes yet.")
            e.setObjectName("muted")
            lay.insertWidget(0, e)
            return
        for n in items:
            lay.insertWidget(lay.count() - 1, self._note_row(n))

    def _note_row(self, n):
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(10)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        body = QLabel(_esc(n.text))
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{TEXT}; background:transparent;")
        txt.addWidget(body)
        when = QLabel(dt.datetime.fromtimestamp(n.created).strftime("%b %d \u00b7 %I:%M %p"))
        when.setObjectName("muted")
        txt.addWidget(when)
        row.addLayout(txt, 1)
        rm = QPushButton("x")
        rm.setObjectName("ghost")
        rm.setFixedWidth(34)
        rm.clicked.connect(lambda _c, i=n.id: (self.notes.remove(i),
                                               self._refresh_notes()))
        row.addWidget(rm)
        return card

    # ---------- Habits ----------
    def _build_habits_page(self):
        w, lay = self._panel("Habits")
        sub = QLabel("Check off each day - keep the streak alive.")
        sub.setObjectName("muted")
        lay.addWidget(sub)
        add_row = QHBoxLayout()
        inp = QLineEdit()
        inp.setPlaceholderText("New habit (e.g. drink water)...")
        add = QPushButton("Add")
        add.setObjectName("primary")
        add_row.addWidget(inp, 1)
        add_row.addWidget(add)
        lay.addLayout(add_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self._habits_lay = QVBoxLayout(holder)
        self._habits_lay.setSpacing(8)
        self._habits_lay.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)

        def add_habit():
            if self.habits.add(inp.text()):
                inp.clear()
                self._refresh_habits()
        inp.returnPressed.connect(add_habit)
        add.clicked.connect(add_habit)
        self._refresh_habits()
        return w

    def _refresh_habits(self):
        lay = self._habits_lay
        while lay.count() > 1:
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        items = self.habits.all()
        if not items:
            e = QLabel("No habits yet - add one above.")
            e.setObjectName("muted")
            lay.insertWidget(0, e)
            return
        today = dt.date.today()
        for h in items:
            lay.insertWidget(lay.count() - 1, self._habit_row(h, today))

    def _habit_row(self, h, today):
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(10)
        cb = QCheckBox()
        cb.setChecked(today.isoformat() in h.days)
        cb.setToolTip("Mark done for today")
        cb.stateChanged.connect(
            lambda s, i=h.id: (self.habits.set_done(i, dt.date.today(), bool(s)),
                               self._refresh_habits()))
        row.addWidget(cb)
        name = QLabel(_esc(h.name))
        name.setObjectName("h2")
        row.addWidget(name, 1)
        n = habits.streak(h.days, today)
        flame = QLabel(f"{n} day streak" if n else "-")
        flame.setStyleSheet(
            f"color:{HILITE if n else MUTED}; font-weight:700; background:transparent;")
        flame.setToolTip("Day streak")
        row.addWidget(flame)
        rm = QPushButton("x")
        rm.setObjectName("ghost")
        rm.setFixedWidth(34)
        rm.clicked.connect(lambda _c, i=h.id: (self.habits.remove(i),
                                               self._refresh_habits()))
        row.addWidget(rm)
        return card

    # ---------- Focus / Pomodoro ----------
    def _build_focus_page(self):
        w, lay = self._panel("Focus")
        self._focus_phase = focus.WORK
        self._focus_remaining = focus.WORK_MINS * 60
        self._focus_running = False
        self._focus_done_sessions = 0
        self.focus_ring = FocusRing()
        lay.addWidget(self.focus_ring, 1, Qt.AlignHCenter)
        self._focus_status = QLabel("Ready when you are.")
        self._focus_status.setObjectName("muted")
        self._focus_status.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self._focus_status)
        row = QHBoxLayout()
        row.addStretch()
        self.focus_btn = QPushButton("Start")
        self.focus_btn.setObjectName("primary")
        self.focus_btn.clicked.connect(self._focus_toggle)
        reset = QPushButton("Reset")
        reset.setObjectName("ghost")
        reset.clicked.connect(self._focus_reset)
        row.addWidget(self.focus_btn)
        row.addWidget(reset)
        row.addStretch()
        lay.addLayout(row)
        self._focus_timer = QTimer(self)
        self._focus_timer.timeout.connect(self._focus_tick)
        self._focus_render()
        return w

    def _focus_render(self):
        total = focus.phase_minutes(self._focus_phase) * 60
        frac = 1.0 - (self._focus_remaining / total) if total else 0.0
        sub = {focus.WORK: "FOCUS", focus.SHORT_BREAK: "BREAK",
               focus.LONG_BREAK: "LONG BREAK"}[self._focus_phase]
        self.focus_ring.set_state(frac, focus.fmt_mmss(self._focus_remaining), sub)

    def _focus_toggle(self):
        if self._focus_running:
            self._focus_timer.stop()
            self._focus_running = False
            self.focus_btn.setText("Resume")
        else:
            self._focus_timer.start(1000)
            self._focus_running = True
            self.focus_btn.setText("Pause")
            self._focus_status.setText("Locked in. You've got this.")

    def _focus_reset(self):
        self._focus_timer.stop()
        self._focus_running = False
        self._focus_phase = focus.WORK
        self._focus_remaining = focus.WORK_MINS * 60
        self.focus_btn.setText("Start")
        self._focus_status.setText("Ready when you are.")
        self._focus_render()

    def _focus_tick(self):
        self._focus_remaining -= 1
        if self._focus_remaining <= 0:
            if self._focus_phase == focus.WORK:
                self._focus_done_sessions += 1
            nxt = focus.next_phase(self._focus_phase, self._focus_done_sessions)
            self._focus_phase = nxt
            self._focus_remaining = focus.phase_minutes(nxt) * 60
            line = focus.phase_line(nxt)
            self._focus_status.setText(line)
            self.log_line(line)
            self.focus_btn.setText("Pause")
            try:
                self._say(line)
            except Exception:
                pass
        self._focus_render()

    # ---------- pages ----------
    def _build_watch_page(self):
        w, lay = self._panel("Watch")
        split = QHBoxLayout()
        split.setSpacing(14)
        split.addWidget(self._build_tasks_widget(), 2)
        split.addWidget(self._build_camera_widget(), 3)
        lay.addLayout(split, 1)
        return w

    def _build_tasks_widget(self):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        lbl = QLabel("MONITORS")
        lbl.setObjectName("field")
        v.addWidget(lbl)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.cards_lay = QVBoxLayout(holder)
        self.cards_lay.setSpacing(10)
        for task in self.engine.tasks():
            card = TaskCard(task, self._check_task, self._retake)
            self.cards[task["name"]] = card
            self.cards_lay.addWidget(card)
        self.cards_lay.addStretch()
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        return box

    def _build_camera_widget(self):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lbl = QLabel("CAMERA")
        lbl.setObjectName("field")
        lay.addWidget(lbl)

        self.preview_label = ClickableLabel(
            "No snapshot yet.\nA frame is captured on each check.\n\n(click to enlarge)")
        self.preview_label.setObjectName("muted")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(340)
        self.preview_label.setStyleSheet(
            f"background:{INK};border-radius:12px;border:1px solid {LINE};")
        self.preview_label.setToolTip("Click to see it full-size with the mess circled")
        self.preview_label.clicked.connect(self._zoom_snapshot)
        lay.addWidget(self.preview_label, 1)

        self.live_btn = QPushButton("Start live view")
        self.live_btn.setObjectName("ghost")
        self.live_btn.setToolTip("Show a live feed to aim the camera (off by default)")
        self.live_btn.clicked.connect(self._toggle_live)
        lay.addWidget(self.live_btn)

        def fl(text):
            l = QLabel(text); l.setObjectName("field"); return l

        grid = QGridLayout()
        grid.setVerticalSpacing(10)
        grid.setHorizontalSpacing(10)
        grid.setColumnStretch(1, 1)
        grid.addWidget(fl("Camera"), 0, 0)
        self.cam_pick = QComboBox()
        self._populate_cameras()
        self.cam_pick.currentIndexChanged.connect(self._on_camera_changed)
        grid.addWidget(self.cam_pick, 0, 1)
        rescan = QPushButton("Rescan")
        rescan.setObjectName("ghost")
        rescan.clicked.connect(self._populate_cameras)
        grid.addWidget(rescan, 0, 2)

        grid.addWidget(fl("Check every"), 1, 0)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 240)
        self.interval_spin.setSuffix(" min")
        self.interval_spin.setValue(self._current_interval())
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        grid.addWidget(self.interval_spin, 1, 1, 1, 2)

        grid.addWidget(fl("Alert after"), 2, 0)
        self.confirm_spin = QSpinBox()
        self.confirm_spin.setRange(1, 5)
        self.confirm_spin.setSuffix(" checks in a row")
        self.confirm_spin.setToolTip(
            "Only alert once a mess shows on this many checks in a row. Higher = "
            "fewer false alarms (someone walking past, briefly setting something "
            "down), but slower to notify. A manual 'Check now' always answers at once.")
        self.confirm_spin.setValue(int(self.engine.cfg.get("alerts", {}).get(
            "confirm_checks", alerts.DEFAULT_CONFIRM_CHECKS)))
        self.confirm_spin.valueChanged.connect(self._on_confirm_changed)
        grid.addWidget(self.confirm_spin, 2, 1, 1, 2)
        lay.addLayout(grid)
        return box

    def _build_activity_page(self):
        w, lay = self._panel("Activity")
        self.feed = QTextEdit()
        self.feed.setReadOnly(True)
        lay.addWidget(self.feed)
        row = QHBoxLayout()
        check_all = QPushButton("Check all now")
        check_all.setObjectName("primary")
        check_all.clicked.connect(self._check_all)
        row.addWidget(check_all)
        snooze = QPushButton("Snooze 1h")
        snooze.clicked.connect(lambda: (self.engine.snooze(60),
                                        self.log_line("Snoozed for 1 hour.")))
        row.addWidget(snooze)
        unsnooze = QPushButton("Resume")
        unsnooze.clicked.connect(lambda: (self.engine.unsnooze(),
                                          self.log_line("Resumed watching.")))
        row.addWidget(unsnooze)
        lay.addLayout(row)
        return w

    def _build_reminders_page(self):
        # The reminders manager IS the page (it brings its own header/controls).
        self.reminders_window = RemindersWindow(self.engine, self.log_line)
        return self.reminders_window

    def _build_chat_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.chat_window = ChatWindow(self.engine, self._chat_frame, self._say)
        v.addWidget(self.chat_window, 1)
        return page

    def _add_system_section(self, lay, fl):
        """System monitor + model/VRAM/quiet-hours controls, added to a layout
        (merged into the Settings tab)."""
        lay.addWidget(self._build_monitor_card())

        grid = QGridLayout()
        grid.setVerticalSpacing(10)
        grid.setHorizontalSpacing(10)
        grid.setColumnStretch(1, 1)

        grid.addWidget(fl("AI model"), 0, 0)
        self.model_pick = QComboBox()
        self.model_pick.addItem("qwen3-vl:8b-instruct  (recommended, ~7 GB)",
                                "qwen3-vl:8b-instruct")
        self.model_pick.addItem("qwen2.5vl:3b  (smallest, ~3 GB)", "qwen2.5vl:3b")
        cur_model = self.engine.cfg.get("ollama", {}).get(
            "model", "qwen3-vl:8b-instruct")
        mi = self.model_pick.findData(cur_model)
        self.model_pick.setCurrentIndex(mi if mi >= 0 else 0)
        self.model_pick.currentIndexChanged.connect(
            lambda _: self._on_model_changed(self.model_pick.currentData()))
        grid.addWidget(self.model_pick, 0, 1)

        grid.addWidget(fl("Keep in VRAM"), 1, 0)
        self.keep_pick = QComboBox()
        self.keep_pick.addItem("Stay loaded while running - no SSD reloads", "24h")
        self.keep_pick.addItem("Free GPU 5 min after each check", "5m")
        self.keep_pick.addItem("Free GPU 30s after each check", "30s")
        self.keep_pick.addItem("Unload after every check", "0")
        self.keep_pick.setToolTip(
            "Staying loaded holds ~5 GB of VRAM the whole time but avoids re-reading "
            "the model from your SSD on every check. Freed automatically on Quit.")
        cur_keep = str(self.engine.cfg.get("ollama", {}).get("keep_alive", "24h"))
        i = self.keep_pick.findData(cur_keep)
        self.keep_pick.setCurrentIndex(i if i >= 0 else 0)
        self.keep_pick.currentIndexChanged.connect(self._on_keep_changed)
        grid.addWidget(self.keep_pick, 1, 1)

        # Do-Not-Disturb: no spoken/pushed alerts between these times.
        qh = self.engine.cfg.get("alerts", {}).get("quiet_hours") or ["22:30", "08:00"]
        grid.addWidget(fl("Quiet from"), 2, 0)
        self.quiet_start = QTimeEdit(QTime(*_parse_hhmm(qh[0])))
        self.quiet_start.setDisplayFormat("h:mm AP")
        self.quiet_start.timeChanged.connect(self._on_quiet_changed)
        grid.addWidget(self.quiet_start, 2, 1)
        grid.addWidget(fl("Quiet until"), 3, 0)
        self.quiet_end = QTimeEdit(QTime(*_parse_hhmm(qh[1])))
        self.quiet_end.setDisplayFormat("h:mm AP")
        self.quiet_end.timeChanged.connect(self._on_quiet_changed)
        grid.addWidget(self.quiet_end, 3, 1)
        lay.addLayout(grid)
        dnd_note = QLabel("During quiet hours checks still run - alerts just stay "
                          "silent (no voice, no phone push).")
        dnd_note.setObjectName("muted")
        dnd_note.setWordWrap(True)
        lay.addWidget(dnd_note)

    # ---------- Settings (System merged in) ----------
    def _build_settings_page(self):
        w, outer = self._panel("Settings")
        # scrollable - Settings now holds System + Appearance + Notifications +
        # Voice input + Config, so it can get tall
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(10)
        outer.addWidget(scroll, 1)
        scroll.setWidget(holder)

        def fl(text):
            l = QLabel(text); l.setObjectName("field"); return l

        def section(title):
            s = QLabel(title)
            s.setObjectName("h1")
            lay.addSpacing(4)
            lay.addWidget(s)

        section("SYSTEM")
        self._add_system_section(lay, fl)

        section("APPEARANCE")
        grid = QGridLayout()
        grid.setVerticalSpacing(10); grid.setHorizontalSpacing(10)
        grid.setColumnStretch(1, 1)

        grid.addWidget(fl("Theme"), 0, 0)
        self.theme_pick = QComboBox()
        for name in THEMES:
            self.theme_pick.addItem(name)
        cur = self.engine.cfg.get("ui", {}).get("theme", DEFAULT_THEME)
        self.theme_pick.setCurrentText(cur if cur in THEMES else DEFAULT_THEME)
        self.theme_pick.currentTextChanged.connect(self._on_theme_changed)
        grid.addWidget(self.theme_pick, 0, 1)

        grid.addWidget(fl("Animations"), 1, 0)
        self.anim_chk = QCheckBox("Aurora background + smooth transitions")
        self.anim_chk.setChecked(ANIMATIONS_ON)
        self.anim_chk.setToolTip("Turn off for max performance / while gaming.")
        self.anim_chk.stateChanged.connect(self._on_animations_toggled)
        grid.addWidget(self.anim_chk, 1, 1)
        lay.addLayout(grid)

        section("NOTIFICATIONS")
        g2 = QGridLayout()
        g2.setVerticalSpacing(10); g2.setHorizontalSpacing(10)
        g2.setColumnStretch(1, 1)
        g2.addWidget(fl("Phone push topic"), 0, 0)
        self.ntfy_edit = QLineEdit(
            self.engine.cfg.get("alerts", {}).get("ntfy_topic", ""))
        self.ntfy_edit.setPlaceholderText("e.g. my-room-x7q2k  (subscribe in the ntfy app)")
        self.ntfy_edit.setToolTip("Free phone alerts: install the ntfy app, subscribe "
                                  "to this exact topic. Leave blank to disable.")
        self.ntfy_edit.editingFinished.connect(self._on_ntfy_changed)
        g2.addWidget(self.ntfy_edit, 0, 1)
        lay.addLayout(g2)

        section("VOICE")
        g3 = QGridLayout()
        g3.setVerticalSpacing(10); g3.setHorizontalSpacing(10)
        g3.setColumnStretch(1, 1)
        g3.addWidget(fl("Spoken alerts"), 0, 0)
        self.voice_pick = QComboBox()
        self.voice_pick.addItem("Off", speech.VOICE_OFF)
        self.voice_pick.addItem("System voice", speech.VOICE_SYSTEM)
        self.voice_pick.addItem("Wall-E voice", speech.VOICE_WALL_E)
        self.voice_pick.setToolTip(
            "System voice uses your OS text-to-speech. Wall-E voice runs the "
            "same speech through a robot effect. All local.")
        cur_voice = self.engine.cfg.get("voice", {}).get("voice",
                                                         speech.VOICE_SYSTEM)
        vi = self.voice_pick.findData(cur_voice)
        self.voice_pick.setCurrentIndex(vi if vi >= 0 else 1)
        self.voice_pick.currentIndexChanged.connect(
            lambda _=None: self._on_voice_changed(self.voice_pick.currentData()))
        g3.addWidget(self.voice_pick, 0, 1)
        test_voice = QPushButton("Test")
        test_voice.setObjectName("ghost")
        test_voice.clicked.connect(self._test_voice)
        g3.addWidget(test_voice, 0, 2)
        lay.addLayout(g3)

        section("CONFIG")
        row = QHBoxLayout()
        edit = QPushButton("Edit config file...")
        edit.setObjectName("ghost")
        edit.clicked.connect(self._open_config)
        reload = QPushButton("Reload config")
        reload.setObjectName("ghost")
        reload.clicked.connect(self._reload_config)
        row.addWidget(edit); row.addWidget(reload); row.addStretch()
        lay.addLayout(row)

        about = QLabel(f"{APP_NAME} \u00b7 fully local AI \u00b7 running on Ollama")
        about.setObjectName("muted")
        lay.addWidget(about)
        lay.addStretch()
        return w

    def _on_theme_changed(self, name):
        qss = apply_theme(name)
        self.setStyleSheet(qss)
        for win in (self.chat_window, self.reminders_window):
            if win is not None:
                win.setStyleSheet(qss)
        self.engine.cfg.setdefault("ui", {})["theme"] = name
        self._save_config()
        self.update()
        self.stack.update()
        self.log_line(f"Theme set to <b>{name}</b>.")

    def _on_animations_toggled(self, state):
        global ANIMATIONS_ON
        ANIMATIONS_ON = bool(state)
        self.engine.cfg.setdefault("ui", {})["animations"] = ANIMATIONS_ON
        self._save_config()
        # kick the current page so the aurora starts/stops immediately
        cur = self.stack.currentWidget()
        if cur is not None:
            cur.update()
        self.log_line("Animations " + ("on." if ANIMATIONS_ON else "off."))

    def _on_ntfy_changed(self):
        topic = self.ntfy_edit.text().strip()
        self.engine.cfg.setdefault("alerts", {})["ntfy_topic"] = topic
        self._save_config()
        self.log_line("Phone-push topic " + ("saved." if topic else "cleared."))

    def _reload_config(self):
        try:
            self.engine.load_config()
            self.log_line("Config reloaded from disk.")
        except Exception as e:
            self.log_line(f"<span style='color:{ALERT};'>Config error: {_esc(str(e))}</span>")

    # ---------- camera handling ----------
    def _camera_names(self):
        return list(self.engine.cfg.get("cameras", {}).keys())

    def _current_source(self):
        cams = self.engine.cfg.get("cameras", {})
        if getattr(self, "_preview_cam", None) in cams:
            return cams[self._preview_cam].get("source", 0)
        first = next(iter(cams.values())) if cams else {"source": 0}
        return first.get("source", 0)

    def _populate_cameras(self):
        self.cam_pick.blockSignals(True)
        self.cam_pick.clear()
        self._cam_sources = []
        self._cam_names = []   # config name per row, None = unconfigured USB cam
        # configured named cameras
        for name, c in self.engine.cfg.get("cameras", {}).items():
            self.cam_pick.addItem(f"{name}  (source {c.get('source')})")
            self._cam_sources.append(c.get("source", 0))
            self._cam_names.append(name)
        # auto-detected webcams not already listed
        for idx in camera.list_webcams():
            if idx not in self._cam_sources:
                self.cam_pick.addItem(f"USB webcam #{idx}")
                self._cam_sources.append(idx)
                self._cam_names.append(None)
        self.cam_pick.blockSignals(False)

    def _on_camera_changed(self, i):
        if not (0 <= i < len(self._cam_sources)):
            return
        src = self._cam_sources[i]
        name = self._cam_names[i]
        self.preview.set_source(src)
        if name is not None:
            # A configured camera: just point the preview at that angle -
            # never rewrite another camera's source.
            self._preview_cam = name
            self.log_line(f"Previewing camera '{name}' (source {src!r}).")
            return
        # An unconfigured USB webcam: adopt it as the FIRST camera's source
        cams = self.engine.cfg.setdefault("cameras", {})
        if cams:
            first_key = next(iter(cams))
            cams[first_key]["source"] = src
            self._preview_cam = first_key
            self._save_config()
            self.log_line(f"Camera '{first_key}' switched to source {src!r} "
                          "and saved.")

    def _toggle_live(self):
        """Live view is opt-in: only hold the camera open while the user wants it."""
        if self.live_on:
            self.preview.stop()
            self.live_on = False
            self.live_btn.setText("Start live view")
            self.live_btn.setObjectName("ghost")
            self.live_btn.setStyleSheet("")
            self.preview_label.setText("Live view off. Showing last snapshot.")
            if self._last_snapshot:
                self.preview_label.setPixmap(
                    jpeg_to_pixmap(self._last_snapshot, PREVIEW_W, self._last_regions))
                self.preview_label.setText("")
        else:
            self.preview.set_source(self._current_source())
            if not self.preview.isRunning():
                self.preview.start()
            self.live_on = True
            self.live_btn.setText("Stop live view")

    def _on_preview(self, frame):
        self._last_preview = frame
        if self.live_on:
            self.preview_label.setPixmap(bgr_to_pixmap(frame, PREVIEW_W))
            self.preview_label.setText("")

    def _zoom_snapshot(self):
        """Open the last room frame full-size with the mess circled."""
        jpeg = self._last_snapshot
        if jpeg is None and self._last_preview is not None:
            jpeg = camera.encode_frame(self._last_preview, None)
        if jpeg is None:
            return
        when = dt.datetime.now().strftime("%b %d, %I:%M %p")
        self._zoom = ZoomDialog(jpeg, self._last_regions, when)
        self._zoom.showFullScreen()

    def _provide_frame(self, cam_name, zone):
        """Feed the engine a frame ONLY while live view is on (avoids opening the
        camera twice) AND the preview is showing that same camera - with several
        cameras configured, the live frame is one angle only. Otherwise return
        None so the engine grabs its own frame and releases the camera."""
        if (self.live_on and self._last_preview is not None
                and cam_name == getattr(self, "_preview_cam", cam_name)):
            return camera.encode_frame(self._last_preview, zone)
        return None

    # ---------- settings writers ----------
    def _save_config(self):
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            doc = yaml.load(f)
        # mirror the live edits we track
        if "cameras" in self.engine.cfg:
            for k, v in self.engine.cfg["cameras"].items():
                if k in doc.get("cameras", {}):
                    doc["cameras"][k]["source"] = v.get("source")
        doc.setdefault("ollama", {})["model"] = self.model_pick.currentData()
        doc["ollama"]["keep_alive"] = self.keep_pick.currentData()
        for t in doc.get("tasks", []):
            if t.get("enabled", True):
                t["interval_minutes"] = self.interval_spin.value()
        # voice choice (off / system / wall-e); rate & voice_name stay hand-edited
        vcfg = self.engine.cfg.get("voice", {})
        if "voice" in vcfg:
            doc.setdefault("voice", {})["voice"] = vcfg["voice"]
        doc.setdefault("alerts", {})["confirm_checks"] = self.confirm_spin.value()
        if self.engine.cfg.get("alerts", {}).get("quiet_hours"):
            from ruamel.yaml.scalarstring import DoubleQuotedScalarString as _DQ
            qh = self.engine.cfg["alerts"]["quiet_hours"]
            # quote them so YAML keeps 'HH:MM' as a string (unquoted 22:30 -> 1350)
            doc["alerts"]["quiet_hours"] = [
                _DQ("%02d:%02d" % _parse_hhmm(x)) for x in qh]
        # phone-push topic (Settings tab)
        if "ntfy_topic" in self.engine.cfg.get("alerts", {}):
            doc["alerts"]["ntfy_topic"] = self.engine.cfg["alerts"]["ntfy_topic"]
        # UI prefs (theme + animations)
        uicfg = self.engine.cfg.get("ui", {})
        if uicfg:
            doc.setdefault("ui", {})
            for k in ("theme", "animations"):
                if k in uicfg:
                    doc["ui"][k] = uicfg[k]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(doc, f)

    def _current_interval(self):
        tasks = self.engine.tasks()
        return int(tasks[0].get("interval_minutes", 10)) if tasks else 10

    def _on_interval_changed(self, mins):
        # apply only to ENABLED tasks (leave the disabled examples alone)
        for t in self.engine.cfg.get("tasks", []):
            if t.get("enabled", True):
                t["interval_minutes"] = mins
        now = time.time()
        for name in list(self.engine.next_due):
            self.engine.next_due[name] = now + mins * 60
        self._save_config()
        self.log_line(f"Check interval set to every {mins} min.")

    def _on_model_changed(self, m):
        old = self.engine.cfg.setdefault("ollama", {}).get("model")
        self.engine.cfg["ollama"]["model"] = m
        self._save_config()
        if old and old != m:
            vision.unload(old, self.engine.cfg["ollama"].get("url",
                          "http://127.0.0.1:11434"))   # free the old one now
        self.log_line(f"AI model set to {m}.")

    def _on_keep_changed(self, _):
        val = self.keep_pick.currentData()
        self.engine.cfg.setdefault("ollama", {})["keep_alive"] = val
        self._save_config()
        self.log_line(f"VRAM policy: {self.keep_pick.currentText()}.")

    def _on_quiet_changed(self, _=None):
        s = self.quiet_start.time().toString("HH:mm")
        e = self.quiet_end.time().toString("HH:mm")
        self.engine.cfg.setdefault("alerts", {})["quiet_hours"] = [s, e]
        self._save_config()
        self.log_line(f"Quiet hours set: {s} - {e}.")

    def _on_confirm_changed(self, n):
        self.engine.cfg.setdefault("alerts", {})["confirm_checks"] = n
        self._save_config()
        if n == 1:
            self.log_line("Alerting on the first messy check.")
        else:
            self.log_line(f"Alerting only after {n} messy checks in a row.")

    def _say(self, text):
        """Speak `text` with the configured voice (non-blocking, best-effort)."""
        speech.speak(text, self.engine.cfg.get("voice", {}))

    def _on_voice_changed(self, choice):
        self.engine.cfg.setdefault("voice", {})["voice"] = choice
        self._save_config()
        self.log_line(f"Voice set to {self.voice_pick.currentText()}.")

    def _test_voice(self):
        choice = self.voice_pick.currentData()
        if choice == speech.VOICE_OFF:
            self.log_line("Voice is off - pick a voice to test it.")
            return
        self.log_line(f"Testing voice - {self.voice_pick.currentText()}.")
        # Test the SELECTED voice even if the config hasn't been saved yet.
        cfg = dict(self.engine.cfg.get("voice", {}))
        cfg["voice"] = choice
        speech.speak(f"Hello, this is {APP_NAME}. I'm watching the room.", cfg)

    def _open_chat(self):
        """Bring the window forward and switch to the Chat tab (tray shortcut)."""
        self._show_window()
        self._select_page("chat")
        if self.chat_window is not None:
            self.chat_window.input.setFocus()

    def _open_reminders(self):
        self._show_window()
        self._select_page("planner")
        self._select_sub("planner", "reminders")

    # ---------- reminder popups ----------
    def _show_reminder_popup(self, text, rid=None):
        toast = ReminderToast(text, self._toast_closed, self._toast_snooze)
        self._toasts.append(toast)
        self._position_toasts()
        toast.show()

    def _position_toasts(self):
        """Stack toasts down the top-right corner of the primary screen."""
        screen = QApplication.primaryScreen().availableGeometry()
        margin = 18
        y = screen.top() + margin
        for t in self._toasts:
            t.adjustSize()
            x = screen.right() - t.width() - margin
            t.move(x, y)
            y += t.height() + 8

    def _toast_closed(self, toast):
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._position_toasts()

    def _toast_snooze(self, text):
        # re-pop the same reminder in 10 minutes
        QTimer.singleShot(10 * 60 * 1000, lambda: self._show_reminder_popup(text))
        self.log_line(f"Snoozed reminder for 10 min: {_esc(text)}")

    def _chat_frame(self):
        """A current camera frame for the chat (so Wall-Eye can 'see')."""
        if self.live_on and self._last_preview is not None:
            return camera.encode_frame(self._last_preview, None)
        cams = self.engine.cfg.get("cameras", {})
        if not cams:
            return None
        cam = next(iter(cams.values()))
        try:
            with self.engine.cam_lock:
                return camera.grab_frame(cam["source"], cam.get("width", 1280),
                                         cam.get("height", 720),
                                         cam.get("warmup_frames", 5))
        except camera.CameraError:
            return self._last_snapshot   # fall back to the last check's frame

    def _open_config(self):
        import subprocess
        subprocess.Popen(["notepad", str(CONFIG_FILE)])

    def _pulse_dot(self):
        if not ANIMATIONS_ON or not hasattr(self, "_dot"):
            return
        self._pulse_t += 0.09
        # breathe the dot's size/brightness between a dim and bright accent
        level = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._pulse_t))
        c = QColor(ACCENT)
        c = QColor(int(c.red() * level), int(c.green() * level),
                   int(c.blue() * level))
        self._dot.setStyleSheet(f"color:{c.name()}; font-size:14px;")

    def _tick_clock(self):
        """Cheap UI-thread update: header clock every second; the Home briefing
        (clock + greeting + reminders + room) refreshes ~every 15s."""
        now = dt.datetime.now()
        self.clock_pill.setText(now.strftime("%#I:%M:%S %p"))
        if hasattr(self, "brief_clock"):
            self.brief_clock.setText(briefing.clock_line(now))
            self.brief_ampm.setText(briefing.ampm(now))
        self._brief_ticks = getattr(self, "_brief_ticks", 0) + 1
        if self._brief_ticks % 15 == 0:
            self._refresh_briefing()

    def _on_stats(self, s):
        """Apply a system-monitor sample produced by SysMonWorker (background)."""
        cpu, mem, gpu = s["cpu"], s["memory"], s["gpu"]
        self.cpu_pill.setText(f"CPU {cpu['percent']:.0f}%")
        self.ram_pill.setText(f"RAM {mem['percent']:.0f}%")
        if gpu.get("error"):
            self.gpu_pill.setText("GPU offline")
        elif gpu["loaded"]:
            self.gpu_pill.setText(f"GPU {gpu['vram_gb']:.1f} GB")
        else:
            self.gpu_pill.setText("GPU idle")

        self.cpu_bar.setValue(int(cpu["percent"]))
        self.ram_bar.setValue(int(mem["percent"]))
        self.ram_detail.setText(f"{mem['used_gb']:.1f} / {mem['total_gb']:.1f} GB")
        if gpu.get("error"):
            self.gpu_detail.setText("Ollama not reachable")
        elif gpu["loaded"]:
            self.gpu_detail.setText(
                f"{', '.join(gpu['names'])} - {gpu['vram_gb']:.1f} GB in VRAM")
        else:
            self.gpu_detail.setText("idle - no model loaded")

    # ---------- actions ----------
    def _check_task(self, task):
        import threading
        self.log_line(f"Checking {task['name']}...")
        threading.Thread(target=self.engine.check_task, args=(task, True),
                         daemon=True).start()

    def _check_all(self):
        for t in self.engine.tasks():
            self._check_task(t)

    def _retake(self, task):
        import threading
        def run():
            self.engine.retake_reference(task)
            ev = {"kind": "ref", "task": task["name"], "time": time.time(),
                  "text": "reference updated"}
            self.bridge.event.emit(ev)
        threading.Thread(target=run, daemon=True).start()

    # ---------- events / feed ----------
    def log_line(self, text):
        ts = dt.datetime.now().strftime("%H:%M")
        self.feed.append(
            f"<div style='margin:0 0 10px 0; line-height:140%;'>"
            f"<span style='color:{MUTED}; font-size:11px;'>{ts}</span>"
            f"&nbsp;&nbsp;{text}</div>")
        sb = self.feed.verticalScrollBar()
        sb.setValue(sb.maximum())

    def on_event(self, ev):
        kind = ev.get("kind")
        # latest check frame becomes the snapshot shown when live view is off
        if kind == "check" and ev.get("frame"):
            self._last_snapshot = ev["frame"]
            self._last_regions = ev.get("regions") if ev.get("alert") else None
            if not self.live_on:
                self.preview_label.setPixmap(
                    jpeg_to_pixmap(ev["frame"], PREVIEW_W, self._last_regions))
                self.preview_label.setText("")
            if hasattr(self, "_refresh_timeline"):
                self._refresh_timeline()
        if kind == "check":
            # feed the Home briefing's room-status card
            self._room_clear = not ev.get("alert")
            self._room_text = "; ".join(ev.get("items") or []) or ev.get("text", "")
            self._refresh_briefing()
        if kind in ("reminder", "reminder_added"):
            self._refresh_briefing()
        if kind == "cleanup":
            self._refresh_cleanup()
            if ev.get("task_id"):
                self.log_line(f"New cleanup task: {_esc(ev['text'])} "
                              f"- answer it on the Cleanup tab.")
        if kind in ("check", "alert", "ref") and ev.get("task") in self.cards:
            if kind != "alert":   # alert duplicates the check line
                self.cards[ev["task"]].update_from_event(ev)
        if kind == "check":
            tag = (f"<span style='color:{ALERT}; font-weight:600;'>messy</span>"
                   if ev.get("alert")
                   else f"<span style='color:{OKGREEN}; font-weight:600;'>clear</span>")
            items = "; ".join(ev.get("items") or [])
            extra = (f"<br><span style='color:{MUTED};'>{items}</span>"
                     if items else "")
            self.log_line(f"<b style='color:{TEXT};'>{ev['task']}</b> &middot; {tag}"
                          f"<br><span style='color:{MUTED};'>{ev['text']}</span>{extra}")
        elif kind == "alert":
            self.cards.get(ev["task"]) and self.cards[ev["task"]].setProperty("alert", "true")
        elif kind == "muted":
            if ev.get("reason") == "confirming":
                # persistence filter: seen once, waiting for a repeat before alerting
                self.log_line(
                    f"<span style='color:{MUTED};'><b style='color:{TEXT};'>"
                    f"{ev['task']}</b> looks messy - confirming before I alert "
                    f"({ev['text']}).</span>")
            else:
                self.log_line(
                    f"<span style='color:{MUTED};'><b style='color:{TEXT};'>"
                    f"{ev['task']}</b> is messy, but the alert was held "
                    f"({ev['text']}).</span>")
        elif kind == "chat":
            user = ev.get("user")
            if user:
                self.log_line(
                    f"<span style='color:{OKGREEN};font-weight:600;'>You</span>"
                    f"<br><span style='color:{MUTED};'>{_esc(user)}</span>")
            self.log_line(
                f"<span style='color:{ACCENT};font-weight:600;'>{APP_NAME}</span>"
                f"<br><span style='color:{TEXT};'>{_esc(ev['text'])}</span>")
        elif kind == "error":
            # escape so camera/vision error text renders verbatim in the feed
            self.log_line(
                f"<span style='color:{ALERT};'><b>{ev['task']}</b> - "
                f"{_esc(ev['text'])}</span>")
        elif kind == "reminder":
            self.log_line(f"Reminder: {_esc(ev['text'])}")
            self._show_reminder_popup(ev["text"], ev.get("reminder_id"))
        elif kind == "reminder_added":
            self.log_line(f"New reminder set: {_esc(ev['text'])}")
            if self.reminders_window is not None:
                self.reminders_window._refresh()
        elif kind == "ref":
            self.log_line(f"New reference saved for <b>{ev['task']}</b>.")

    # ---------- tray ----------
    def _build_tray(self):
        icon = QIcon(str((CONFIG_FILE.parent / "icon.ico")))
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        show = QAction("Open dashboard", self)
        show.triggered.connect(self._show_window)
        menu.addAction(show)
        chat_a = QAction(f"Chat with {APP_NAME}", self)
        chat_a.triggered.connect(self._open_chat)
        menu.addAction(chat_a)
        menu.addSeparator()
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self._quit)
        menu.addAction(quit_a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._show_window()
            if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _set_display_active(self, active):
        """Pause/resume all the purely-visual background work. While the window
        is hidden in the tray (its usual state) there's no point ticking the
        clock, breathing the dot, or polling Ollama every 2s - the room-watch
        scheduler runs independently in the engine and is untouched."""
        if not hasattr(self, "sysmon_worker"):
            return   # still constructing
        if active:
            self.clock_timer.start(1000)
            self.pulse_timer.start(130)
            self.sysmon_worker.resume()
            self._tick_clock()
            self._refresh_briefing()
            if getattr(self, "_timeline_dirty", False):
                self._refresh_timeline()   # catch up on checks missed while hidden
        else:
            self.clock_timer.stop()
            self.pulse_timer.stop()
            self.sysmon_worker.pause()

    def showEvent(self, e):
        super().showEvent(e)
        self._set_display_active(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._set_display_active(False)

    def closeEvent(self, e):
        # hide to tray instead of quitting
        e.ignore()
        self.hide()   # triggers hideEvent -> pauses display work
        self.tray.showMessage(APP_NAME, "Still watching - in the tray.",
                              QSystemTrayIcon.Information, 2000)

    def _quit(self):
        self.preview.stop()
        self.sysmon_worker.stop()
        self.engine.stop_event.set()
        self.engine.shutdown()   # unload model from VRAM
        QApplication.quit()


# ---- crash reporting -------------------------------------------------------
class BugReportDialog(QDialog):
    """Modal crash report: everything a GitHub issue needs, one click to copy."""

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} hit a bug")
        self.setModal(True)
        self.resize(680, 480)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        intro = QLabel(
            f"{APP_NAME} {VERSION} ran into an unexpected error "
            f"({errors.format_platform_info()}). The app may keep working, "
            "but please report this so it can be fixed.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        box = QPlainTextEdit()
        box.setPlainText(report)
        box.setReadOnly(True)
        box.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        box.setFont(mono)
        lay.addWidget(box, 1)

        note = QLabel("Click \"Copy details\", then paste the report into a "
                      "new GitHub issue on the Wall-Eye repository.")
        note.setWordWrap(True)
        lay.addWidget(note)

        row = QHBoxLayout()
        copy_btn = QPushButton("Copy details")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(report))
        row.addWidget(copy_btn)
        row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        lay.addLayout(row)


def _show_crash_dialog(report):
    """Qt-safe presenter for errors.install_excepthook: only pops the dialog
    when a QApplication exists and we are on the GUI thread. Raising here is
    fine - the excepthook catches it and falls back to stderr."""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("no QApplication yet - cannot show the crash dialog")
    if QThread.currentThread() is not app.thread():
        raise RuntimeError("crash on a worker thread - dialog would be unsafe")
    BugReportDialog(report).exec()


def _acquire_single_instance():
    """Return a held handle if we're the ONLY Wall-Eye dashboard, else None.
    Uses a Windows named mutex (survives only while this process lives), so a
    second launch - e.g. the Startup shortcut plus a manual double-click -
    detects the first and bows out instead of adding a duplicate tray icon."""
    if sys.platform != "win32":
        return True
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    h = ctypes.windll.kernel32.CreateMutexW(None, False,
                                            "WallEye_Dashboard_Singleton")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return h   # keep the handle alive for the process lifetime


def main():
    # Crash reporting first, so even startup failures produce a usable report
    # (log + dialog; the hook falls back to stderr if the dialog can't show).
    errors.install_excepthook(_show_crash_dialog)
    _single = _acquire_single_instance()
    if _single is None:
        # Another dashboard is already running - don't stack a second tray icon.
        try:
            import notify
            notify.toast(APP_NAME,
                         f"{APP_NAME} is already running (check the tray).")
        except Exception:
            pass
        return
    main._single_instance = _single   # anchor the mutex handle to module scope

    engine = WallEye()
    threading.Thread(target=engine.scheduler_loop, daemon=True).start()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # Guarantee the model is freed from VRAM whenever the app exits, by any path.
    app.aboutToQuit.connect(engine.shutdown)
    win = Dashboard(engine)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
