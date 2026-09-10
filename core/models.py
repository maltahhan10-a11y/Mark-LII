"""Task-routed model selection, cached config, and pooled Gemini clients.

Before this module every call site picked its own model by hand, so the same
string ("gemini-flash-latest") appeared in a dozen files and a model swap meant
a dozen edits. Worse, the choice was uniform: a one-line "is this a click or a
scroll?" classification paid for the same model that writes a 600-line Blender
script.

`for_task()` is the single place that decision now lives. Each task maps to an
*ordered* tuple of candidates rather than one name, because Gemini model ids
come and go — a 404 on the first should fall through to the next instead of
failing the user's request. `call()` does that walk for you.

Everything is overridable from config/api_keys.json without touching code:

    "models": {
        "code":  "gemini-3.7-flash",
        "image": ["imagen-4.0-generate-001", "gemini-3-pro-image-preview"]
    }

A string or a list both work; a list is treated as the candidate order.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


# ── the registry ────────────────────────────────────────────────────────────
# Ordered by preference. Pro models are deliberately absent from the default
# lists: they answer 404 or 429 on a standard key, and burning a failed call
# before reaching one that works costs seconds on every single request.
_REGISTRY: dict[str, tuple[str, ...]] = {
    # Conversation, summaries, rewriting — the default for anything textual.
    "text":   ("gemini-flash-latest", "gemini-2.5-flash"),

    # Writing and repairing code: Blender build scripts, dev_agent, code_helper.
    # Given the largest, most capable candidate first — a truncated or wrong
    # script costs a whole retry cycle, which is far more expensive than the
    # slightly slower model.
    # A long chain on purpose. Writing a build script is the one task with no
    # graceful degradation -- if every candidate 503s the user gets nothing at
    # all -- and flash models return "high demand" often enough that three
    # candidates were observed exhausting themselves inside a minute. Every
    # name here was checked against models.list() on this account.
    "code":   ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
               "gemini-flash-latest", "gemini-3-flash-preview",
               "gemini-2.5-flash", "gemini-2.5-pro"),

    # Text -> picture. Imagen is deliberately absent: it was deprecated and
    # shut down on 2026-08-17, so listing it as a fallback would just burn a
    # failed call before reaching something that works. These are called
    # through generate_content, not generate_images, and return the picture as
    # an inline_data part.
    "image":  ("gemini-3.1-flash-image", "gemini-3-pro-image-preview",
               "gemini-2.5-flash-image"),

    # Looking at pixels: screenshots, renders, camera frames, PDFs.
    "vision": ("gemini-flash-latest", "gemini-2.5-flash"),

    # Short, high-volume, low-stakes classification — "is this a click or a
    # scroll?". The lite model answers these correctly and answers them fast,
    # and this is where most of the latency win lives.
    "fast":   ("gemini-flash-lite-latest", "gemini-flash-latest"),

    # Grounded web answers.
    "search": ("gemini-flash-latest", "gemini-2.5-flash"),

    # The realtime voice session. Not used through call() — main.py hands this
    # straight to client.aio.live.connect().
    "live":   ("models/gemini-2.5-flash-native-audio-preview-12-2025",),
}

DEFAULT_TASK = "text"

# Aliases, so a caller can say what it is doing rather than look up a key.
_ALIASES = {
    "chat": "text", "summary": "text", "summarize": "text", "write": "text",
    "coding": "code", "script": "code", "build": "code", "fix": "code",
    "picture": "image", "img": "image", "render": "image", "draw": "image",
    "see": "vision", "screen": "vision", "screenshot": "vision", "ocr": "vision",
    "classify": "fast", "route": "fast", "intent": "fast", "cheap": "fast",
    "web": "search", "grounded": "search",
    "voice": "live", "audio": "live", "realtime": "live",
}


# ── cached config ───────────────────────────────────────────────────────────
# api_keys.json was being opened and parsed on every API key lookup and every
# model lookup — 52 call sites across the project, several of them per request.
# The file changes only when the user edits settings, so cache on mtime: a
# stat() is roughly two orders of magnitude cheaper than an open+parse, and an
# edit is still picked up on the next call with no restart.
_cfg_cache: dict = {}
_cfg_stamp: tuple[float, int] | None = None
_cfg_lock = threading.Lock()


def config(force: bool = False) -> dict:
    """config/api_keys.json, reparsed only when the file actually changes."""
    global _cfg_cache, _cfg_stamp
    try:
        st = CONFIG_PATH.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return _cfg_cache

    if not force and stamp == _cfg_stamp and _cfg_cache:
        return _cfg_cache

    with _cfg_lock:
        try:
            _cfg_cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _cfg_stamp = stamp
        except Exception:
            pass          # keep whatever we had; a half-written file is transient
    return _cfg_cache


def invalidate() -> None:
    """Force the next config() to re-read. Call after writing the file."""
    global _cfg_stamp
    _cfg_stamp = None


# ── task -> model ───────────────────────────────────────────────────────────

def _normalise(task: str) -> str:
    key = (task or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = _ALIASES.get(key, key)
    return key if key in _REGISTRY else DEFAULT_TASK


def candidates(task: str) -> tuple[str, ...]:
    """Every model to try for `task`, best first. Config overrides win."""
    key = _normalise(task)
    override = (config().get("models") or {}).get(key)
    if isinstance(override, str) and override.strip():
        # Keep the built-ins behind the override so a stale pin still degrades
        # to something that works instead of failing outright.
        rest = tuple(m for m in _REGISTRY[key] if m != override.strip())
        return (override.strip(),) + rest
    if isinstance(override, (list, tuple)):
        picked = tuple(str(m).strip() for m in override if str(m).strip())
        if picked:
            return picked
    return _REGISTRY[key]


def for_task(task: str = DEFAULT_TASK) -> str:
    """The single best model id for `task` — what most call sites want."""
    return candidates(task)[0]


# ── pooled clients ──────────────────────────────────────────────────────────
# genai.Client() builds an HTTP session and reads credentials. Constructing one
# per request threw away connection reuse and added a TLS handshake to every
# call; keying a cache on the API key gives all of it back.
_clients: dict[str, object] = {}
_client_lock = threading.Lock()


def api_keys() -> list[str]:
    """Every configured Gemini key: the primary, then the fallbacks.

    The fallbacks exist because a free-tier key exhausts per-model, not per
    account -- an image request can 429 while text still works fine on the
    same key. call() walks them so a spent quota degrades to the next key
    instead of failing the user's request.
    """
    cfg = config()
    keys = [cfg.get("gemini_api_key", "")]
    keys.extend(cfg.get("gemini_api_key_fallbacks", []) or [])
    seen, out = set(), []
    for k in keys:
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def client(api_key: str = ""):
    """A cached genai.Client for `api_key` (falls back to the configured key)."""
    from google import genai

    key = (api_key or config().get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("No Gemini API key is configured.")
    got = _clients.get(key)
    if got is None:
        with _client_lock:
            got = _clients.get(key)
            if got is None:
                got = genai.Client(api_key=key)
                _clients[key] = got
    return got


# ── the convenience path ────────────────────────────────────────────────────
# A spike is worth waiting out; a 404 or an exhausted allowance is not.
_TRANSIENT = ("503", "UNAVAILABLE", "overloaded", "500", "INTERNAL")
_QUOTA = "429"


def call(task: str, contents, config_overrides: dict | None = None,
         api_key: str = "", retries: int = 1):
    """Run `contents` against the best available model for `task`.

    Walks the candidate list on failure, so a retired model id degrades to the
    next one instead of surfacing as an error. Returns the raw genai response;
    raises RuntimeError only when every candidate has failed.
    """
    keys = [api_key.strip()] if api_key and api_key.strip() else api_keys()
    if not keys:
        raise RuntimeError("No Gemini API key is configured.")
    failures: list[str] = []

    # Model is the outer loop: a better model on a spare key beats a worse
    # model on the primary one.
    for model in candidates(task):
        for key_index, key in enumerate(keys):
            cl = client(key)
            for attempt in range(max(1, retries) + 1):
                try:
                    return cl.models.generate_content(
                        model=model, contents=contents,
                        config=config_overrides or None,
                    )
                except Exception as exc:
                    text = str(exc)
                    # An exhausted quota is a property of the key, so move to
                    # the next key rather than sleeping and retrying this one.
                    if _QUOTA in text or "RESOURCE_EXHAUSTED" in text:
                        failures.append(f"{model}/key{key_index + 1}: quota")
                        break
                    if any(t in text for t in _TRANSIENT) and attempt < retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    failures.append(
                        f"{model}/key{key_index + 1}: {text.splitlines()[0][:90]}"
                    )
                    break

    raise RuntimeError(
        f"Every model for task '{_normalise(task)}' failed — " + "; ".join(failures)
    )


def describe() -> str:
    """A human-readable dump of the live routing table (for status/debug)."""
    rows = [f"  {k:<7} -> {', '.join(candidates(k))}" for k in _REGISTRY]
    return "Model routing:\n" + "\n".join(rows)
