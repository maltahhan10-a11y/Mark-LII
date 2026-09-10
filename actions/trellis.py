"""Speech -> reference image -> 3D mesh, via Microsoft TRELLIS.2 on fal.ai.

The script builder in `blender_control` composes primitives, which is exactly
right for things with a structure you can describe -- a bridge has two towers,
a deck and a cable fan -- and exactly wrong for things whose whole point is
their shape. Nobody can write "a melting clock draped over a branch" as a
sequence of cylinders.

TRELLIS.2 is Microsoft's 4B-parameter 3D generator and covers that half. It
does not run here: it needs Linux and a 24GB NVIDIA card, and this is an Apple
M5, so it is called as a hosted endpoint on fal.ai.

It is also **image-to-3D only** -- there is no text conditioning in .2 -- so a
spoken request has to become a picture before it can become a mesh. Which kind
of picture is the interesting decision:

    a real, nameable thing   -> find a photograph of it
    ("the Sydney Opera House")  A generated picture of a landmark is a
                                plausible-looking lie; a photograph is the
                                thing itself, and TRELLIS reproduces what it
                                is shown.

    anything invented        -> generate the picture
    ("a melting clock made     No photograph exists, and this is where an
     of frosted glass")        abstract request actually lives. The generator
                               is what turns a description into a subject at
                               all.

Nothing here writes to Blender; `blender_control` imports the GLB this
produces. Generated meshes cost money per call, so results are cached on disk
by description exactly as the build scripts are.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from core import models

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
CACHE_DIR = Path.home() / "Documents" / "Jarvis Blender" / ".trellis"

FAL_QUEUE = "https://queue.fal.run"
FAL_MODEL = "fal-ai/trellis-2"
# fal's own image generator, used when Gemini's image quota is spent. Keeping a
# second route matters: the free Gemini image tier runs out quickly, and an
# exhausted quota should not take the abstract half of this feature down.
FAL_IMAGE_MODEL = "fal-ai/flux/schnell"

SUBMIT_TIMEOUT = 60.0
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 600.0        # a 1536-resolution generation is not quick
DOWNLOAD_TIMEOUT = 300.0

# A data URI avoids a second API surface (fal's upload endpoint) for what is
# usually a few hundred KB. Past this, upload properly instead of inlining.
MAX_INLINE_BYTES = 4 * 1024 * 1024


# ── configuration ───────────────────────────────────────────────────────────

def _config() -> dict:
    return models.config()


def fal_key() -> str:
    cfg = _config()
    for name in ("fal_key", "fal_api_key", "FAL_KEY"):
        key = (cfg.get(name) or "").strip()
        if key:
            return key
    return ""


def configured() -> bool:
    return bool(fal_key())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "asset"


def _key(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").lower().split()).encode()).hexdigest()[:12]


def cache_path(description: str, quality: str = "standard") -> Path:
    return CACHE_DIR / f"{_slug(description)}-{_key(description + quality)}.glb"


# ── deciding which kind of picture to look for ──────────────────────────────

# Words that mean "this is invented, no photograph of it exists". A request
# carrying any of them goes to the generator even if it also names something
# real -- "a dragon made of Big Ben" is not a photograph of Big Ben.
_ABSTRACT_HINTS = (
    "abstract", "surreal", "imaginary", "fictional", "invented", "impossible",
    "dream", "dreamlike", "melting", "made of", "made out of", "in the style",
    "stylised", "stylized", "fantasy", "alien", "creature", "monster",
    "concept", "futuristic", "steampunk", "cyberpunk", "organic", "sculpture",
    "sculpted", "twisted", "flowing", "morphing", "hybrid", "cross between",
    "reimagined", "cartoon", "low poly", "voxel", "if ", "as if",
)

# A proper noun is the strongest signal that a real photograph exists.
_PROPER = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*\b")
_STOPWORDS = frozenset({
    "Make", "Build", "Create", "Model", "Give", "Show", "Please", "Can",
    "Could", "Would", "The", "This", "That", "And", "For", "With", "From",
    "Blender", "Jarvis", "A", "An", "I", "It", "My",
})


def wants_generated_image(description: str) -> bool:
    """True if this should be drawn rather than photographed.

    Errs toward generating. A generated picture of a real object is merely a
    slightly-off reference; a photograph of the wrong thing entirely -- which
    is what an image search returns for "a melting clock made of glass" -- is
    a wrong build.
    """
    text = (description or "").strip()
    if not text:
        return True
    low = text.lower()
    if any(hint in low for hint in _ABSTRACT_HINTS):
        return True
    named = [m for m in _PROPER.findall(text) if m not in _STOPWORDS]
    return not named


# ── getting the reference picture ───────────────────────────────────────────

def _http(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        # Some image hosts refuse a bare urllib user agent.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def search_image(description: str, tries: int = 5) -> tuple[bytes, str]:
    """The best photograph of a real subject. Returns (png/jpeg bytes, source)."""
    try:
        from ddgs import DDGS
    except ImportError:
        return b"", "the ddgs package is not installed"

    # A plain object shot reconstructs far better than a scene: TRELLIS models
    # what fills the frame, so a cluttered photo becomes a cluttered mesh.
    query = f"{description} full view isolated"
    results = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=tries * 2))
    except Exception as exc:
        return b"", f"image search failed: {exc}"
    if not results:
        return b"", "no images found"

    # Prefer big and roughly square: a panorama crops badly into a 3D subject.
    def rank(r):
        try:
            w, h = int(r.get("width") or 0), int(r.get("height") or 0)
        except (TypeError, ValueError):
            return (1, 0)
        if not w or not h:
            return (1, 0)
        ratio = max(w, h) / max(1, min(w, h))
        return (0 if ratio <= 2.0 else 1, -(w * h))

    for r in sorted(results, key=rank)[:tries]:
        url = r.get("image") or ""
        if not url:
            continue
        try:
            data = _http(url, 25.0)
        except Exception:
            continue
        if len(data) > 4000:          # skip placeholders and error pages
            return data, r.get("source") or url
    return b"", "every candidate image failed to download"


def generate_image(description: str) -> tuple[bytes, str]:
    """Draw the subject. Gemini first, then fal -- see FAL_IMAGE_MODEL."""
    prompt = (
        f"{description}. A single subject, centred, filling the frame, "
        "photographed against a plain neutral background. Even, soft "
        "lighting with no harsh shadows. The entire object visible, nothing "
        "cropped, nothing else in shot. Three-quarter view."
    )
    try:
        resp = models.call("image", prompt)
        for part in resp.candidates[0].content.parts:
            blob = getattr(part, "inline_data", None)
            if blob is not None and blob.data:
                return blob.data, "generated (Gemini)"
    except Exception as exc:
        print(f"[TRELLIS] Gemini image generation unavailable: {exc}")

    data, err = _fal_image(prompt)
    if data:
        return data, "generated (fal)"
    return b"", err or "no image generator available"


def _fal_image(prompt: str) -> tuple[bytes, str]:
    key = fal_key()
    if not key:
        return b"", "no fal key configured"
    try:
        out = _fal_run(FAL_IMAGE_MODEL, {"prompt": prompt, "image_size": "square_hd"},
                       key, poll_timeout=180.0)
        images = out.get("images") or []
        if not images:
            return b"", "fal returned no image"
        return _http(images[0]["url"], DOWNLOAD_TIMEOUT), ""
    except Exception as exc:
        return b"", f"fal image generation failed: {exc}"


def reference_image(description: str, prefer: str = "auto") -> tuple[bytes, str]:
    """The picture TRELLIS will be shown. `prefer`: auto | search | generate.

    Falls through in both directions: a search that finds nothing is drawn
    instead, and a generator that is out of quota falls back to a photograph.
    Either reference beats failing the request.
    """
    mode = (prefer or "auto").strip().lower()
    if mode == "auto":
        mode = "generate" if wants_generated_image(description) else "search"

    order = ("generate", "search") if mode == "generate" else ("search", "generate")
    problems = []
    for step in order:
        data, note = (generate_image(description) if step == "generate"
                      else search_image(description))
        if data:
            return data, note
        problems.append(f"{step}: {note}")
    return b"", "; ".join(problems)


# ── fal.ai queue ────────────────────────────────────────────────────────────

def _fal_error(exc) -> str:
    """Turn an HTTPError into the reason fal actually gave.

    urllib renders every 403 as "HTTP Error 403: Forbidden", which hides the
    one thing worth knowing. fal puts a plain sentence in the body -- an empty
    balance, an unknown model, a malformed input -- and each of those wants a
    different response from whoever is reading the log.
    """
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list) and detail:
        detail = "; ".join(
            str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail
        )
    if not detail:
        return f"HTTP {exc.code}"
    text = str(detail)
    if "balance" in text.lower():
        return (
            "the fal.ai account has no credit left — top it up at "
            "https://fal.ai/dashboard/billing"
        )
    return f"HTTP {exc.code}: {text}"


def _fal_post(url: str, payload: dict, key: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_fal_error(exc)) from None


def _fal_get(url: str, key: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Key {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_fal_error(exc)) from None


def _fal_run(model: str, payload: dict, key: str,
             poll_timeout: float = POLL_TIMEOUT, log=None) -> dict:
    """Submit, poll, and return the finished output. Raises on failure."""
    submitted = _fal_post(f"{FAL_QUEUE}/{model}", payload, key, SUBMIT_TIMEOUT)
    request_id = submitted.get("request_id")
    if not request_id:
        raise RuntimeError(f"fal did not return a request id: {submitted}")

    status_url = f"{FAL_QUEUE}/{model}/requests/{request_id}/status"
    result_url = f"{FAL_QUEUE}/{model}/requests/{request_id}"
    deadline = time.time() + poll_timeout
    last = ""
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        state = _fal_get(status_url, key, 30.0)
        status = (state.get("status") or "").upper()
        if status != last:
            last = status
            if log:
                pos = state.get("queue_position")
                log(f"{status.lower()}" + (f" (queue position {pos})" if pos else ""))
        if status == "COMPLETED":
            return _fal_get(result_url, key, 60.0)
        if status in ("FAILED", "CANCELLED", "ERROR"):
            raise RuntimeError(f"fal request {status.lower()}: {state}")
    raise RuntimeError(f"fal request timed out after {poll_timeout:.0f}s")


def _data_uri(image: bytes) -> str:
    if len(image) > MAX_INLINE_BYTES:
        raise RuntimeError(
            f"reference image is {len(image) // 1024}KB, over the "
            f"{MAX_INLINE_BYTES // 1024}KB inline limit"
        )
    kind = "png" if image[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(image).decode("ascii")


# Quality presets. TRELLIS.2's own default decimation target is 500,000
# vertices, which is a film asset -- it imports slowly, drags the viewport and
# is miserable to edit. These keep the silhouette and surface detail while
# landing at a size Blender stays responsive at, which is what "optimised but
# still detailed" actually means in practice.
QUALITY = {
    "fast":     {"resolution": 512,  "decimation_target": 30_000,
                 "texture_size": 1024, "ss_sampling_steps": 12,
                 "shape_slat_sampling_steps": 12, "tex_slat_sampling_steps": 12},
    "standard": {"resolution": 1024, "decimation_target": 80_000,
                 "texture_size": 2048, "ss_sampling_steps": 12,
                 "shape_slat_sampling_steps": 12, "tex_slat_sampling_steps": 12},
    "best":     {"resolution": 1536, "decimation_target": 200_000,
                 "texture_size": 4096, "ss_sampling_steps": 20,
                 "shape_slat_sampling_steps": 20, "tex_slat_sampling_steps": 20},
}


def image_to_3d(image: bytes, quality: str = "standard", log=None) -> bytes:
    """Run TRELLIS.2 on `image` and return the GLB bytes."""
    key = fal_key()
    if not key:
        raise RuntimeError(
            "No fal.ai key configured. Add \"fal_key\" to config/api_keys.json "
            "-- get one at https://fal.ai/dashboard/keys"
        )
    preset = QUALITY.get((quality or "standard").lower(), QUALITY["standard"])
    payload = {"image_url": _data_uri(image), "remesh": True, **preset}
    if log:
        log(f"sending the reference to TRELLIS.2 at {preset['resolution']}...")
    out = _fal_run(FAL_MODEL, payload, key, log=log)
    glb = (out.get("model_glb") or {}).get("url")
    if not glb:
        raise RuntimeError(f"TRELLIS returned no mesh: {out}")
    if log:
        log("downloading the mesh...")
    return _http(glb, DOWNLOAD_TIMEOUT)


def build(description: str, quality: str = "standard", prefer: str = "auto",
          reuse: bool = True, log=None) -> tuple[Path | None, str]:
    """description -> a .glb on disk. Returns (path, note-or-error).

    Cached by description and quality: a generated mesh costs real money, and
    re-running the same request should not spend it twice.
    """
    def say(msg: str) -> None:
        print(f"[TRELLIS] {msg}")
        if log:
            log(msg)

    if not description or not description.strip():
        return None, "What would you like me to make?"

    target = cache_path(description, quality)
    if reuse and target.exists() and target.stat().st_size > 1024:
        say("reusing the mesh I already generated for this (no API call).")
        return target, "from cache"

    if not configured():
        return None, (
            "I need a fal.ai key to generate 3D models. Add \"fal_key\" to "
            "config/api_keys.json -- you can create one at "
            "https://fal.ai/dashboard/keys"
        )

    say("finding a reference image...")
    image, source = reference_image(description, prefer)
    if not image:
        return None, f"I couldn't get a reference image. ({source})"
    say(f"reference: {source}, {len(image) // 1024}KB")

    try:
        glb = image_to_3d(image, quality, log=say)
    except Exception as exc:
        return None, f"TRELLIS failed: {exc}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(glb)
    except Exception as exc:
        return None, f"Could not save the mesh: {exc}"
    return target, source
