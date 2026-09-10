"""The floating layer: an always-on-top orb, and an edge glow for listening.

MARK LII normally runs with no window at all. What stays on screen is
`FloatingOrb` -- a small frameless, translucent, always-on-top widget that can
be dragged with the mouse or with a bare-hand pinch, and that opens the full
hub only when asked. Everything else (voice, tools, integrations, the remote
dashboard) runs whether or not the hub window exists, because the backend only
ever talks to the `JarvisUI` facade.

`ScreenGlow` is the second half: a click-through, full-screen border that
breathes colour around the edges of the display while the assistant is
listening, in the manner of the Apple Intelligence indicator.
"""
from __future__ import annotations

import math
import random
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QConicalGradient, QCursor, QFont, QImage, QLinearGradient,
    QPainter, QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

ORB_SIZE = 168          # the orb box; the orb itself is inset for glow room
ORB_FOOT = 46           # status line / action strip below the orb
PANEL_W = 246           # the expanded side panel
AGENTS_W = 300          # the agent launcher
AGENT_ROW = 58          # one agent sub-panel
AGENT_PAD = 12
GLOW_BAND = 88.0        # thickness of the screen-edge aurora, in px

# The orb is painted at ORB_SIZE and then scaled, so every dimension in this
# file stays a design coordinate and only two places know about the zoom: the
# transform in paintEvent and the inverse in _zone_at.
ORB_SCALE_MIN = 0.55
ORB_SCALE_MAX = 2.60

# The Apple-Intelligence signature: warm pink -> violet -> blue, sampled at the
# stock cyan accent. These are reference hues, not the painted ones -- see
# aurora_ribbon(), which rotates the whole ribbon onto whatever accent the user
# has chosen so the screen edge always matches the rest of the UI.
_AURORA = ("#ff4d8d", "#a95cff", "#3d7bff", "#00d4ff", "#ff9d3d")

# The accent these reference hues were picked against. Kept as a literal rather
# than imported from ui.C: C.PRI is rewritten in place when the user changes
# colour, so reading it here would make the offsets drift on every change.
_AURORA_BASE = "#00d4ff"


def _C():
    """The live palette. Imported lazily so ui.py can import this module."""
    from ui import C
    return C


# macOS window levels. A Qt::Tool window lands on NSFloatingWindowLevel (3),
# which is above ordinary windows but *below* the menu bar and the Dock — so a
# full-screen effect gets its top and bottom edges clipped. Going to 25
# (NSScreenSaverWindowLevel - 1) puts the glow over the entire desktop.
_NS_LEVEL_OVERLAY = 25
# CanJoinAllSpaces | Stationary | FullScreenAuxiliary: follow the user across
# Spaces, don't slide with them, and stay up over a full-screened app.
_NS_BEHAVIOR_ALL_SPACES = (1 << 0) | (1 << 4) | (1 << 8)


def raise_above_menubar(widget, level: int = _NS_LEVEL_OVERLAY) -> bool:
    """Lift a native window above the macOS menu bar and onto every Space.

    Qt has no API for either, so this reaches through the NSView that backs
    the widget and talks to AppKit directly. Every failure mode is silent and
    non-fatal: without it the window simply behaves as an ordinary floating
    panel, which is what it did before.
    """
    import platform
    if platform.system() != "Darwin":
        return False
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        view = ctypes.c_void_p(int(widget.winId()))
        if not view.value:
            return False

        # id window = [view window]
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        window = objc.objc_msgSend(view, objc.sel_registerName(b"window"))
        if not window:
            return False

        # [window setLevel:level] / [window setCollectionBehavior:...]
        objc.objc_msgSend.restype = None
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long
        ]
        objc.objc_msgSend(
            ctypes.c_void_p(window), objc.sel_registerName(b"setLevel:"), level
        )
        objc.objc_msgSend(
            ctypes.c_void_p(window),
            objc.sel_registerName(b"setCollectionBehavior:"),
            _NS_BEHAVIOR_ALL_SPACES,
        )
        return True
    except Exception as exc:
        print(f"[Overlay] Could not raise window above the menu bar: {exc}")
        return False


def _col(hex_str: str, alpha: int = 255) -> QColor:
    c = QColor(hex_str)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
        int(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _hue_offsets() -> list[tuple[float, float, float]]:
    """Each signature colour as (hue offset from the base accent, sat, val).

    Storing the ribbon as *offsets* rather than absolute hues is what lets it
    follow the accent: rotate every offset onto the new accent's hue and the
    gradient keeps its exact shape and spacing, just centred somewhere else on
    the wheel.
    """
    base_h = QColor(_AURORA_BASE).hsvHueF()
    out = []
    for hexc in _AURORA:
        c = QColor(hexc)
        out.append(((c.hsvHueF() - base_h) % 1.0, c.hsvSaturationF(), c.valueF()))
    return out


_AURORA_OFFSETS = _hue_offsets()

# aurora_ribbon() runs inside paintEvent at 30fps. The accent only changes when
# the user drags the hue wheel, so memoise on it and the per-frame cost drops
# to one dict lookup.
_ribbon_cache: dict[str, list[QColor]] = {}


def aurora_ribbon(accent_hex: str) -> list[QColor]:
    """The aurora gradient, rotated onto `accent_hex`.

    A near-grey accent desaturates the ribbon the same way apply_ui_accent()
    desaturates the rest of the palette, so choosing a monochrome theme gives a
    monochrome glow instead of a rainbow that matches nothing on screen.
    """
    key = (accent_hex or "").strip().lower()
    got = _ribbon_cache.get(key)
    if got is not None:
        return got

    accent = QColor(key)
    if not accent.isValid():
        accent = QColor(_AURORA_BASE)
    acc_s = accent.hsvSaturationF()
    grey = acc_s < 0.08
    # A pure grey has no hue at all (Qt reports -1). Rotating by it would be
    # meaningless, so hold the ribbon at its stock angle and let the
    # desaturation below do the work -- the same thing apply_ui_accent() does
    # for the rest of the palette.
    acc_h = accent.hsvHueF()
    if acc_h < 0:
        acc_h = QColor(_AURORA_BASE).hsvHueF()

    ribbon = []
    for dh, sat, val in _AURORA_OFFSETS:
        c = QColor()
        c.setHsvF((acc_h + dh) % 1.0, min(1.0, sat * (0.15 if grey else 1.0)), val)
        ribbon.append(c)

    if len(_ribbon_cache) > 32:
        _ribbon_cache.clear()
    _ribbon_cache[key] = ribbon
    return ribbon


# ────────────────────────────────────────────────────────────────────────────
#  Screen-edge aurora
# ────────────────────────────────────────────────────────────────────────────
class ScreenGlow(QWidget):
    """A click-through aurora around the edge of the screen.

    Shown while the assistant is listening or speaking; the band's width and
    opacity ride the live audio level, so it breathes with the voice. The
    window hides itself once the fade-out completes, so an idle assistant
    costs nothing.
    """

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # A Qt::Tool window is an NSPanel on macOS, and an NSPanel hides itself
        # the moment the owning app goes inactive. Without this the aurora
        # would vanish whenever the user clicked into another app -- i.e. all
        # the time, since that is exactly when it is worth showing.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)

        self._phase = 0.0
        self._target = 0.0      # 0..1 -- how present the aurora should be
        self._level = 0.0       # eased actual
        self._amp = 0.0         # live audio 0..1
        self._amp_disp = 0.0
        self._speaking = False

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)

    # -- API -----------------------------------------------------------------

    def set_active(self, active: bool, speaking: bool = False) -> None:
        self._speaking = speaking
        self._target = 1.0 if active else 0.0
        if active and not self.isVisible():
            scr = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
            if scr is not None:
                # The full screen, not the available area: the aurora frames the
                # desktop itself, menu bar and Dock included. raise_above_menubar
                # is what makes those edges actually visible.
                self.setGeometry(scr.geometry())
            self.show()
            raise_above_menubar(self)
        if active and not self._tmr.isActive():
            self._tmr.start(33)

    def set_audio_level(self, level: float) -> None:
        try:
            lv = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return
        if lv > self._amp:
            self._amp = lv

    # -- animation -----------------------------------------------------------

    def _step(self) -> None:
        self._amp *= 0.86
        self._amp_disp += (self._amp - self._amp_disp) * 0.35
        self._level += (self._target - self._level) * 0.14
        self._phase = (self._phase + (0.020 if self._speaking else 0.010)) % 1.0

        if self._target == 0.0 and self._level < 0.01:
            self._level = 0.0
            self._tmr.stop()
            self.hide()
            return
        self.update()

    # -- paint ---------------------------------------------------------------

    def paintEvent(self, _) -> None:
        """Paint the aurora as one smooth field, not a stack of strokes.

        The hue comes from a conical gradient swept around the screen centre;
        the shape comes from a separate alpha mask that fades from the edges
        inward. Multiplying the two (DestinationIn) gives a continuous falloff
        with no concentric banding. Both are built on a small off-screen image
        and scaled up -- a soft glow survives the resample, and this keeps a
        full-screen effect off the critical path on a Retina display.
        """
        if self._level <= 0.005:
            return
        p = QPainter(self)
        if not p.isActive():
            return

        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        scale = min(1.0, 420.0 / max(W, H))
        lw, lh = max(int(W * scale), 8), max(int(H * scale), 8)

        strength = self._level * (0.55 + 0.45 * self._amp_disp)
        band = GLOW_BAND * (0.8 + 0.5 * self._amp_disp) * self._level * scale

        layer = QImage(lw, lh, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.RenderHint.Antialiasing)

        # The ribbon is already rotated onto the live accent, so the old
        # blend-toward-PRI is gone: it was there to drag a fixed rainbow back
        # towards the theme, and it only ever muddied the result.
        pri = _col(_C().PRI)
        stops = aurora_ribbon(pri.name())
        grad = QConicalGradient(QPointF(lw / 2, lh / 2), -self._phase * 360.0)
        for i, col in enumerate(stops + [stops[0]]):
            grad.setColorAt(min(i / len(stops), 1.0), col)
        lp.fillRect(0, 0, lw, lh, grad)

        lp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        lp.drawImage(0, 0, self._edge_mask(lw, lh, band))
        lp.end()

        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(min(1.0, strength))
        p.drawImage(QRectF(0, 0, W, H), layer)
        p.setOpacity(1.0)

    @staticmethod
    def _edge_mask(w: int, h: int, band: float) -> QImage:
        """An alpha ramp: opaque at every edge, transparent `band` px inward.

        The four edges are drawn with Plus so the corners accumulate instead of
        overwriting each other, which is what keeps the corner glow from
        showing a seam.
        """
        mask = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        mask.fill(Qt.GlobalColor.transparent)
        mp = QPainter(mask)
        mp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        band = max(band, 2.0)

        white = QColor(255, 255, 255, 255)
        clear = QColor(255, 255, 255, 0)
        # Two mid-stops bend the ramp into a soft shoulder instead of a
        # straight line, which is what makes it read as light rather than paint.
        ramp = ((0.0, 255), (0.28, 150), (0.62, 42), (1.0, 0))

        edges = (
            ((0, 0), (0, band), QRectF(0, 0, w, band)),
            ((w, 0), (w - band, 0), QRectF(w - band, 0, band, h)),
            ((0, h), (0, h - band), QRectF(0, h - band, w, band)),
            ((0, 0), (band, 0), QRectF(0, 0, band, h)),
        )
        for (x0, y0), (x1, y1), rect in edges:
            g = QLinearGradient(QPointF(x0, y0), QPointF(x1, y1))
            for pos, alpha in ramp:
                c = QColor(white if alpha else clear)
                c.setAlpha(alpha)
                g.setColorAt(pos, c)
            mp.fillRect(rect, g)
        mp.end()
        return mask


# ────────────────────────────────────────────────────────────────────────────
#  The floating orb
# ────────────────────────────────────────────────────────────────────────────
class FloatingOrb(QWidget):
    """The always-present face of the assistant.

    Left click toggles the hub, drag moves it, right click opens the menu.
    Hovering reveals three hit zones under the orb: HUB, MIC, STOP.
    """

    hub_toggled = pyqtSignal()
    wake_requested = pyqtSignal()
    mute_toggled = pyqtSignal()
    interrupt_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    position_changed = pyqtSignal(int, int)
    expanded_changed = pyqtSignal(bool)
    agent_run_requested = pyqtSignal(str)

    def __init__(self, assistant_name: str = "JARVIS", parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Keep the orb on screen when the app is not frontmost (see ScreenGlow).
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._name = assistant_name.upper()
        self.state = "SLEEPING"
        self.muted = False
        self.hub_open = False
        self.gesture_hint = ""          # transient gesture readout
        self._gesture_until = 0.0

        self.expanded = False
        self.agents_mode = False
        self._agents_w = 0.0            # animated 0 -> AGENTS_W
        self._agents: list = []         # [(key, name, description)]
        self._agent_zone = -1           # hovered RUN button, -1 = none
        self._agent_busy: set = set()   # keys currently working
        self._scale = 1.0               # live zoom, driven by the two-hand pinch
        self._panel_w = 0.0             # animated 0 -> PANEL_W
        self._apply_size()
        self._log: list[str] = []       # last few activity lines, newest last

        # Gesture readiness. Not where the hand is — that cannot be known
        # accurately — but whether the camera has one, and what it is holding.
        self.hands_seen = 0
        self.hand_pose = ""
        self._tint = 0.0                # eased 0..1, follows hands_seen
        self._pose_tint = 0.0           # eased 0..1, follows an active pose

        self._tick = 0
        self._spin = 0.0
        self._spin2 = 140.0
        self._aurora = 0.0
        self._flare = 0.0               # state-change burst, decays to 0
        self._hover = 0.0
        self._core = 1.0
        self._amp = 0.0
        self._amp_disp = 0.0
        self._pulses: list[float] = []
        self._sparks: list[list[float]] = []
        self._bars = [0.0] * 44

        self._drag_from = None
        self._moved = False
        self._zone = -1                 # hovered action zone, -1 = none

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    # -- API -----------------------------------------------------------------

    def set_state(self, state: str) -> None:
        if state != self.state:
            self._flare = 1.0
        self.state = state

    def set_audio_level(self, level: float) -> None:
        try:
            lv = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return
        if lv > self._amp:
            self._amp = lv

    def flash_gesture(self, text: str) -> None:
        self.gesture_hint = text.upper()
        self._gesture_until = time.time() + 2.0
        self._flare = 1.0

    def set_expanded(self, expanded: bool) -> None:
        """Grow the orb into a panel, or fold it back to just the orb."""
        expanded = bool(expanded)
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self._flare = 1.0
        self.expanded_changed.emit(expanded)

    def toggle_expanded(self) -> None:
        self.set_expanded(not self.expanded)

    def set_agents(self, agents: list, shown: bool = True) -> None:
        """Show the agent launcher: a card per agent, each with a RUN button.

        `agents` is a list of (key, name, description). The orb keeps no
        knowledge of what an agent is or how to start one -- it draws the
        cards, and emits the key of whichever RUN was pressed.
        """
        self._agents = list(agents or [])
        self.agents_mode = bool(shown and self._agents)
        if self.agents_mode:
            # The two panels occupy the same space, so opening the launcher
            # closes the status readout rather than fighting it for width.
            self.expanded = False
        self._agent_zone = -1
        self.update()

    def set_agent_busy(self, key: str, busy: bool = True) -> None:
        """Mark one agent as working, so its card can say so."""
        if busy:
            self._agent_busy.add(key)
        else:
            self._agent_busy.discard(key)
        self.update()

    def hide_agents(self) -> None:
        self.agents_mode = False
        self._agent_zone = -1

    def set_hidden(self, hidden: bool) -> None:
        """Take the orb off screen entirely, or bring it back.

        Hiding is cosmetic only — the assistant keeps listening and every tool
        keeps working; there is simply nothing to look at.
        """
        if hidden:
            self.hide()
        else:
            self.show()
            self.raise_()
            raise_above_menubar(self)

    def set_hand_state(self, hands: int, pose: str = "") -> None:
        """The camera can see this many hands, holding this pose (or none)."""
        if hands and not self.hands_seen:
            self._flare = 1.0           # a small greeting as the hand arrives
        self.hands_seen = int(hands)
        self.hand_pose = pose or ""

    def push_log(self, text: str) -> None:
        """Feed one activity line to the expanded panel."""
        line = " ".join(str(text).split())
        if not line:
            return
        self._log.append(line)
        del self._log[:-6]

    def showEvent(self, e) -> None:
        super().showEvent(e)
        # Re-assert the level every time: macOS resets it when a window is
        # re-shown, and a Qt::Tool panel would otherwise sink back under
        # full-screen apps.
        raise_above_menubar(self)

    def nudge(self, dx: int, dy: int) -> None:
        """Move by a pixel delta, clamped to the screen. Used by pinch-drag."""
        g = self.geometry()
        self._move_to(g.x() + int(dx), g.y() + int(dy))

    def _move_to(self, x: int, y: int) -> None:
        scr = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if scr is not None:
            a = scr.availableGeometry()
            x = max(a.left() - 12, min(x, a.right() - self.width() + 12))
            y = max(a.top() - 12, min(y, a.bottom() - self.height() + 12))
        self.move(x, y)
        self.position_changed.emit(x, y)

    # -- animation -----------------------------------------------------------

    @property
    def _listening(self) -> bool:
        return self.state in ("LISTENING", "THINKING") and not self.muted

    def _step(self) -> None:
        self._tick += 1
        self._amp *= 0.86
        self._amp_disp += (self._amp - self._amp_disp) * 0.4
        amp = self._amp_disp

        speaking = self.state == "SPEAKING"
        boost = 1.0 + amp * 1.8
        self._spin = (self._spin + (1.5 if speaking else 0.55) * boost) % 360
        self._spin2 = (self._spin2 - (1.1 if speaking else 0.38) * boost) % 360
        self._aurora = (self._aurora + (0.011 if self._listening else 0.004)) % 1.0
        self._flare *= 0.94

        tgt_hover = 1.0 if self.underMouse() else 0.0
        self._hover += (tgt_hover - self._hover) * 0.22

        # Ease in rather than snap: hand tracking drops a frame here and there,
        # and a light that blinks on every dropped frame is worse than no light.
        self._tint += ((1.0 if self.hands_seen else 0.0) - self._tint) * 0.10
        self._pose_tint += ((1.0 if self.hand_pose else 0.0) - self._pose_tint) * 0.16

        tgt_core = 1.0 + amp * (0.16 if speaking else 0.09)
        if self.muted:
            tgt_core = 0.92
        self._core += (tgt_core - self._core) * 0.24

        # Expanding sonar rings, quicker while there is voice on the wire.
        lim = ORB_SIZE * 0.5
        spd = 1.35 if (speaking or amp > 0.05) else 0.65
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if speaking else 0.02):
            self._pulses.append(ORB_SIZE * 0.16)

        # Orbiting sparks: [angle, radius, speed, life]
        if len(self._sparks) < (14 if speaking else 8) and random.random() < 0.25:
            self._sparks.append([
                random.uniform(0, 360),
                random.uniform(ORB_SIZE * 0.22, ORB_SIZE * 0.42),
                random.uniform(0.5, 2.2) * (1 if random.random() < 0.7 else -1),
                1.0,
            ])
        self._sparks = [
            [s[0] + s[2] * boost, s[1], s[2], s[3] - 0.008]
            for s in self._sparks if s[3] > 0
        ]

        # Waveform ring.
        for i in range(len(self._bars)):
            target = amp * random.uniform(0.35, 1.0) if not self.muted else 0.02
            self._bars[i] += (target - self._bars[i]) * 0.3

        if self.gesture_hint and time.time() > self._gesture_until:
            self.gesture_hint = ""

        # Ease the side panel open and closed by resizing the window itself —
        # the widget is transparent, so there is nothing to clip against.
        target_w = PANEL_W if self.expanded else 0.0
        if abs(self._panel_w - target_w) > 0.5:
            self._panel_w += (target_w - self._panel_w) * 0.28
            if abs(self._panel_w - target_w) <= 0.5:
                self._panel_w = target_w
            self._apply_size()

        target_a = AGENTS_W if self.agents_mode else 0.0
        if abs(self._agents_w - target_a) > 0.5:
            self._agents_w += (target_a - self._agents_w) * 0.28
            if abs(self._agents_w - target_a) <= 0.5:
                self._agents_w = target_a
            self._apply_size()

        self.update()

    # -- size ----------------------------------------------------------------

    def _apply_size(self) -> None:
        """Resize the window to the design size times the live zoom."""
        s = self._scale
        self.setFixedSize(
            max(1, int(round((ORB_SIZE + self._panel_w + self._agents_w) * s))),
            max(1, int(round(self._design_height() * s))),
        )

    def _design_height(self) -> float:
        """How tall the window is in design units.

        The orb alone needs ORB_SIZE + ORB_FOOT. The launcher usually needs
        more than that, so the window grows downward to fit the cards instead
        of squeezing them into the orb's height.
        """
        base = ORB_SIZE + ORB_FOOT
        if self._agents_w > 2 and self._agents:
            need = AGENT_PAD * 2 + 22 + len(self._agents) * AGENT_ROW + 16
            return max(base, need)
        return base

    def scale_by(self, ratio: float) -> float:
        """Multiply the orb's size by `ratio`. Returns the scale actually set.

        Multiplicative because the gesture reports a ratio per frame: the
        deltas compose, so letting go and grabbing again continues from the
        current size instead of snapping back to where a drag began.
        """
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            return self._scale
        if not (ratio > 0.0) or not math.isfinite(ratio):
            return self._scale
        return self.set_scale(self._scale * ratio)

    def set_scale(self, scale: float) -> float:
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            return self._scale
        if not math.isfinite(scale):
            return self._scale
        scale = max(ORB_SCALE_MIN, min(ORB_SCALE_MAX, scale))
        if abs(scale - self._scale) < 1e-3:
            return self._scale
        # Grow about the orb's centre rather than its top-left, so the orb
        # stays under the hands instead of sliding away from them.
        old_w, old_h = self.width(), self.height()
        self._scale = scale
        self._apply_size()
        dx = (self.width() - old_w) // 2
        dy = (self.height() - old_h) // 2
        if dx or dy:
            g = self.frameGeometry().topLeft()
            self._move_to(g.x() - dx, g.y() - dy)
        self.update()
        return self._scale

    def scale(self) -> float:
        return self._scale

    # -- geometry ------------------------------------------------------------

    def _orb_rect(self) -> QRectF:
        m = 16.0
        return QRectF(m, m, ORB_SIZE - 2 * m, ORB_SIZE - 2 * m)

    def _agent_zones(self) -> list[tuple[str, QRectF]]:
        """RUN button rects, in design coordinates, keyed by agent."""
        if self._agents_w <= 2 or not self._agents:
            return []
        x0 = ORB_SIZE - 6
        w = self._agents_w - 10
        y = AGENT_PAD + 24
        out = []
        for key, _name, _desc in self._agents:
            out.append((key, QRectF(x0 + w - 60, y + 14, 44, 22)))
            y += AGENT_ROW
        return out

    def _zones(self) -> list[tuple[str, QRectF]]:
        y = ORB_SIZE + 6
        w = (ORB_SIZE - 14) / 4
        names = ("HUB", "MIC", "STOP", "LESS" if self.expanded else "MORE")
        return [
            (n, QRectF(7 + i * w, y, w - 3, 20)) for i, n in enumerate(names)
        ]

    # -- paint ---------------------------------------------------------------

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Everything below is written in design coordinates; the zoom is
        # applied once, here. Painting rather than recomputing keeps the
        # layout, the hit zones and the glow geometry in one set of numbers.
        if self._scale != 1.0:
            p.scale(self._scale, self._scale)
        C = _C()

        r = self._orb_rect()
        cx, cy = r.center().x(), r.center().y()
        rad = r.width() / 2
        amp = self._amp_disp
        speaking = self.state == "SPEAKING"

        accent = _col(C.ACC if speaking else (C.MUTED_C if self.muted else C.PRI))

        # 1. Outer halo -- the soft presence that keeps the orb visible on any
        #    wallpaper without a window frame.
        # The hand tint rides on the halo: being seen makes the orb sit a
        # little brighter in its own light, before anything else changes.
        lift = 34 * self._tint + 26 * self._pose_tint
        halo = QRadialGradient(QPointF(cx, cy), rad * 2.0)
        halo.setColorAt(0.0, _col(accent.name(), int(70 + 90 * amp + lift)))
        halo.setColorAt(0.45, _col(accent.name(), int(26 + 40 * amp + lift * 0.6)))
        halo.setColorAt(1.0, _col(accent.name(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(cx, cy), rad * 2.0, rad * 2.0)

        # 2. Apple-Intelligence ring: a conical aurora stroked around the rim,
        #    bright while listening, a thin ember otherwise.
        self._paint_aurora_ring(p, cx, cy, rad, accent)

        # 2b. The readiness collar: a thin ring outside the rim that appears
        #     when the camera has a hand, and closes into a solid band while a
        #     pose is actually being held. This is the whole gesture feedback —
        #     "you are in position", not "your hand is at these coordinates".
        if self._tint > 0.01:
            self._paint_ready_collar(p, cx, cy, rad, accent)

        # 3. Glass body.
        body = QRadialGradient(QPointF(cx - rad * 0.3, cy - rad * 0.35), rad * 1.7)
        body.setColorAt(0.0, _col("#0b2733", 232))
        body.setColorAt(0.55, _col(C.PANEL, 226))
        body.setColorAt(1.0, _col("#00060a", 238))
        p.setBrush(body)
        p.setPen(QPen(_col(accent.name(), 150), 1.2))
        p.drawEllipse(QPointF(cx, cy), rad * 0.86, rad * 0.86)

        # 4. Instrument ticks -- the ring that spins faster under load.
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin)
        for i in range(72):
            major = i % 6 == 0
            a = int((150 if major else 60) * (0.5 + 0.5 * self._core))
            p.setPen(QPen(_col(accent.name(), a), 1.6 if major else 0.9))
            r0 = rad * (0.90 if major else 0.94)
            p.drawLine(QPointF(r0, 0), QPointF(rad * 1.0, 0))
            p.rotate(5)
        p.restore()

        # 5. Counter-rotating arc brackets.
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin2)
        p.setPen(QPen(_col(accent.name(), 190), 2.0))
        arc = QRectF(-rad * 0.78, -rad * 0.78, rad * 1.56, rad * 1.56)
        for start in (0, 120, 240):
            p.drawArc(arc, int((start + 8) * 16), int(58 * 16))
        p.restore()

        # 6. Sonar pulses.
        for pr in self._pulses:
            frac = 1.0 - (pr / (rad * 1.6))
            p.setPen(QPen(_col(accent.name(), int(90 * max(frac, 0.0))), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), pr, pr)

        # 7. Live waveform ring.
        n = len(self._bars)
        for i, v in enumerate(self._bars):
            ang = math.radians(i * 360.0 / n - 90 + self._spin * 0.2)
            r0 = rad * 0.64
            r1 = r0 + rad * (0.05 + v * 0.26)
            p.setPen(QPen(_col(accent.name(), int(70 + 175 * min(v * 2, 1.0))), 2.0))
            p.drawLine(
                QPointF(cx + math.cos(ang) * r0, cy + math.sin(ang) * r0),
                QPointF(cx + math.cos(ang) * r1, cy + math.sin(ang) * r1),
            )

        # 8. Core.
        core_r = rad * 0.26 * self._core
        core = QRadialGradient(QPointF(cx, cy), core_r * 1.9)
        core.setColorAt(0.0, _col("#ffffff", 240))
        core.setColorAt(0.30, _col(accent.name(), 210))
        core.setColorAt(0.65, _col(accent.name(), 70))
        core.setColorAt(1.0, _col(accent.name(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(core)
        p.drawEllipse(QPointF(cx, cy), core_r * 1.9, core_r * 1.9)

        # 9. Sparks.
        for ang, rr, _spd, life in self._sparks:
            a = math.radians(ang)
            p.setBrush(_col(accent.name(), int(190 * life)))
            p.drawEllipse(
                QPointF(cx + math.cos(a) * rr, cy + math.sin(a) * rr), 1.5, 1.5
            )

        # 10. Hub-open marker: a filled notch at the top of the rim.
        if self.hub_open:
            p.setBrush(_col(C.GREEN, 220))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy - rad * 1.02), 3.0, 3.0)

        # 11. Status line + action zones.
        self._paint_footer(p, C, accent)

        # 12. The expanded side panel.
        if self._panel_w > 2:
            self._paint_panel(p, C, accent)

        # 13. The agent launcher.
        if self._agents_w > 2:
            self._paint_agents(p, C, accent)

    def _paint_ready_collar(self, p: QPainter, cx, cy, rad, accent) -> None:
        """Dashed while a hand is merely seen, solid while a pose is held."""
        t, pt = self._tint, self._pose_tint
        r = rad * 1.14

        # Soft wash so the change is felt even out of the corner of the eye.
        wash = QRadialGradient(QPointF(cx, cy), r * 1.35)
        wash.setColorAt(0.0, _col(accent.name(), 0))
        wash.setColorAt(0.72, _col(accent.name(), int(18 * t + 22 * pt)))
        wash.setColorAt(1.0, _col(accent.name(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(wash)
        p.drawEllipse(QPointF(cx, cy), r * 1.35, r * 1.35)

        # The collar. Dashes close up as a pose engages, so "seen" and
        # "holding something" are distinguishable at a glance.
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(_col(accent.name(), int(58 * t + 112 * pt)), 1.5 + 0.9 * pt)
        if pt < 0.75:
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            gap = 3.5 - 3.0 * pt
            pen.setDashPattern([2.0 + 5.0 * pt, max(gap, 0.5)])
        p.setPen(pen)
        p.drawEllipse(QPointF(cx, cy), r, r)


    def _paint_agents(self, p: QPainter, C, accent) -> None:
        """One sub-panel per agent, built from the same parts as the main one.

        A row of text with a button beside it reads as a menu. Giving each
        agent its own bordered card, header rule and status line makes the
        launcher read as a rack of instruments instead -- which is what it is,
        and matches the panel next to it rather than competing with it.
        """
        w = self._agents_w
        x0 = ORB_SIZE - 6
        outer = QRectF(x0, 10, w - 10, self._design_height() - 20)
        if outer.width() < 12:
            return
        p.setOpacity(min(1.0, w / AGENTS_W))

        shell = QPainterPath()
        shell.addRoundedRect(outer, 8, 8)
        p.fillPath(shell, _col(C.PANEL, 238))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_col(accent.name(), 135), 1.0))
        p.drawPath(shell)

        tx = outer.x() + AGENT_PAD
        tw = outer.width() - AGENT_PAD * 2
        y = outer.y() + 9

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(_col(accent.name(), 242))
        p.drawText(QRectF(tx, y, tw, 14), Qt.AlignmentFlag.AlignLeft, "\u25c8 AGENTS")
        p.setFont(QFont("Courier New", 6))
        p.setPen(_col(C.TEXT_DIM, 190))
        p.drawText(QRectF(tx, y + 1, tw, 12), Qt.AlignmentFlag.AlignRight,
                   f"{len(self._agents)} READY")
        y += 16
        p.setPen(QPen(_col(C.BORDER, 210), 1.0))
        p.drawLine(QPointF(tx, y), QPointF(tx + tw, y))
        y += 8

        zones = dict(self._agent_zones())
        for i, (key, name, desc) in enumerate(self._agents):
            hot = (i == self._agent_zone)
            busy = key in self._agent_busy
            card = QRectF(tx - 3, y, tw + 6, AGENT_ROW - 8)

            sub = QPainterPath()
            sub.addRoundedRect(card, 6, 6)
            p.fillPath(sub, _col(accent.name(), 30 if hot else 14))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_col(accent.name(), 150 if hot else 70), 1.0))
            p.drawPath(sub)

            # A lit bar down the left edge: the cheapest way to make a card
            # look instrumented rather than boxed.
            p.fillRect(QRectF(card.x() + 1.5, card.y() + 6, 2.0, card.height() - 12),
                       _col(accent.name(), 235 if (hot or busy) else 120))

            ix = card.x() + 10
            iw = card.width() - 20

            p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            p.setPen(_col(C.TEXT, 245))
            p.drawText(QRectF(ix, card.y() + 5, iw - 54, 12),
                       Qt.AlignmentFlag.AlignLeft, name[:18])

            p.setPen(QPen(_col(C.BORDER, 120), 1.0))
            p.drawLine(QPointF(ix, card.y() + 19), QPointF(ix + iw - 54, card.y() + 19))

            p.setFont(QFont("Courier New", 6))
            p.setPen(_col(C.TEXT_DIM, 220))
            p.drawText(QRectF(ix, card.y() + 22, iw - 54, 11),
                       Qt.AlignmentFlag.AlignLeft, desc[:32])

            p.setPen(_col(accent.name() if busy else C.TEXT_DIM, 230 if busy else 150))
            p.drawText(QRectF(ix, card.y() + 33, iw - 54, 11),
                       Qt.AlignmentFlag.AlignLeft,
                       ("\u25cf WORKING" if busy else "\u25cb IDLE"))

            run = zones.get(key)
            if run is not None:
                rp = QPainterPath()
                rp.addRoundedRect(run, 4, 4)
                p.fillPath(rp, _col(accent.name(), 215 if hot else 110))
                p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
                p.setPen(_col("#05070c" if hot else C.TEXT, 250))
                p.drawText(run, Qt.AlignmentFlag.AlignCenter,
                           "\u2022\u2022\u2022" if busy else "RUN")
            y += AGENT_ROW
        p.setOpacity(1.0)

    def _paint_panel(self, p: QPainter, C, accent) -> None:
        """The expanded readout: state, gesture channel, recent activity.

        Everything here is a mirror of what the hub shows, so the panel is a
        glance rather than a second place to operate the assistant.
        """
        w = self._panel_w
        x0 = ORB_SIZE - 6
        rect = QRectF(x0, 16, w - 10, ORB_SIZE - 32)
        if rect.width() < 12:
            return
        p.setOpacity(min(1.0, w / PANEL_W))

        body = QPainterPath()
        body.addRoundedRect(rect, 7, 7)
        p.fillPath(body, _col(C.PANEL, 232))
        # drawPath strokes AND fills with the current brush, which is still the
        # sparks' accent colour at this point. Clear it or the outline floods
        # the panel.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_col(accent.name(), 120), 1.0))
        p.drawPath(body)

        pad = 11.0
        tx = rect.x() + pad
        tw = rect.width() - pad * 2
        y = rect.y() + pad

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(_col(accent.name(), 235))
        p.drawText(QRectF(tx, y, tw, 14), Qt.AlignmentFlag.AlignLeft, f"◈ {self._name}")
        y += 15
        p.setPen(QPen(_col(C.BORDER, 200), 1.0))
        p.drawLine(QPointF(tx, y), QPointF(tx + tw, y))
        y += 8

        p.setFont(QFont("Courier New", 7))
        rows = [
            ("STATE", "MUTED" if self.muted else self.state),
            ("HUB", "OPEN" if self.hub_open else "CLOSED"),
            ("GESTURE", self.gesture_hint or "—"),
            ("HAND", (self.hand_pose or "ready").upper()
                     if self.hands_seen else "not seen"),
        ]
        for key, val in rows:
            p.setPen(_col(C.TEXT_DIM, 220))
            p.drawText(QRectF(tx, y, 54, 12), Qt.AlignmentFlag.AlignLeft, key)
            p.setPen(_col(C.TEXT, 235))
            p.drawText(
                QRectF(tx + 56, y, tw - 56, 12),
                Qt.AlignmentFlag.AlignLeft,
                str(val)[:22],
            )
            y += 13

        y += 4
        p.setPen(_col(C.TEXT_DIM, 200))
        p.drawText(QRectF(tx, y, tw, 12), Qt.AlignmentFlag.AlignLeft, "ACTIVITY")
        y += 13
        p.setFont(QFont("Courier New", 6))
        avail = int((rect.bottom() - pad - y) // 11)
        for line in self._log[-max(avail, 0):]:
            p.setPen(_col(C.TEXT_MED, 215))
            p.drawText(
                QRectF(tx, y, tw, 11),
                Qt.AlignmentFlag.AlignLeft,
                line[:38],
            )
            y += 11
        if not self._log:
            p.setPen(_col(C.TEXT_DIM, 150))
            p.drawText(QRectF(tx, y, tw, 11), Qt.AlignmentFlag.AlignLeft, "no activity yet")
        p.setOpacity(1.0)

    def _paint_aurora_ring(self, p: QPainter, cx, cy, rad, accent) -> None:
        """The rim aurora. Colour travels around the ring; the band widens and
        brightens while the assistant is actually listening."""
        listening = self._listening
        power = (0.30 if listening else 0.09) + 0.55 * self._amp_disp + 0.35 * self._flare
        power = min(power, 1.0)

        # Rotated onto the live accent, so the rim, the hub frame and the
        # screen edge are all the same ribbon at the same angle.
        stops = aurora_ribbon(accent.name())
        grad = QConicalGradient(QPointF(cx, cy), -self._aurora * 360.0)
        for i, col in enumerate(stops + [stops[0]]):
            grad.setColorAt(min(i / len(stops), 1.0), col)

        passes = 5
        for i in range(passes):
            frac = i / (passes - 1)
            width = rad * (0.05 + frac * 0.30)
            alpha = power * (1.0 - frac) ** 1.6
            if alpha <= 0.01:
                continue
            p.setOpacity(alpha)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(grad, width))
            rr = rad * 0.93
            p.drawEllipse(QPointF(cx, cy), rr, rr)
        p.setOpacity(1.0)

    def _paint_footer(self, p: QPainter, C, accent) -> None:
        label = self.gesture_hint or (
            self.hand_pose.upper() if self.hand_pose else
            "HAND READY" if self.hands_seen else
            "MUTED" if self.muted else
            {"SPEAKING": "SPEAKING", "THINKING": "THINKING",
             "LISTENING": "LISTENING", "SLEEPING": "STANDBY"}.get(self.state, self.state)
        )
        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))

        if self._hover < 0.35:
            p.setPen(_col(accent.name(), int(200 * (1 - self._hover / 0.35))))
            p.drawText(
                QRectF(0, ORB_SIZE + 4, ORB_SIZE, 22),
                Qt.AlignmentFlag.AlignCenter,
                f"◈ {label}",
            )
            if self._hover < 0.05:
                return

        # Hover-revealed action strip.
        p.setOpacity(min(self._hover / 0.6, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)   # see _paint_panel
        for i, (name, rect) in enumerate(self._zones()):
            hot = i == self._zone
            path = QPainterPath()
            path.addRoundedRect(rect, 4, 4)
            p.fillPath(path, _col(C.PANEL2, 225))
            p.setPen(QPen(_col(accent.name() if hot else C.BORDER_B, 220), 1.0))
            p.drawPath(path)
            p.setPen(_col(accent.name() if hot else C.TEXT_MED, 235))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
        p.setOpacity(1.0)

    # -- interaction ---------------------------------------------------------

    def _zone_at(self, pos) -> int:
        if self._hover < 0.3:
            return -1
        # Mouse positions arrive in device pixels; the zones are design
        # coordinates. Undo the paint transform rather than scaling every
        # rect, so the two can never disagree.
        pt = QPointF(pos)
        if self._scale != 1.0:
            pt = QPointF(pt.x() / self._scale, pt.y() / self._scale)
        for i, (_n, rect) in enumerate(self._zones()):
            if rect.contains(pt):
                return i
        return -1

    def _agent_zone_at(self, pos) -> int:
        """Index of the RUN button under `pos`, or -1.

        Unlike the orb's own zones this ignores hover state: the launcher is
        already open, so its buttons must stay clickable even when the pointer
        arrives straight onto one without crossing the orb first.
        """
        if self._agents_w <= 2:
            return -1
        pt = QPointF(pos)
        if self._scale != 1.0:
            pt = QPointF(pt.x() / self._scale, pt.y() / self._scale)
        for i, (_k, rect) in enumerate(self._agent_zones()):
            if rect.adjusted(-6, -6, 6, 6).contains(pt):
                return i
        return -1

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_from = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        zone = self._zone_at(e.position())
        if zone != self._zone:
            self._zone = zone
            self.update()
        az = self._agent_zone_at(e.position())
        if az != self._agent_zone:
            self._agent_zone = az
            self.update()
        if self._drag_from is not None and e.buttons() & Qt.MouseButton.LeftButton:
            target = e.globalPosition().toPoint() - self._drag_from
            if not self._moved:
                start = self.frameGeometry().topLeft()
                if (target - start).manhattanLength() > 4:
                    self._moved = True
            if self._moved:
                self._move_to(target.x(), target.y())
            e.accept()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_from = None
        if self._moved:
            return

        # The launcher sits over the area a stray click would otherwise read
        # as "open the hub", so it is checked first.
        az = self._agent_zone_at(e.position())
        if az >= 0:
            zones = self._agent_zones()
            if az < len(zones):
                self.agent_run_requested.emit(zones[az][0])
            return

        zone = self._zone_at(e.position())
        if zone == 0:
            self.hub_toggled.emit()
        elif zone == 1:
            self.mute_toggled.emit()
        elif zone == 2:
            self.interrupt_requested.emit()
        elif zone == 3:
            self.toggle_expanded()
        else:
            self.hub_toggled.emit()

    def mouseDoubleClickEvent(self, e) -> None:
        self.wake_requested.emit()

    def leaveEvent(self, e) -> None:
        self._zone = -1

    def contextMenuEvent(self, e) -> None:
        C = _C()
        m = QMenu(self)
        m.setStyleSheet(f"""
            QMenu {{
                background: {C.PANEL}; color: {C.TEXT};
                border: 1px solid {C.BORDER_B};
                font-family: 'Courier New'; font-size: 11px; padding: 4px;
            }}
            QMenu::item {{ padding: 5px 22px 5px 14px; }}
            QMenu::item:selected {{ background: {C.PRI_GHO}; color: {C.PRI}; }}
            QMenu::separator {{ height: 1px; background: {C.BORDER}; margin: 4px 2px; }}
        """)
        m.addAction("Close hub" if self.hub_open else "Open hub",
                    self.hub_toggled.emit)
        m.addAction("Minimise panel" if self.expanded else "Expand panel",
                    self.toggle_expanded)
        m.addAction("Hide orb", lambda: self.set_hidden(True))
        m.addAction("Wake", self.wake_requested.emit)
        m.addAction("Unmute mic" if self.muted else "Mute mic",
                    self.mute_toggled.emit)
        m.addAction("Interrupt", self.interrupt_requested.emit)
        m.addSeparator()
        m.addAction(f"Quit {self._name}", self.quit_requested.emit)
        m.exec(e.globalPos())
