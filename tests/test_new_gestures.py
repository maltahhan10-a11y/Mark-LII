"""Synthetic tests for the fist-mute and two-hand resize gestures.

Neither can be checked by eye without a camera and a pair of hands, and both
are held states, so the thing worth testing is the *transitions*: engaging,
releasing, and the near-misses each guard against. The detectors are driven
directly with hand state, which keeps MediaPipe and the webcam out of it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gestures import (
    FIST_OFF_FRAMES, FIST_ON_FRAMES, RESIZE_ARM_FRAMES, RESIZE_DEADZONE,
    GestureEngine, _Hand,
)


def _engine():
    holds, resizes = [], []
    eng = GestureEngine(
        on_gesture=lambda n, m: None,
        on_hold=lambda name, active: holds.append((name, active)),
        on_resize=resizes.append,
    )
    return eng, holds, resizes


def _hand(**kw):
    h = _Hand()
    for k, v in kw.items():
        setattr(h, k, v)
    return h


# ── fist -> mute ────────────────────────────────────────────────────────────

def test_fist_engages_after_debounce_and_releases_on_open():
    eng, holds, _ = _engine()
    eng._hands = {0: _hand(fist=True)}
    for _ in range(FIST_ON_FRAMES - 1):
        eng._detect_fist()
    assert holds == [], "engaged before the debounce elapsed"
    eng._detect_fist()
    assert holds == [("mute", True)]

    eng._hands = {0: _hand(fist=False)}
    for _ in range(FIST_OFF_FRAMES - 1):
        eng._detect_fist()
    assert holds == [("mute", True)], "released before the debounce elapsed"
    eng._detect_fist()
    assert holds == [("mute", True), ("mute", False)]


def test_hand_leaving_frame_releases_the_mute():
    """Walking away must give the microphone back, not leave it dead."""
    eng, holds, _ = _engine()
    eng._hands = {0: _hand(fist=True)}
    for _ in range(FIST_ON_FRAMES):
        eng._detect_fist()
    eng._hands = {}
    for _ in range(FIST_OFF_FRAMES):
        eng._detect_fist()
    assert holds[-1] == ("mute", False)


def test_thumbs_up_does_not_mute():
    """A thumbs-up folds all four fingers too; the thumb is what separates it."""
    eng, holds, _ = _engine()
    eng._hands = {0: _hand(fist=False, thumb_out=True)}
    for _ in range(FIST_ON_FRAMES * 3):
        eng._detect_fist()
    assert holds == []


def test_a_flicker_does_not_release():
    """One dropped tracking frame must not flick the microphone back on."""
    eng, holds, _ = _engine()
    eng._hands = {0: _hand(fist=True)}
    for _ in range(FIST_ON_FRAMES):
        eng._detect_fist()
    eng._hands = {}
    eng._detect_fist()                      # a single lost frame
    eng._hands = {0: _hand(fist=True)}
    for _ in range(FIST_ON_FRAMES):
        eng._detect_fist()
    assert holds == [("mute", True)]


def test_a_ghost_hand_is_ignored():
    eng, holds, _ = _engine()
    eng._hands = {0: _hand(fist=True, ghost=True)}
    for _ in range(FIST_ON_FRAMES * 3):
        eng._detect_fist()
    assert holds == []


# ── two-hand pinch -> resize ────────────────────────────────────────────────

def _pinch_pair(ax, ay, bx, by):
    return {0: _hand(pinched=True, x=ax, y=ay),
            1: _hand(pinched=True, x=bx, y=by)}


def _arm(eng, span=0.30):
    eng._hands = _pinch_pair(0.0, 0.0, span, 0.0)
    for _ in range(RESIZE_ARM_FRAMES + 1):
        eng._detect_resize()


def test_pulling_apart_grows_and_together_shrinks():
    eng, _, sizes = _engine()
    _arm(eng, 0.30)
    sizes.clear()
    eng._hands = _pinch_pair(0.0, 0.0, 0.45, 0.0)     # apart
    eng._detect_resize()
    assert sizes and sizes[-1] > 1.0

    sizes.clear()
    eng._hands = _pinch_pair(0.0, 0.0, 0.22, 0.0)     # together
    eng._detect_resize()
    assert sizes and sizes[-1] < 1.0


def test_a_diagonal_pull_is_measured_on_the_true_separation():
    """The gesture is described as diagonal, so it must not read one axis."""
    eng, _, sizes = _engine()
    _arm(eng, 0.30)
    sizes.clear()
    # Same separation, rotated 45 degrees: distance is unchanged, so nothing
    # should be emitted just for turning the hands.
    d = 0.30 / (2 ** 0.5)
    eng._hands = _pinch_pair(0.0, 0.0, d, d)
    eng._detect_resize()
    assert sizes == []

    # Now pull along the diagonal: that is a real change and must register.
    eng._hands = _pinch_pair(0.0, 0.0, d * 1.5, d * 1.5)
    eng._detect_resize()
    assert sizes and sizes[-1] > 1.0


def test_ratios_compose_to_the_total_change():
    eng, _, sizes = _engine()
    _arm(eng, 0.20)
    sizes.clear()
    for span in (0.24, 0.30, 0.40):
        eng._hands = _pinch_pair(0.0, 0.0, span, 0.0)
        eng._detect_resize()
    total = 1.0
    for r in sizes:
        total *= r
    assert abs(total - (0.40 / 0.20)) < 1e-6


def test_one_hand_or_an_open_hand_does_nothing():
    eng, _, sizes = _engine()
    eng._hands = {0: _hand(pinched=True, x=0.0, y=0.0)}
    for _ in range(RESIZE_ARM_FRAMES * 2):
        eng._detect_resize()
    eng._hands = {0: _hand(pinched=True, x=0.0, y=0.0),
                  1: _hand(pinched=False, x=0.3, y=0.0)}
    for _ in range(RESIZE_ARM_FRAMES * 2):
        eng._detect_resize()
    assert sizes == []


def test_jitter_below_the_deadzone_is_ignored():
    eng, _, sizes = _engine()
    _arm(eng, 0.30)
    sizes.clear()
    eng._hands = _pinch_pair(0.0, 0.0, 0.30 * (1 + RESIZE_DEADZONE / 2), 0.0)
    eng._detect_resize()
    assert sizes == []


def test_releasing_and_regrabbing_does_not_jump():
    """A new grab continues from the current size, it does not snap back."""
    eng, _, sizes = _engine()
    _arm(eng, 0.30)
    sizes.clear()
    eng._hands = {}                       # let go
    eng._detect_resize()
    _arm(eng, 0.80)                       # grab again, hands much wider apart
    assert sizes == [], "re-grabbing emitted a jump"
