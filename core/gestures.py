"""Bare-hand gesture control for MARK LII.

A Python port of the hand-tracking engine from Jared Rhodes' `barehands`
(https://github.com/jaredrhod/barehands, stage.html). The pinch signature, the
clap gate and the release hysteresis below are transcribed from that project's
fitted thresholds -- the comments name the barehands revision each rule came
from so they stay traceable.

Barehands runs MediaPipe in the browser and drives glass cards. MARK LII runs
it headless on a worker thread and drives the assistant itself:

    CLAP        two open palms, fingers up, brought together  -> wake if asleep,
                                                                 else open the hub
    SWIPE       open palm thrown sideways across the frame    -> hide the hub
    TAP         a quick pinch on a hand that stays still      -> wake / enable
    THRUST      open palm pushed at the camera                -> halt / mute
    PINCH-DRAG  pinch and move                                -> drag the orb
    KNOB        index + middle out, THUMB OUT, hand rotated   -> volume
    SLIDER      index + middle out, THUMB TUCKED, up or down   -> brightness
    FIST        a closed fist, held                           -> mute the mic
    TWO-PINCH   pinch with both hands, move apart/together    -> resize the orb

The last two are *held* states rather than one-shot events: the mic stays
muted for exactly as long as a fist is visible, and the orb tracks the hands
continuously while both stay pinched. Neither goes through the fire cooldowns,
which exist to stop a single pose being counted twice.

The two dials are continuous rather than one-shot: they arm once the pose has
held for a few frames, then emit one step per unit of travel, so a slow
movement is a fine adjustment and a large one is coarse. They are the only
gestures exempt from the fire cooldowns, which exist to stop a single pose
being counted twice.

The thumb is what separates them, and that is deliberate. Both dials use the
same two fingers, so the hand only has to learn one shape: thumb out and you
are turning a knob, thumb tucked and you are sliding a slider. It also keeps
them clear of the pinch -- both require the thumb to be well away from the
index tip (DIAL_MIN_GAP), which is the one thing a pinch cannot be.

Two coordinate spaces are in play, and mixing them silently breaks the fitted
thresholds:

* Signature space -- span, pinch gap, arch ratios, palm aspect. Barehands
  fitted these against a 1920x1080 capture, where MediaPipe's per-axis
  normalisation stretches y by 16/9 relative to x. Every pose measurement below
  runs on landmarks pre-scaled into that same space, so the numbers transfer
  whatever resolution the camera actually hands us.
* Width units -- cursor position, hand speed, inter-hand distance for the clap.
  These are isotropic (y scaled by frame height/width). Barehands' pixel
  thresholds, tuned on a ~1440px window, are divided by 1440 to land here.
"""
from __future__ import annotations

import contextlib
import json
import math
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"
MODEL_PATH = CONFIG_DIR / "models" / "hand_landmarker.task"

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)

# Barehands was tuned against a ~1440px-wide window; its pixel/second and pixel
# thresholds are converted to width units by this divisor.
_BH_WIDTH = 1440.0

# ...and against a 1920x1080 capture, which is the aspect its pose signatures
# assume. CAPTURE_SIZE asks the camera for the same shape so the correction
# below is usually a no-op.
_BH_FRAME_ASPECT = 9.0 / 16.0
CAPTURE_SIZE = (1280, 720)

GHOST_BIRTH_SPEED = 900.0 / _BH_WIDTH   # v19.2 birth-speed gate
FAST_HAND_SPEED = 800.0 / _BH_WIDTH     # v3.9.8 speed-aware release
SELF_HEAL_SPEED = 500.0 / _BH_WIDTH     # v3.9.8 ghost self-heal
PROBATION_SPEED = 600.0 / _BH_WIDTH     # v3.9.15 probation skip-at-speed

# MARK LII gesture tuning (not from barehands -- these map poses to actions).
TAP_MAX_MS = 400.0
TAP_MAX_TRAVEL = 0.10
SWIPE_WINDOW_MS = 600.0
SWIPE_MIN_TRAVEL = 0.45
THRUST_WINDOW_MS = 450.0
THRUST_GROWTH = 1.55

# A pinch that outlives the tap window and travels becomes a drag.
DRAG_MIN_TRAVEL = 0.035

# The finger dials. A pose must survive DIAL_ARM_FRAMES before it counts, which
# keeps a hand passing through "one finger up" on its way somewhere else from
# grabbing the volume. DIAL_STEP is the travel, in width units, that one step
# costs: at 0.035 a comfortable vertical sweep of the hand is worth about nine
# steps, which is a usable range without being twitchy.
DIAL_ARM_FRAMES = 4
DIAL_STEP = 0.035

# Both dials are held on index + middle; the thumb picks which one.
DIAL_FINGERS = (True, True, False, False)

# The thumb must be at least this far from the index tip, relative to hand
# span, before either dial arms. A pinch lives below 0.38, so nothing that
# qualifies here can also be read as a pinch.
DIAL_MIN_GAP = 0.55

# One volume step per this much rotation. At 22 degrees a quarter turn of the
# wrist is about four steps, which is roughly how far a physical volume knob
# travels for the same change.
DIAL_ANGLE_STEP = math.radians(22.0)

# Below this, the thumb and fingertips are too close together for the angle
# between them to be stable enough to measure.
DIAL_MIN_ARM = 0.35

COOLDOWNS = {"summon": 2.5, "dismiss": 1.2, "wake": 0.8, "halt": 2.0}

# ── held mute -------------------------------------------------------------
# A closed fist mutes the microphone for as long as it is held, and opening
# the hand releases it. Unlike every other gesture here this is a *state*, not
# an event, so it does not go through _fire() and has no cooldown.
#
# The two frame counts are deliberately different. Engaging takes a few frames
# so that a hand passing through a fist shape on its way somewhere else -- the
# midpoint of a clap, a hand entering frame -- does not cut the mic. Releasing
# takes longer, because a dropped frame from the tracker reads exactly like an
# opened hand, and a mic that flickers back on mid-sentence is worse than one
# that stays off a beat too long.
FIST_ON_FRAMES = 4
FIST_OFF_FRAMES = 8

# ── two-handed resize ------------------------------------------------------
# Pinch with both hands and move them apart to grow the orb, together to
# shrink it. The pinch is what separates this from the clap: the clap law
# disqualifies any hand that pinched recently, so the two cannot collide.
RESIZE_ARM_FRAMES = 3        # both hands pinched this long before it engages
RESIZE_MIN_SPAN = 0.06       # ignore hands practically on top of each other
RESIZE_DEADZONE = 0.012      # ignore ratio noise smaller than this
GLOBAL_LOCKOUT = 0.4


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


@dataclass
class _Hand:
    """Per-hand tracking state. Mirrors a barehands `cursor`."""

    x: float = 0.0
    y: float = 0.0
    seeded: bool = False
    history: list[tuple[float, float, float]] = field(default_factory=list)

    pinched: bool = False
    pinch_t: float = 0.0
    pinch_origin: tuple[float, float] = (0.0, 0.0)
    # "never pinched", as a time far enough in the past that every recency
    # window fails closed. Zero would only work by accident: it reads as
    # "pinched at t=0", which is harmless against a monotonic clock in the
    # billions but wrong against any timeline that starts near zero.
    last_pinch_t: float = -1e9
    ok_ema: float = 0.0
    ok_prev: bool = False
    open_prev: bool = False
    bad_run: int = 0
    prob_kill: bool = False
    ghost: bool = False

    dragging: bool = False
    drag_last: tuple[float, float] = (0.0, 0.0)

    ext: tuple[bool, ...] = (False, False, False, False)
    thumb_out: bool = False         # abducted, not folded across the palm
    pinch_ratio: float = 1.0        # thumb-to-index gap over hand span
    knob_angle: float = 0.0         # thumb -> fingertip angle, radians
    knob_arm: float = 0.0           # length of that vector, in span units
    dial_pose: str | None = None    # the pose currently being held
    dial_frames: int = 0            # how long it has been held
    dial_armed: str | None = None   # the dial actually driving something
    dial_anchor: float = 0.0        # y the next brightness step is measured from
    knob_prev: float = 0.0          # last angle seen, for unwrapping
    knob_accum: float = 0.0         # rotation banked but not yet a step

    fist: bool = False              # every finger folded, thumb not abducted

    palm_open: bool = False
    soft_open: bool = False
    last_soft_t: float = 0.0
    hand_up: float = 0.0
    wrist: tuple[float, float] = (0.0, 0.0)
    mcp: tuple[float, float] = (0.0, 0.0)

    span_hist: list[tuple[float, float]] = field(default_factory=list)
    open_hist: list[tuple[float, float]] = field(default_factory=list)


class _P:
    """A landmark rescaled into barehands' 1920x1080 signature space."""

    __slots__ = ("x", "y", "z")

    def __init__(self, lm, y_scale: float):
        self.x = lm.x
        self.y = lm.y * y_scale
        self.z = getattr(lm, "z", 0.0) or 0.0


def _dist(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def ensure_model(path: Path | None = None) -> Path:
    """Return the hand_landmarker model path, downloading it once if needed."""
    path = path or MODEL_PATH
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[Gestures] Downloading hand landmark model (~7MB) to {path} ...")
    tmp = path.with_suffix(".partial")
    urllib.request.urlretrieve(MODEL_URL, tmp)  # noqa: S310 - fixed https URL
    tmp.replace(path)
    print("[Gestures] Hand landmark model ready.")
    return path


class GestureEngine:
    """Watches the webcam on a worker thread and emits named gestures.

    `on_gesture(name, meta)` and `on_drag(dx, dy)` are called from the worker
    thread; Qt callers must hop them onto the GUI thread with a signal.
    """

    def __init__(
        self,
        on_gesture: Callable[[str, dict], None],
        on_drag: Callable[[float, float], None] | None = None,
        on_dial: Callable[[str, int], None] | None = None,
        on_presence: Callable[[int, str], None] | None = None,
        on_hold: Callable[[str, bool], None] | None = None,
        on_resize: Callable[[float], None] | None = None,
        camera_index: int | None = None,
        target_fps: int | None = None,
    ):
        cfg = _cfg()
        self._on_gesture = on_gesture
        self._on_drag = on_drag
        self._on_dial = on_dial
        self._on_presence = on_presence
        # Held states (currently just the fist-mute): (name, active). Called
        # only on a change, so a fist held for a minute costs two calls.
        self._on_hold = on_hold
        # Two-handed resize: the multiplicative change since the last call.
        # Incremental rather than absolute so the engine never has to know
        # what size the thing being resized currently is.
        self._on_resize = on_resize
        self._presence = (0, "")   # last (hand count, pose) actually reported

        self._fist_on = 0          # consecutive frames with a fist visible
        self._fist_off = 0         # consecutive frames without one
        self._fist_held = False    # what we last told the caller

        self._resize_frames = 0    # frames both hands have been pinched
        self._resize_span = 0.0    # last inter-hand distance emitted from
        self._camera_index = (
            camera_index
            if camera_index is not None
            else int(cfg.get("camera_index", 0))
        )
        self._target_fps = target_fps or int(cfg.get("gesture_fps", 20))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._released = threading.Event()   # set while the camera is handed over
        self._hands: dict[int, _Hand] = {}
        self._clap_hist: list[tuple[float, float, bool]] = []
        self._last_fire: dict[str, float] = {}
        self._last_any_fire = 0.0
        self.running = False
        self.enabled = bool(cfg.get("gestures_enabled", True))
        self.last_error: str | None = None
        self.hands_seen = 0          # cosmetic: what the overlay reads out

    # ------------------------------------------------------------------ API

    def start(self) -> bool:
        """Start the tracking thread. Returns False if unavailable."""
        if not self.enabled:
            print("[Gestures] Disabled (gestures_enabled=false in config).")
            return False
        if self._thread is not None:
            return self.running
        try:
            import cv2  # noqa: F401
            import mediapipe  # noqa: F401
        except ImportError as exc:
            self.last_error = f"missing dependency: {exc}"
            print(
                f"[Gestures] Unavailable ({exc}). "
                "Install with: pip install 'mediapipe<1.0' opencv-python"
            )
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="markii-gestures", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self.running = False

    def pause(self, timeout: float = 2.0) -> None:
        """Release the camera so another feature can open it.

        Blocks until the worker has actually let go, so the caller does not
        race the capture handle.
        """
        if not self.running:
            return
        self._released.clear()
        self._paused.set()
        self._released.wait(timeout)

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # --------------------------------------------------------------- worker

    def _run(self) -> None:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        try:
            model_path = ensure_model()
        except Exception as exc:
            self.last_error = f"model download failed: {exc}"
            print(f"[Gestures] {self.last_error}")
            return

        options = vision.HandLandmarkerOptions(
            # CPU delegate: the Metal path in mediapipe aborts the whole
            # process on macOS ("graph_service.h: Service is unavailable"),
            # and hand tracking at 20fps is cheap enough on CPU.
            base_options=mp_python.BaseOptions(
                model_asset_path=str(model_path),
                delegate=mp_python.BaseOptions.Delegate.CPU,
            ),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.6,
            min_tracking_confidence=0.6,
        )

        frame_budget = 1.0 / max(self._target_fps, 1)
        cap = None
        print(
            f"[Gestures] Armed (camera {self._camera_index}, {self._target_fps} fps). "
            "Clap = wake/hub, palm swipe right = open panel then agents, "
            "palm swipe left = fold back then hide hub, pinch tap = wake, "
            "palm thrust = halt, pinch-drag = move the orb, "
            "two fingers + thumb out turned like a knob = volume, "
            "two fingers + thumb tucked slid up/down = brightness."
        )
        self.running = True

        try:
            with vision.HandLandmarker.create_from_options(options) as landmarker:
                while not self._stop.is_set():
                    started = time.monotonic()

                    if self._paused.is_set():
                        if cap is not None:
                            cap.release()
                            cap = None
                            self._hands.clear()
                            self.hands_seen = 0
                        self._released.set()
                        time.sleep(0.2)
                        continue

                    if cap is None:
                        cap = self._open_camera(cv2)
                        if cap is None:
                            time.sleep(2.0)
                            continue

                    ok, frame = cap.read()
                    if not ok:
                        time.sleep(0.1)
                        continue

                    frame = cv2.flip(frame, 1)  # mirror: hands move as you see them
                    h, w = frame.shape[:2]
                    aspect = h / w if w else 1.0
                    # Rescale y into barehands' 16:9 signature space. Exactly
                    # 1.0 when the camera honoured CAPTURE_SIZE.
                    y_scale = aspect / _BH_FRAME_ASPECT
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

                    try:
                        result = landmarker.detect_for_video(
                            image, int(started * 1000)
                        )
                    except Exception as exc:  # a bad frame must not kill the loop
                        print(f"[Gestures] detect failed: {exc}")
                        result = None

                    now = time.monotonic() * 1000.0
                    landmarks = list(getattr(result, "hand_landmarks", None) or [])
                    self.hands_seen = len(landmarks)
                    self._process(now, landmarks, aspect, y_scale)

                    elapsed = time.monotonic() - started
                    if elapsed < frame_budget:
                        time.sleep(frame_budget - elapsed)
        except Exception as exc:
            self.last_error = str(exc)
            print(f"[Gestures] Loop stopped: {exc}")
        finally:
            if cap is not None:
                cap.release()
            self._released.set()
            self.running = False

    def _open_camera(self, cv2):
        try:
            backend = cv2.CAP_AVFOUNDATION
        except AttributeError:
            backend = 0
        cap = cv2.VideoCapture(self._camera_index, backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            self.last_error = f"camera {self._camera_index} unavailable"
            print(
                f"[Gestures] Camera {self._camera_index} could not be opened. "
                "Grant Camera permission in System Settings > Privacy & Security."
            )
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_SIZE[1])
        self.last_error = None
        return cap

    # ------------------------------------------------------------ detection

    def _process(
        self, now: float, landmarks: list, aspect: float, y_scale: float
    ) -> None:
        seen: set[int] = set()

        for idx, lms in enumerate(landmarks[:2]):
            seen.add(idx)
            hand = self._hands.setdefault(idx, _Hand())
            self._read_hand(hand, [_P(lm, y_scale) for lm in lms], now, aspect, y_scale)
            # The dial runs first: an armed dial suppresses the drag and the
            # tap for that hand, so turning the volume can never also move the
            # orb or wake the assistant.
            self._detect_dial(hand, now)
            self._detect_drag(hand, now)
            self._detect_tap(hand, now)
            self._detect_swipe(hand, now)
            self._detect_thrust(hand, now)

        for idx in list(self._hands):
            if idx not in seen:
                del self._hands[idx]

        self._detect_clap(now)
        self._detect_fist()
        self._detect_resize()
        self._emit_presence()

    def _emit_presence(self) -> None:
        """Report *whether* a hand is being tracked, and what it is doing.

        Deliberately not *where*. Nothing tells us where the camera sits
        relative to the screen, how wide its lens is, or how far away the hand
        is, so any frame-to-screen mapping is a guess that will be wrong by
        some centimetres -- and every gesture here is relative anyway, so a
        position would be decoration, not information. What is worth showing
        is that the hand is seen and which pose it is holding.

        Emitted only when the state changes, so a still hand costs nothing.
        """
        if self._on_presence is None:
            return

        pose = ""
        for hand in self._hands.values():
            # Most specific pose wins: a pose that is actively driving
            # something outranks one that is merely recognisable.
            if hand.pinched and not hand.ghost:
                pose = "pinch"
                break
            if hand.fist and not hand.ghost:
                pose = "fist"
            if hand.dial_armed:
                pose = hand.dial_armed
                break
            if hand.palm_open:
                pose = "palm"

        state = (len(self._hands), pose)
        if state == self._presence:
            return
        self._presence = state
        try:
            self._on_presence(state[0], state[1])
        except Exception as exc:
            print(f"[Gestures] presence handler failed: {exc}")

    def _read_hand(
        self, cur: _Hand, lms, now: float, aspect: float, y_scale: float
    ) -> None:
        """Transcription of the barehands per-frame hand read.

        `lms` arrives already in signature space; positions that feed speed
        and clap distances are converted back to isotropic width units.
        """
        # y is currently stretched by y_scale; undo it, then apply the frame
        # aspect to get isotropic width units.
        to_wu = aspect / y_scale if y_scale else aspect
        wrist, mcp = lms[0], lms[9]
        span = _dist(wrist, mcp)
        cur.hand_up = (wrist.y - mcp.y) / span if span > 0 else 0.0
        cur.wrist = (wrist.x, wrist.y * to_wu)
        cur.mcp = (mcp.x, mcp.y * to_wu)

        gap = _dist(lms[4], lms[8])
        ratio = gap / span if span > 0 else 1.0

        # Cursor position: midpoint of thumb tip and index tip, smoothed 0.45.
        px = (lms[8].x + lms[4].x) / 2
        py = ((lms[8].y + lms[4].y) / 2) * to_wu
        if not cur.seeded:
            cur.x, cur.y, cur.seeded = px, py, True
        else:
            cur.x += (px - cur.x) * 0.45
            cur.y += (py - cur.y) * 0.45
        cur.history.append((cur.x, cur.y, now))
        if len(cur.history) > 10:
            cur.history.pop(0)

        was_pinched = cur.pinched

        # v3.6.1 -- a fist folds every finger; a real pinch leaves three out.
        ext = [
            _dist(lms[t], wrist) > 1.45 * _dist(lms[m], wrist)
            for t, m in ((8, 5), (12, 9), (16, 13), (20, 17))
        ]
        ext_fingers = sum(ext)
        cur.ext = tuple(ext)
        cur.pinch_ratio = ratio

        # Thumb abducted or folded? The tip of a thumb held out to the side is
        # further from the pinky knuckle than the joint below it; a thumb
        # folded across the palm travels the other way and ends up closer.
        cur.thumb_out = _dist(lms[4], lms[17]) > 1.10 * _dist(lms[3], lms[17])

        # A closed fist: nothing extended and the thumb wrapped in rather than
        # held out. The thumb test is what keeps a "thumbs up" -- which also
        # folds all four fingers -- from muting the microphone.
        cur.fist = ext_fingers == 0 and not cur.thumb_out

        # The knob vector: thumb tip to the midpoint of the index and middle
        # tips — the line between the two halves of a pinched grip. Turning the
        # hand as if on a dial rotates it. Measured in isotropic width units,
        # because an angle taken in signature space would be sheared by the
        # per-axis normalisation and would read a pure rotation as uneven.
        tip_mx = (lms[8].x + lms[12].x) / 2
        tip_my = (lms[8].y + lms[12].y) / 2
        vx = tip_mx - lms[4].x
        vy = (tip_my - lms[4].y) * to_wu
        # Image y grows downward, so a rising atan2 is clockwise on screen.
        cur.knob_angle = math.atan2(vy, vx)
        cur.knob_arm = math.hypot(vx, vy) / span if span > 0 else 0.0

        def arch(tip: int, knuckle: int) -> float:
            td = _dist(lms[tip], wrist)
            md = _dist(lms[knuckle], wrist)
            return td / md if md > 0 else 9.0

        # v3.8.2 THE CONTRAST LAW -- the index curls in to the thumb while
        # middle/ring/pinky stay arched out past the knuckle circle.
        f8 = arch(8, 5)
        back_mean = (arch(12, 9) + arch(16, 13) + arch(20, 17)) / 3

        # v3.8.4 -- palm aspect picks the orientation regime; in profile the
        # thumb is the judge instead of the arch contrast.
        palm_w = _dist(lms[5], lms[17])
        palm_aspect = span / palm_w if palm_w > 0 else 9.0
        t_rel = _dist(lms[4], lms[13]) / span if span > 0 else 0.0

        # v3.8.5 -- either signature admits: contrast (frontal) OR far thumb.
        ok_back = (back_mean - f8 > 0.18 and back_mean > 1.30) or (
            palm_aspect < 2.0 and t_rel > 0.95
        )
        cur.ok_ema = 0.70 * cur.ok_ema + 0.30 * (1.0 if ok_back else 0.0)
        ok_now = ok_back and cur.ok_prev  # v3.8.6 two clean frames enter at once
        cur.ok_prev = ok_back

        # v3.9.8 / v3.9.25 -- speed-aware release; a fast hand must open wide.
        speed = self._speed(cur.history)
        rel_bar = 0.70 if speed > FAST_HAND_SPEED else 0.55
        open_read = ratio >= rel_bar
        rel_ok = (open_read and cur.open_prev) if speed > FAST_HAND_SPEED else open_read
        cur.open_prev = open_read

        # v3.9.30 THE SANITY BOUND -- no real hand exceeds palm aspect 5.5.
        hand_garbage = palm_aspect > 6
        if hand_garbage:
            cur.prob_kill = True

        if hand_garbage:
            cur.pinched = False
        elif was_pinched:
            cur.pinched = not rel_ok
        else:
            # v3.9.32 -- frontal gap ceiling 0.32, rotated palm 0.38.
            ceiling = 0.38 if palm_aspect < 2.0 else 0.32
            cur.pinched = ratio < ceiling and (ok_now or cur.ok_ema > 0.55)

        # v3.9.15 PINCH PROBATION -- a fresh pinch must keep its signature
        # through its first 400ms or it is silently dropped.
        if cur.pinched and not was_pinched:
            cur.bad_run = 0
            cur.prob_kill = False
        elif (
            cur.pinched
            and was_pinched
            and now - cur.pinch_t < 400
            and speed < PROBATION_SPEED
        ):
            cur.bad_run = 0 if ok_back else cur.bad_run + 1
            if cur.bad_run >= 4:
                cur.pinched = False
                cur.prob_kill = True
        else:
            cur.bad_run = 0

        if cur.pinched:
            cur.last_pinch_t = now

        # THE PALM ENGINE -- extension and gross travel, never stillness.
        cur.palm_open = ext_fingers >= 4 and ratio > 0.8
        cur.soft_open = ext_fingers >= 3 and ratio > 0.7
        if cur.soft_open:
            cur.last_soft_t = now

        if cur.pinched and not was_pinched:
            cur.pinch_t = now
            cur.pinch_origin = (cur.x, cur.y)
            cur.drag_last = (cur.x, cur.y)
            # v19.2 THE BIRTH-SPEED GATE -- a pinch born mid-sweep is blur.
            cur.ghost = speed > GHOST_BIRTH_SPEED
        elif cur.pinched and cur.ghost and speed < SELF_HEAL_SPEED:
            # v3.9.8 -- ghosts self-heal once the hand decelerates.
            cur.ghost = False
            cur.pinch_t = now
            cur.pinch_origin = (cur.x, cur.y)
            cur.drag_last = (cur.x, cur.y)

        cur.span_hist.append((now, span))
        while cur.span_hist and now - cur.span_hist[0][0] > THRUST_WINDOW_MS:
            cur.span_hist.pop(0)
        cur.open_hist.append((now, cur.x if cur.palm_open else math.nan))
        while cur.open_hist and now - cur.open_hist[0][0] > SWIPE_WINDOW_MS:
            cur.open_hist.pop(0)

    @staticmethod
    def _speed(history: list[tuple[float, float, float]]) -> float:
        if len(history) < 2:
            return 0.0
        x0, y0, t0 = history[0]
        x1, y1, t1 = history[-1]
        dt = (t1 - t0) / 1000.0
        if dt <= 0:
            return 0.0
        return math.hypot(x1 - x0, y1 - y0) / dt

    # ------------------------------------------------------------- gestures

    def _detect_drag(self, cur: _Hand, now: float) -> None:
        """A held pinch that travels moves the orb, in width units per frame.

        The drag only arms once the pinch has outlived the tap window, so a
        tap and a drag never fire from the same pinch.
        """
        if not cur.pinched or cur.ghost or cur.prob_kill or cur.dial_armed:
            cur.dragging = False
            return
        held = now - cur.pinch_t
        travel = math.hypot(cur.x - cur.pinch_origin[0], cur.y - cur.pinch_origin[1])
        if not cur.dragging:
            if held <= TAP_MAX_MS and travel < DRAG_MIN_TRAVEL:
                return
            cur.dragging = True
            cur.drag_last = (cur.x, cur.y)
            return
        dx, dy = cur.x - cur.drag_last[0], cur.y - cur.drag_last[1]
        cur.drag_last = (cur.x, cur.y)
        if self._on_drag is not None and (dx or dy):
            try:
                self._on_drag(dx, dy)
            except Exception as exc:
                print(f"[Gestures] drag handler failed: {exc}")

    def _detect_dial(self, cur: _Hand, now: float) -> None:
        """Index and middle out; the thumb says which dial and how it moves.

        Thumb out  -> volume, turned like a knob (clockwise raises it).
        Thumb in   -> brightness, slid up and down.

        Both demand the thumb be well clear of the index tip, which is exactly
        what a pinch is not, so the two can never be confused for each other.
        """
        pose = None
        if (
            not cur.pinched
            and cur.ext == DIAL_FINGERS
            and cur.pinch_ratio >= DIAL_MIN_GAP
        ):
            pose = "volume" if cur.thumb_out else "brightness"
            if pose == "volume" and cur.knob_arm < DIAL_MIN_ARM:
                pose = None      # too small a vector to read an angle from

        if pose is None:
            cur.dial_pose = None
            cur.dial_frames = 0
            cur.dial_armed = None
            cur.knob_accum = 0.0
            return

        if pose != cur.dial_pose:
            cur.dial_pose = pose
            cur.dial_frames = 1
            cur.dial_armed = None
            cur.knob_accum = 0.0
            return

        cur.dial_frames += 1
        if cur.dial_armed is None:
            if cur.dial_frames < DIAL_ARM_FRAMES:
                return
            # Arm where the hand is now, so holding the pose costs nothing
            # until it actually moves.
            cur.dial_armed = pose
            cur.dial_anchor = cur.y
            cur.knob_prev = cur.knob_angle
            cur.knob_accum = 0.0
            return

        if pose == "volume":
            self._turn_knob(cur)
        else:
            # y grows downward, so travel upward is a positive step.
            while cur.dial_anchor - cur.y >= DIAL_STEP:
                cur.dial_anchor -= DIAL_STEP
                self._emit_dial(pose, +1)
            while cur.y - cur.dial_anchor >= DIAL_STEP:
                cur.dial_anchor += DIAL_STEP
                self._emit_dial(pose, -1)

    def _turn_knob(self, cur: _Hand) -> None:
        """Accumulate rotation and pay it out one step at a time.

        The angle is unwrapped across the +/-pi seam: without that, a hand
        crossing straight up would register half a turn in one frame and jump
        the volume across its whole range.
        """
        delta = cur.knob_angle - cur.knob_prev
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        cur.knob_prev = cur.knob_angle

        # A jump this large in one frame is a tracking glitch, not a wrist.
        if abs(delta) > math.pi / 2:
            return

        cur.knob_accum += delta
        while cur.knob_accum >= DIAL_ANGLE_STEP:
            cur.knob_accum -= DIAL_ANGLE_STEP
            self._emit_dial("volume", +1)      # clockwise
        while cur.knob_accum <= -DIAL_ANGLE_STEP:
            cur.knob_accum += DIAL_ANGLE_STEP
            self._emit_dial("volume", -1)      # counter-clockwise

    def _emit_dial(self, kind: str, direction: int) -> None:
        """Dials bypass _fire: they are continuous, and the cooldowns there
        exist to stop one-shot poses being counted twice."""
        if self._on_dial is None:
            return
        try:
            self._on_dial(kind, direction)
        except Exception as exc:
            print(f"[Gestures] dial handler for {kind} failed: {exc}")

    def _detect_tap(self, cur: _Hand, now: float) -> None:
        """A quick pinch released without travel = wake voice input."""
        if cur.pinched or cur.pinch_t == 0 or cur.prob_kill or cur.ghost:
            return
        if cur.dial_armed:
            cur.pinch_t = 0          # a dial is running; this is not a tap
            return
        held = now - cur.pinch_t
        was_drag = cur.dragging
        cur.pinch_t = 0  # consume the release either way
        cur.dragging = False
        if was_drag or held > TAP_MAX_MS:
            return
        travel = math.hypot(cur.x - cur.pinch_origin[0], cur.y - cur.pinch_origin[1])
        if travel < TAP_MAX_TRAVEL:
            self._fire("wake", {"held_ms": round(held)})

    def _detect_swipe(self, cur: _Hand, now: float) -> None:
        """An open palm thrown sideways = hide the hub."""
        samples = [(t, x) for t, x in cur.open_hist if not math.isnan(x)]
        if len(samples) < 4 or len(samples) != len(cur.open_hist):
            return  # the palm must stay open for the whole window
        travel = samples[-1][1] - samples[0][1]
        if abs(travel) >= SWIPE_MIN_TRAVEL:
            cur.open_hist.clear()
            self._fire("dismiss", {"direction": "right" if travel > 0 else "left"})

    def _detect_thrust(self, cur: _Hand, now: float) -> None:
        """An open palm pushed at the camera = halt."""
        if not cur.palm_open or len(cur.span_hist) < 5:
            return
        spans = [s for _, s in cur.span_hist]
        first, last = spans[0], spans[-1]
        if first > 0 and last / first >= THRUST_GROWTH:
            cur.span_hist.clear()
            self._fire("halt", {"growth": round(last / first, 2)})

    def _detect_clap(self, now: float) -> None:
        """v3.9.7 THE PRAYER LAW -- two open, vertical palms brought together."""
        ids = list(self._hands)
        if len(ids) == 2:
            a, b = self._hands[ids[0]], self._hands[ids[1]]
            # Any hand that pinched in the last 800ms is disqualified.
            recent_pinch = (
                a.pinched
                or b.pinched
                or now - max(a.last_pinch_t, b.last_pinch_t) < 800
            )
            if recent_pinch:
                self._clap_hist.clear()
                return

            wrist_d = math.hypot(a.wrist[0] - b.wrist[0], a.wrist[1] - b.wrist[1])
            mcp_d = math.hypot(a.mcp[0] - b.mcp[0], a.mcp[1] - b.mcp[1])
            both_open = (a.soft_open or now - a.last_soft_t < 250) and (
                b.soft_open or now - b.last_soft_t < 250
            )
            both_up = a.hand_up > 0.85 and b.hand_up > 0.85

            self._clap_hist.append((now, wrist_d, both_open and both_up))
            while self._clap_hist and now - self._clap_hist[0][0] > 900:
                self._clap_hist.pop(0)

            was_apart = any(now - t <= 800 and d > 0.18 for t, d, _ in self._clap_hist)
            if wrist_d < 0.11 and mcp_d < 0.09 and both_up and both_open and was_apart:
                self._clap_hist.clear()
                self._fire("summon", {})
        elif self._clap_hist:
            # Palms merged into one detection at contact: the vanish read,
            # honoured only if the last qualified pose was already closing.
            t_last, d_last, q_last = self._clap_hist[-1]
            was_apart = any(
                t_last - t <= 800 and d > 0.18 for t, d, _ in self._clap_hist
            )
            if now - t_last < 200 and q_last and d_last < 0.16 and was_apart:
                self._clap_hist.clear()
                self._fire("summon", {"via": "vanish"})
            else:
                self._clap_hist.clear()

    def _detect_fist(self) -> None:
        """A closed fist holds the microphone muted; opening it releases.

        A held state rather than a toggle, so there is no way to end up out of
        sync with it: what the camera sees is what the mic is doing. Losing
        the hand entirely counts as no fist, which is the safe direction --
        the mic comes back rather than staying dead once you walk away.
        """
        if self._on_hold is None:
            return

        showing = any(h.fist and not h.ghost for h in self._hands.values())
        if showing:
            self._fist_on += 1
            self._fist_off = 0
        else:
            self._fist_off += 1
            self._fist_on = 0

        if not self._fist_held and self._fist_on >= FIST_ON_FRAMES:
            self._fist_held = True
        elif self._fist_held and self._fist_off >= FIST_OFF_FRAMES:
            self._fist_held = False
        else:
            return

        print(f"[Gestures] fist-mute {'on' if self._fist_held else 'off'}")
        try:
            self._on_hold("mute", self._fist_held)
        except Exception as exc:
            print(f"[Gestures] hold handler failed: {exc}")

    def _detect_resize(self) -> None:
        """Both hands pinched, moving apart or together = resize the orb.

        Reported as the ratio of this frame's hand separation to the last
        one's. A ratio composes: the caller multiplies whatever size it
        currently has by it and needs no notion of where the gesture started,
        which also means letting go and grabbing again simply continues from
        the current size instead of snapping.
        """
        if self._on_resize is None:
            return

        ids = list(self._hands)
        both = [self._hands[i] for i in ids] if len(ids) == 2 else []
        pinching = len(both) == 2 and all(h.pinched and not h.ghost for h in both)
        if not pinching:
            self._resize_frames = 0
            self._resize_span = 0.0
            return

        a, b = both
        span = math.hypot(a.x - b.x, a.y - b.y)
        if span < RESIZE_MIN_SPAN:
            return

        self._resize_frames += 1
        if self._resize_frames < RESIZE_ARM_FRAMES:
            self._resize_span = span      # settle before acting on it
            return
        if self._resize_span <= 0.0:
            self._resize_span = span
            return

        ratio = span / self._resize_span
        if abs(ratio - 1.0) < RESIZE_DEADZONE:
            return
        self._resize_span = span
        try:
            self._on_resize(ratio)
        except Exception as exc:
            print(f"[Gestures] resize handler failed: {exc}")

    def _fire(self, name: str, meta: dict) -> None:
        now = time.monotonic()
        if now - self._last_any_fire < GLOBAL_LOCKOUT:
            return
        if now - self._last_fire.get(name, 0.0) < COOLDOWNS.get(name, 1.0):
            return
        self._last_fire[name] = now
        self._last_any_fire = now
        print(f"[Gestures] {name} {meta or ''}")
        try:
            self._on_gesture(name, meta)
        except Exception as exc:
            print(f"[Gestures] handler for {name} failed: {exc}")


# ---------------------------------------------------------------- singleton

_ENGINE: GestureEngine | None = None


def set_engine(engine: GestureEngine | None) -> None:
    global _ENGINE
    _ENGINE = engine


def get_engine() -> GestureEngine | None:
    return _ENGINE


@contextlib.contextmanager
def camera_lease():
    """Hand the camera to another feature for the duration of the block.

    The gesture engine holds the webcam for the life of the process so a
    gesture can summon the UI cold; anything else that wants the camera
    (vision captures, the live HUD feed) borrows it through here.
    """
    engine = _ENGINE
    borrowed = False
    try:
        if engine is not None and engine.running and not engine.paused:
            engine.pause()
            borrowed = True
        yield
    finally:
        if borrowed and engine is not None:
            engine.resume()
