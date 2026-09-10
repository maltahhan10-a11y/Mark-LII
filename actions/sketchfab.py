"""Sketchfab: search a library of real, human-made 3D models and import them.

This is the opposite trade from TRELLIS. A generator invents a plausible shell
for anything you can describe; a library returns hand-built topology with real
UVs and materials, but only for things somebody already made. "A coffee mug" is
in here a thousand times over. "A cross between a shark and a helicopter" is
not in here at all, and no amount of searching will put it there.

Searching is free and needs no account. Downloading needs a token from a free
account. Every downloadable model carries a Creative Commons licence and most
of them legally require credit, so attribution is recorded next to the .blend
rather than left to the user to remember.
"""

from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from core import models

CACHE_DIR = Path.home() / "Documents" / "Jarvis Blender" / ".sketchfab"
SEARCH_URL = "https://api.sketchfab.com/v3/search"
MODEL_URL = "https://api.sketchfab.com/v3/models"

# Licences that permit both commercial use and modification. Anything outside
# this set can still be lovely work, but importing it into a user's project
# quietly saddles them with terms they never agreed to.
OPEN_LICENSES = "cc0,by,by-sa"
# NonCommercial included -- fine for personal use, not for shipping.
ALL_FREE_LICENSES = "cc0,by,by-sa,by-nc,by-nc-sa"

# A model under ~200 triangles is usually a placeholder cube someone uploaded
# by accident; past ~400k it will crawl in the viewport and is nearly always a
# scanned or sculpted asset that wants decimating before it is usable.
MIN_FACES = 200
IDEAL_FACES = (1_000, 150_000)
MAX_FACES = 400_000

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Jarvis/1.0"}


def _config() -> dict:
    return models.config()


def token() -> str:
    return (_config().get("sketchfab_token") or "").strip()


def configured() -> bool:
    return bool(token())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "model"


# ----------------------------------------------------------------- searching

_STOP = {
    "a", "an", "the", "of", "for", "with", "and", "some", "me", "my", "please",
    "make", "build", "create", "model", "3d", "add", "put", "in", "into", "on",
    "get", "find", "download", "import", "generate", "give", "want", "need",
    "blender", "scene", "object", "asset", "mesh",
}


def keywords(description: str) -> list[str]:
    """The searchable nouns in a spoken request.

    "can you make me a 3d model of a red ferrari" is a sentence, not a query.
    Sketchfab matches on titles and tags, so the filler has to come out or it
    drags every result toward whatever happens to be tagged "make".
    """
    words = re.findall(r"[a-z0-9']+", (description or "").lower())
    kept = [w for w in words if w not in _STOP and len(w) > 1]
    return kept or words


def search(query: str, licenses: str = OPEN_LICENSES, limit: int = 24,
           animated: bool | None = None) -> list[dict]:
    """Downloadable models matching a query. Never raises; [] on failure."""
    params = {
        "type": "models",
        "downloadable": "true",
        "q": query,
        "count": str(min(limit, 24)),
        "licenses": licenses,
        "sort_by": "-likeCount",
    }
    if animated is not None:
        params["animated"] = "true" if animated else "false"
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8")).get("results", []) or []
    except Exception as exc:
        print(f"[Sketchfab] search failed: {exc}")
        return []


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())


def score(query: str, model: dict) -> float:
    """How well one result answers the request. Higher is better.

    The hard case is not finding nothing, it is finding something confidently
    wrong: searching "dragon" surfaces a katana with a dragon on the handle,
    which matches the word perfectly and is not a dragon. Title length is what
    separates them -- a model called "Dragon" is a dragon, and every extra noun
    in the title is another thing the model might actually be instead.
    """
    terms = set(keywords(query))
    if not terms:
        return 0.0

    name = _norm(model.get("name", ""))
    name_words = [w for w in name.split() if w not in _STOP]
    name_set = set(name_words)
    tags = {_norm(t.get("name", "")) for t in (model.get("tags") or [])}

    hit = 0.0
    for t in terms:
        if t in name_set:
            hit += 1.0
        elif any(t == g for g in tags):
            hit += 0.7
        elif any(t in g or g in t for g in tags) or t in name:
            hit += 0.45
    coverage = hit / len(terms)
    if coverage == 0:
        return 0.0

    # Extra nouns in the title mean the model is probably that other thing.
    extra = len(name_set - terms)
    focus = 1.0 / (1.0 + 0.75 * extra)

    # An exact title is as unambiguous as this gets.
    if name_set and name_set == terms:
        focus = 1.6

    faces = int(model.get("faceCount") or 0)
    if faces < MIN_FACES:
        weight = 0.25                     # a stub or an empty upload
    elif faces > MAX_FACES:
        weight = 0.45                     # will bog the viewport down
    elif IDEAL_FACES[0] <= faces <= IDEAL_FACES[1]:
        weight = 1.0
    else:
        weight = 0.8

    # Popularity breaks ties; it must not decide the match. A beautifully
    # made "Spaceship Corridor" with 3000 likes is still not a spaceship, and
    # a wide multiplier here lets it outrank the plain one that is.
    likes = int(model.get("likeCount") or 0)
    quality = 1.0 + min(likes, 4000) / 10000.0    # 1.0 .. 1.4

    lic = (model.get("license") or {}).get("label", "")
    licence_bonus = 1.08 if "CC0" in lic else 1.0   # no attribution burden

    return coverage * focus * weight * quality * licence_bonus


def rank(query: str, results: list[dict]) -> list[dict]:
    scored = [(score(query, m), m) for m in results]
    scored = [(s, m) for s, m in scored if s > 0]
    scored.sort(key=lambda p: p[0], reverse=True)
    for s, m in scored:
        m["_score"] = round(s, 3)
    return [m for _s, m in scored]


def find(description: str, licenses: str = OPEN_LICENSES,
         limit: int = 24) -> list[dict]:
    """Best matches for a spoken description, best first."""
    query = " ".join(keywords(description))
    hits = rank(query, search(query, licenses, limit))
    if not hits and len(query.split()) > 1:
        # Fall back to the most specific single word: "vintage racing bicycle"
        # finds nothing, "bicycle" finds plenty.
        longest = max(query.split(), key=len)
        hits = rank(longest, search(longest, licenses, limit))
    return hits


# --------------------------------------------------------------- downloading

def _api(url: str, timeout: float = 40.0) -> dict:
    tok = token()
    if not tok:
        raise RuntimeError("no Sketchfab token")
    req = urllib.request.Request(url, headers={**_UA, "Authorization": f"Token {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        if exc.code == 401:
            raise RuntimeError(
                "the Sketchfab token was rejected — check it at "
                "https://sketchfab.com/settings/password"
            ) from None
        if exc.code == 403:
            raise RuntimeError(f"Sketchfab refused the download: {body or exc.code}") from None
        if exc.code == 404:
            raise RuntimeError("that model is no longer downloadable") from None
        raise RuntimeError(f"Sketchfab HTTP {exc.code}: {body}") from None


def download(uid: str, timeout: float = 180.0) -> tuple[bytes, str]:
    """Fetch a model's archive. Returns (data, kind) where kind is glb|gltf."""
    links = _api(f"{MODEL_URL}/{uid}/download")
    # glb is a single self-contained file; the gltf flavour is a zip of loose
    # files and textures that has to be unpacked before Blender will take it.
    for kind in ("glb", "gltf"):
        entry = links.get(kind) or {}
        url = entry.get("url")
        if not url:
            continue
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), kind
    raise RuntimeError("Sketchfab returned no glb or gltf for that model")


def _unpack_gltf(data: bytes, dest: Path) -> Path:
    """Unzip a gltf archive and return the .gltf to import."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)
    for pattern in ("*.gltf", "*.glb"):
        found = sorted(dest.rglob(pattern))
        if found:
            return found[0]
    raise RuntimeError("the archive held no gltf file")


def credit(model: dict) -> str:
    """The attribution line. CC-BY and CC-BY-SA require this by law."""
    name = model.get("name", "Untitled")
    author = ((model.get("user") or {}).get("displayName")
              or (model.get("user") or {}).get("username") or "unknown")
    lic = (model.get("license") or {}).get("label", "unknown licence")
    url = model.get("viewerUrl") or f"https://sketchfab.com/models/{model.get('uid','')}"
    return f'"{name}" by {author}, licensed {lic} — {url}'


def needs_credit(model: dict) -> bool:
    return "CC0" not in (model.get("license") or {}).get("label", "")


def fetch(model: dict) -> tuple[Path, str]:
    """Download a chosen model to disk, cached. Returns (path, kind)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    uid = model.get("uid", "")
    stem = f"{_slug(model.get('name',''))}-{uid[:8]}"
    glb = CACHE_DIR / f"{stem}.glb"
    if glb.exists() and glb.stat().st_size > 1024:
        return glb, "glb"
    folder = CACHE_DIR / stem
    existing = sorted(folder.glob("*.gltf")) if folder.exists() else []
    if existing:
        return existing[0], "gltf"

    data, kind = download(uid)
    if kind == "glb":
        glb.write_bytes(data)
        return glb, "glb"
    return _unpack_gltf(data, folder), "gltf"


# ------------------------------------------------------- shared credentials

def _addon_cache_files() -> list[Path]:
    """The official plugin's credential cache, one per installed Blender."""
    root = Path.home() / "Library" / "Application Support" / "Blender"
    return [p / "scripts" / "sketchfab_cache" / ".cache"
            for p in root.glob("*") if p.is_dir()]


def save_token(value: str) -> str:
    """Store the token for both the plugin and Jarvis.

    The official Blender plugin and this module talk to the same API with the
    same 'Authorization: Token ...' header, but they keep the credential in
    different places. Writing both means the user pastes the token once and
    the Sketchfab panel inside Blender is logged in too.
    """
    value = (value or "").strip()
    if not value:
        return "That did not look like a token."

    cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg["sketchfab_token"] = value
    cfg_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    models.invalidate()

    shared = 0
    for cache in _addon_cache_files():
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
            data["api_token"] = value
            cache.write_text(json.dumps(data), encoding="utf-8")
            shared += 1
        except Exception:
            pass

    who = whoami()
    tail = f" Signed in as {who}." if who else ""
    extra = f" The Blender plugin is signed in too." if shared else ""
    return f"Saved your Sketchfab token.{tail}{extra}"


def whoami() -> str:
    """The account name for a token, or '' if it is not valid."""
    try:
        req = urllib.request.Request(
            "https://api.sketchfab.com/v3/me",
            headers={**_UA, "Authorization": f"Token {token()}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            me = json.loads(resp.read().decode("utf-8"))
        return me.get("displayName") or me.get("username") or ""
    except Exception:
        return ""
