"""Face scanning for MARK LII — a party trick, done properly.

Detects the faces in front of the camera, guesses an age and a gender for
each, and recognises people who have been introduced by name.

On DeepFace: it cannot run here. DeepFace requires TensorFlow, and TensorFlow
publishes no wheel for Python 3.14, so `pip install deepface` is unresolvable
on this machine. Everything below uses models OpenCV can run directly through
its own DNN backend, which needs no extra packages at all:

    YuNet    face detection      (OpenCV Zoo)
    SFace    128-d face embeddings for matching   (OpenCV Zoo)
    GoogLeNet age + gender       (ONNX model zoo, Levi & Hassner)

Age here is a guess and is presented as one. The age network sorts a face into
eight coarse buckets and its output is close to flat, so the *bucket* is the
only real signal — averaging the distribution returns about 29 for everybody,
which is why this reports a range rather than a number.

Deliberately absent: the ethnicity classifier DeepFace ships. It has no sound
scientific basis and no place in a family demo.
"""
from __future__ import annotations

import json
import platform
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = BASE_DIR / "config" / "models" / "face"
FACES_PATH = BASE_DIR / "config" / "faces.json"

# The opencv_zoo files are stored in Git LFS: the raw.githubusercontent URL
# serves a ~130-byte pointer file that loads as a corrupt model, so these must
# come from the media host.
MODELS = {
    "yunet.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        100_000,
    ),
    "sface.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        1_000_000,
    ),
    "age.onnx": (
        "https://media.githubusercontent.com/media/onnx/models/main/validated/"
        "vision/body_analysis/age_gender/models/age_googlenet.onnx",
        1_000_000,
    ),
    "gender.onnx": (
        "https://media.githubusercontent.com/media/onnx/models/main/validated/"
        "vision/body_analysis/age_gender/models/gender_googlenet.onnx",
        1_000_000,
    ),
}

AGE_BUCKETS = [(0, 2), (4, 6), (8, 12), (15, 20), (25, 32), (38, 43),
               (48, 53), (60, 100)]
# Two phrasings per bucket: the age is spoken back either to the person in
# front of the camera or about them, and one list cannot serve both without
# producing "you look in their late twenties".
AGE_WORDS_YOU = ["like a toddler", "about five", "around ten",
                 "like a teenager", "to be in your late twenties",
                 "around forty", "around fifty", "over sixty"]
AGE_WORDS_THEY = ["like a toddler", "about five", "around ten",
                  "like a teenager", "to be in their late twenties",
                  "around forty", "around fifty", "over sixty"]

# OpenCV's published SFace threshold: cosine similarity at or above this means
# the same person.
MATCH_THRESHOLD = 0.363
# The face crop is squared up and padded before the age net sees it. Tested
# against a tight crop, which pushed an adult into the 8-12 bucket.
CROP_MARGIN = 0.5
DETECT_CONFIDENCE = 0.80

_engine: dict | None = None


def _check_ready() -> str | None:
    if platform.system() != "Darwin" and not MODEL_DIR.exists():
        pass          # nothing platform-specific here; the camera is the limit
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        return f"Face scanning needs OpenCV and numpy ({exc})."
    return None


def ensure_models(log=None) -> str | None:
    """Download the four model files once. Returns an error string or None."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, min_size) in MODELS.items():
        path = MODEL_DIR / name
        if path.exists() and path.stat().st_size >= min_size:
            continue
        if log:
            log(f"Downloading the {name.split('.')[0]} model…")
        try:
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https URLs
            if tmp.stat().st_size < min_size:
                tmp.unlink(missing_ok=True)
                return (f"The {name} download came back too small — the model "
                        "host may have changed.")
            tmp.replace(path)
        except Exception as exc:
            return f"Could not download the face models: {exc}"
    return None


def _load(log=None):
    """Build the detector, recogniser and attribute nets once."""
    global _engine
    if _engine is not None:
        return _engine, None

    err = _check_ready() or ensure_models(log)
    if err:
        return None, err

    import cv2

    try:
        _engine = {
            "cv2": cv2,
            "detector": cv2.FaceDetectorYN.create(
                str(MODEL_DIR / "yunet.onnx"), "", (320, 320),
                DETECT_CONFIDENCE, 0.3, 5000,
            ),
            "recognizer": cv2.FaceRecognizerSF.create(
                str(MODEL_DIR / "sface.onnx"), ""
            ),
            "age": cv2.dnn.readNetFromONNX(str(MODEL_DIR / "age.onnx")),
            "gender": cv2.dnn.readNetFromONNX(str(MODEL_DIR / "gender.onnx")),
        }
    except Exception as exc:
        return None, f"Could not load the face models: {exc}"
    return _engine, None


# ── camera ──────────────────────────────────────────────────────────────────

def _capture(warmup: int = 8):
    """One frame from the webcam, borrowed from the gesture engine."""
    import cv2

    try:
        from core.gestures import camera_lease
    except Exception:
        import contextlib
        camera_lease = contextlib.nullcontext

    try:
        index = int(json.loads(
            (BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8")
        ).get("camera_index", 0))
    except Exception:
        index = 0

    with camera_lease():
        try:
            backend = cv2.CAP_AVFOUNDATION if platform.system() == "Darwin" else cv2.CAP_ANY
        except AttributeError:
            backend = 0
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            return None
        for _ in range(warmup):
            cap.read()
        ok, frame = cap.read()
        cap.release()
    return frame if ok else None


# ── analysis ────────────────────────────────────────────────────────────────

def _detect(engine, frame):
    cv2 = engine["cv2"]
    height, width = frame.shape[:2]
    engine["detector"].setInputSize((width, height))
    _count, faces = engine["detector"].detect(frame)
    if faces is None:
        return []
    # Biggest first: the nearest face is the one being asked about.
    return sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)


def _square_crop(frame, face, margin: float = CROP_MARGIN):
    """A padded, square crop. The age net was trained on faces with context;
    a tight box reads an adult as a child."""
    height, width = frame.shape[:2]
    x, y, w, h = (float(v) for v in face[:4])
    cx, cy = x + w / 2, y + h / 2
    side = max(w, h) * (1.0 + margin)
    x0 = max(int(cx - side / 2), 0)
    y0 = max(int(cy - side / 2), 0)
    x1 = min(int(cx + side / 2), width)
    y1 = min(int(cy + side / 2), height)
    return frame[y0:y1, x0:x1]


def _distributions(engine, frame, face):
    """Raw age and gender probabilities for one face in one frame."""
    import numpy as np

    cv2 = engine["cv2"]
    crop = _square_crop(frame, face)
    if crop.size == 0:
        return None, None
    blob = cv2.dnn.blobFromImage(crop, 1.0, (224, 224), (104, 117, 123),
                                 swapRB=False)

    def softmax(vec):
        shifted = np.exp(vec - vec.max())
        return shifted / shifted.sum()

    engine["age"].setInput(blob)
    ages = softmax(engine["age"].forward()[0])
    engine["gender"].setInput(blob)
    genders = softmax(engine["gender"].forward()[0])
    return ages, genders


def _attributes(engine, frame, face) -> dict:
    ages, genders = _distributions(engine, frame, face)
    if ages is None:
        return {}
    return _label(ages, genders)


def _attributes_over(engine, samples) -> dict:
    """Average the estimates across several frames of the same face.

    A single frame is not stable enough for this to be fun: the same person
    read as "a teenager" on one call and "late twenties" on the next, because
    the age network is barely confident and a blink or a shadow tips which
    bucket wins. Averaging a handful of frames settles it.
    """
    import numpy as np

    age_sum, gender_sum, used = None, None, 0
    for frame, face in samples:
        ages, genders = _distributions(engine, frame, face)
        if ages is None:
            continue
        age_sum = ages if age_sum is None else age_sum + ages
        gender_sum = genders if gender_sum is None else gender_sum + genders
        used += 1
    if not used:
        return {}
    return _label(age_sum / used, gender_sum / used, frames=used)


def _label(ages, genders, frames: int = 1) -> dict:
    import numpy as np

    top = int(np.argmax(ages))
    low, high = AGE_BUCKETS[top]
    return {
        "age_range": (low, high),
        "age_words": AGE_WORDS_THEY[top],
        "age_words_you": AGE_WORDS_YOU[top],
        "age_confidence": float(ages[top]),
        "gender": "male" if genders[0] > genders[1] else "female",
        "gender_confidence": float(max(genders)),
        "frames": frames,
    }


def _capture_many(count: int = 3, gap: float = 0.12) -> list:
    """A few frames in one camera session, so the lease is taken once."""
    import cv2

    try:
        from core.gestures import camera_lease
    except Exception:
        import contextlib
        camera_lease = contextlib.nullcontext

    try:
        index = int(json.loads(
            (BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8")
        ).get("camera_index", 0))
    except Exception:
        index = 0

    frames = []
    with camera_lease():
        try:
            backend = cv2.CAP_AVFOUNDATION if platform.system() == "Darwin" else cv2.CAP_ANY
        except AttributeError:
            backend = 0
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            return []
        for _ in range(8):
            cap.read()                 # let exposure settle
        for i in range(max(count, 1)):
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
            if i + 1 < count:
                time.sleep(gap)
        cap.release()
    return frames


def _group_faces(engine, frames) -> list:
    """Track the same face across frames by how close the boxes sit."""
    import math

    groups: list[dict] = []
    for frame in frames:
        for face in _detect(engine, frame):
            cx, cy = face[0] + face[2] / 2, face[1] + face[3] / 2
            for group in groups:
                gf = group["face"]
                gx, gy = gf[0] + gf[2] / 2, gf[1] + gf[3] / 2
                if math.hypot(cx - gx, cy - gy) < max(float(face[2]), float(gf[2])) * 0.6:
                    group["samples"].append((frame, face))
                    break
            else:
                groups.append({"face": face, "frame": frame,
                               "samples": [(frame, face)]})
    groups.sort(key=lambda g: float(g["face"][2]) * float(g["face"][3]),
                reverse=True)
    return groups


def _embed(engine, frame, face):
    aligned = engine["recognizer"].alignCrop(frame, face)
    return engine["recognizer"].feature(aligned)


# ── the roll of known faces ─────────────────────────────────────────────────

def _load_faces() -> dict:
    try:
        data = json.loads(FACES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_faces(people: dict) -> None:
    FACES_PATH.parent.mkdir(parents=True, exist_ok=True)
    FACES_PATH.write_text(json.dumps(people, indent=2), encoding="utf-8")


def _identify(engine, embedding) -> tuple[str | None, float]:
    """Closest enrolled person, and the score. None if nobody is close enough."""
    import numpy as np

    cv2 = engine["cv2"]
    best_name, best_score = None, 0.0
    for name, samples in _load_faces().items():
        for sample in samples:
            known = np.array(sample, dtype=np.float32).reshape(1, -1)
            score = float(engine["recognizer"].match(
                embedding, known, cv2.FaceRecognizerSF_FR_COSINE
            ))
            if score > best_score:
                best_name, best_score = name, score
    if best_score >= MATCH_THRESHOLD:
        return best_name, best_score
    return None, best_score


# ── actions ─────────────────────────────────────────────────────────────────

def _describe(index: int, face, attrs: dict, who: str | None) -> str:
    """One line about one face.

    A recognised face reports what is *known* about the person -- name, age,
    what they do -- because a stored fact beats a guess from pixels every
    time. The estimator only speaks for strangers, and for anyone recognised
    whose profile has no age saved.
    """
    if who:
        spoken, _follow, _rest = _profile(who)
        if spoken and spoken != who:
            return spoken
        # Known face, nothing saved about them: fall through to the estimate
        # rather than saying a bare name.

    low, high = attrs.get("age_range", (0, 0))
    name = who or f"Person {index + 1}"
    bits = [f"{name}: looks {attrs.get('age_words', 'hard to place')} "
            f"({low}\u2013{high})"]
    if attrs.get("gender"):
        bits.append(attrs["gender"])
    return ", ".join(bits)


def _profile(name: str) -> tuple[str, str, list]:
    """(spoken, follow_up, remaining) for a known person. Never raises."""
    try:
        from actions import people
        return people.summary(name)
    except Exception as exc:
        print(f"[Face] Profile lookup failed for {name}: {exc}")
        return name, "", []


def _annotate(engine, frame, rows) -> bytes | None:
    """Draw the boxes and labels, and hand the picture back as PNG bytes."""
    cv2 = engine["cv2"]
    # OpenCV works in BGR, so the assistant's amber is (0, 212, 255) here —
    # written the other way round it comes out cyan.
    ACCENT = (0, 212, 255)
    canvas = frame.copy()
    thickness = max(2, canvas.shape[1] // 480)
    font_scale = canvas.shape[1] / 1100.0
    for face, attrs, who in rows:
        x, y, w, h = (int(v) for v in face[:4])
        cv2.rectangle(canvas, (x, y), (x + w, y + h), ACCENT, thickness)
        low, high = attrs.get("age_range", (0, 0))
        label = f"{who or 'unknown'}  {low}-{high}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                      font_scale, thickness)
        top = max(y - th - 12, 0)
        cv2.rectangle(canvas, (x, top), (x + tw + 12, y), ACCENT, -1)
        cv2.putText(canvas, label, (x + 6, y - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale, (0, 0, 0), thickness)
    # Downscale before encoding: a full 1920x1080 PNG is nearly 3 MB, and the
    # overlay it lands in is a few hundred pixels wide.
    height, width = canvas.shape[:2]
    if width > 960:
        scale = 960.0 / width
        canvas = cv2.resize(canvas, (960, int(height * scale)),
                            interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", canvas,
                              [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return buffer.tobytes() if ok else None


def _show(player, image: bytes | None) -> None:
    if player is not None and image:
        try:
            player.show_camera_frame(image)
        except Exception:
            pass


def scan(player=None) -> str:
    """Look at everyone in front of the camera and report on each."""
    def log(msg):
        if player is not None:
            try:
                player.write_log(f"SYS: {msg}")
            except Exception:
                pass

    engine, err = _load(log)
    if err:
        return err

    frames = _capture_many(3)
    if not frames:
        return ("I couldn't open the camera. Check that nothing else is using "
                "it and that camera access is allowed.")

    groups = _group_faces(engine, frames)
    if not groups:
        return "I can't see anyone in front of the camera."

    rows, lines = [], []
    for index, group in enumerate(groups[:6]):
        attrs = _attributes_over(engine, group["samples"])
        who, _score = _identify(
            engine, _embed(engine, group["frame"], group["face"])
        )
        rows.append((group["face"], attrs, who))
        lines.append(_describe(index, group["face"], attrs, who))

    _show(player, _annotate(engine, frames[0], rows))
    count = len(rows)
    head = "One face" if count == 1 else f"{count} faces"
    # The age estimate is a guess and should be flagged as one -- but only
    # when it was actually used. Saying "ages are a rough guess" after
    # reciting a stored date of birth is wrong.
    hedge = " (Ages are a rough guess.)" if any(
        who is None or not _profile(who)[0].strip() or _profile(who)[0] == who
        for _face, _attrs, who in rows
    ) else ""
    return f"{head}. " + ". ".join(lines) + "." + hedge + _follow_ups(rows)


def _follow_ups(rows) -> str:
    """The offer of everything that did not make the spoken summary.

    Kept to two questions. Everything stored about a person cannot be recited
    at them -- that is the whole point of the tiering -- but a wall of
    questions is no better, so the rest is acknowledged rather than listed.
    """
    asks: list[str] = []
    extra = 0
    for _face, _attrs, who in rows:
        if not who:
            continue
        _spoken, follow, rest = _profile(who)
        if follow:
            asks.append(follow)
        if len(rest) > 1:
            extra += len(rest) - 1
    if not asks:
        return ""
    out = " " + " ".join(asks[:2])
    if len(asks) > 2:
        out += f" I have details on {len(asks) - 2} more of them too."
    elif extra:
        out += " There's more saved as well, if you want it."
    return out


def guess_age(player=None) -> str:
    """The nearest face only — the 'guess my age' party trick."""
    engine, err = _load()
    if err:
        return err

    frames = _capture_many(4)
    if not frames:
        return "I couldn't open the camera."

    groups = _group_faces(engine, frames)
    if not groups:
        return "I can't see a face to look at."

    group = groups[0]
    face = group["face"]
    attrs = _attributes_over(engine, group["samples"])
    who, _score = _identify(engine, _embed(engine, group["frame"], face))
    _show(player, _annotate(engine, frames[0], [(face, attrs, who)]))

    low, high = attrs["age_range"]
    lead = f"{who}, you look" if who else "You look"
    return (f"{lead} {attrs['age_words_you']} — somewhere between {low} and "
            f"{high}. Don't hold me to it.")


def enroll(name: str, player=None) -> str:
    """Learn a face under a name, so it can be recognised later."""
    if not name or not name.strip():
        return "Whose face am I learning?"
    name = name.strip()

    engine, err = _load()
    if err:
        return err

    frames = _capture_many(4, gap=0.3)
    if not frames:
        return "I couldn't open the camera."

    samples = []
    for frame in frames:
        faces = _detect(engine, frame)
        if faces:
            samples.append(_embed(engine, frame, faces[0]).flatten().tolist())

    if not samples:
        return f"I couldn't get a clear look at a face to save as {name}."

    people = _load_faces()
    people.setdefault(name, [])
    people[name].extend(samples)
    people[name] = people[name][-9:]       # a few angles is plenty
    _save_faces(people)
    return (f"Got it — I'll recognise {name} now. "
            f"({len(samples)} view{'s' if len(samples) != 1 else ''} saved.)")


def identify(player=None) -> str:
    """Name whoever is in front of the camera."""
    engine, err = _load()
    if err:
        return err
    if not _load_faces():
        return ("I don't know anyone's face yet. Introduce someone first — "
                "say something like 'remember this face as Alex'.")

    frames = _capture_many(3)
    if not frames:
        return "I couldn't open the camera."
    groups = _group_faces(engine, frames)
    if not groups:
        return "I can't see anyone."

    rows, names = [], []
    for group in groups[:6]:
        face = group["face"]
        who, score = _identify(engine, _embed(engine, group["frame"], face))
        attrs = _attributes_over(engine, group["samples"])
        rows.append((face, attrs, who))
        names.append(f"{who} ({score:.0%} sure)" if who else "someone I don't know")

    _show(player, _annotate(engine, frames[0], rows))
    return "I can see " + ", ".join(names) + "." + _follow_ups(rows)


def forget(name: str) -> str:
    if not name or not name.strip():
        return "Whose face should I forget?"
    people = _load_faces()
    for key in list(people):
        if key.lower() == name.strip().lower():
            del people[key]
            _save_faces(people)
            return f"Forgotten {key}'s face."
    return f"I don't have a face saved for {name}."


def list_people() -> str:
    people = _load_faces()
    if not people:
        return "I don't know anyone's face yet."
    entries = ", ".join(f"{n} ({len(v)} views)" for n, v in people.items())
    return f"Faces I know: {entries}."


def face_id(parameters: dict, player=None) -> str:
    """Entry point — routes on parameters["action"]."""
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()
    action = action.replace("-", "_").replace(" ", "_")
    name = params.get("name") or params.get("person") or ""

    if player is not None:
        try:
            player.write_log(f"[Face] {action}")
        except Exception:
            pass

    if action in ("scan", "scan_faces", "look", "detect"):
        return scan(player)
    if action in ("guess_age", "age", "how_old", "guess"):
        return guess_age(player)
    if action in ("enroll", "remember", "learn", "add"):
        return enroll(name, player)
    if action in ("identify", "who", "recognise", "recognize", "who_is_this"):
        return identify(player)
    if action in ("forget", "remove", "delete"):
        return forget(name)
    if action in ("list", "list_people", "known"):
        return list_people()
    # Answers the follow-up a scan offered: "yes, tell me his courses".
    if action in ("details", "detail", "about", "more", "tell_me_more", "info"):
        attr = (params.get("attribute") or params.get("about")
                or params.get("topic") or params.get("detail") or "")
        try:
            from actions import people
            if not name:
                return "Who do you want to know about?"
            if not attr:
                spoken, follow, rest = people.summary(name)
                if not rest:
                    return f"{spoken}. That's everything I have saved."
                return f"{spoken}. I also have: {', '.join(rest)}."
            return people.details(name, attr)
        except Exception as exc:
            return f"I couldn't look that up: {exc}"

    return (f"Unknown face action: '{action}'. "
            "Valid actions: scan | guess_age | enroll | identify | forget | "
            "list | details.")
