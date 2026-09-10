"""
BackgroundMonitor — user-configured topic watching.
Checks DDG news once per day per topic; alerts JARVIS when a new headline appears.
No crypto, no finance, no uninvited tracking.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


# ── Blocked categories (never monitor regardless of what user says) ────────────

_BLOCKED = {
    # Marka / varlık adları — her dilde aynı yazılır
    "bitcoin", "ethereum", "dogecoin", "solana", "binance",
    "nft", "blockchain", "defi", "altcoin", "memecoin", "coin", "token",
    # "kripto" kökünün farklı dillerdeki yazılışları
    "crypto", "kripto", "cripto", "krypto", "крипто", "仮想通貨", "暗号資産",
    "cryptocurrency",
}

def _is_blocked(topic: str) -> bool:
    t = topic.lower()
    return any(word in t for word in _BLOCKED)


# ── Slug / hash helpers ────────────────────────────────────────────────────────

def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")

def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Memory I/O ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("monitors", {})
    return data if isinstance(data, dict) else {}

def _save(monitors: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    memory = load_memory()
    memory["monitors"] = monitors
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def add_monitor(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return "Please specify a topic to monitor."
    if _is_blocked(topic):
        return "I don't monitor crypto or financial topics."
    monitors = _load()
    slug = _slug(topic)
    if slug in monitors:
        return f"Already monitoring: {monitors[slug]['topic']}"
    monitors[slug] = {
        "topic":      topic,
        "added":      datetime.now().strftime("%Y-%m-%d"),
        "last_check": "",
        "last_hash":  "",
    }
    _save(monitors)
    print(f"[Monitor] ➕ Added: {topic}")
    return f"Now monitoring: {topic}"


def remove_monitor(topic: str) -> str:
    topic = topic.strip().lower()
    monitors = _load()
    # exact slug match first
    slug = _slug(topic)
    if slug in monitors:
        label = monitors.pop(slug)["topic"]
        _save(monitors)
        return f"Stopped monitoring: {label}"
    # partial match fallback
    for key, val in list(monitors.items()):
        if topic in val.get("topic", "").lower():
            label = monitors.pop(key)["topic"]
            _save(monitors)
            return f"Stopped monitoring: {label}"
    return f"Not found in monitored topics: {topic}"


def list_monitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def check_all() -> list[str]:
    """
    Run all pending topic checks (once per day per topic).
    Returns a list of [MONITOR_ALERT] strings — empty if nothing new.
    """
    from actions.web_search import _ddg_news

    monitors = _load()
    if not monitors:
        return []

    today   = datetime.now().strftime("%Y-%m-%d")
    alerts  = []
    changed = False

    for slug, data in monitors.items():
        if data.get("last_check") == today:
            continue                     # already checked today

        topic = data.get("topic", slug)
        try:
            results = _ddg_news(topic, max_results=5)
            if not results:
                monitors[slug]["last_check"] = today
                changed = True
                continue

            top   = results[0]
            title = top.get("title", "").strip()
            if not title:
                continue

            h = _title_hash(title)
            monitors[slug]["last_check"] = today
            changed = True

            if h == data.get("last_hash"):
                continue                 # same headline as last check — no alert

            monitors[slug]["last_hash"] = h

            snippet = top.get("snippet", "")[:150]
            source  = top.get("source", "")

            # A changed headline hash says something happened; it does not say
            # what, or whether it matters. Hermes reads around the story and
            # writes the briefing -- but only now, once the cheap check has
            # already proven there is news. Running it on every topic every day
            # would spend the budget almost entirely on "nothing has changed".
            briefing = _hermes_briefing(topic, title)
            if briefing:
                alerts.append(f"[MONITOR_ALERT] {topic}\n{briefing}")
                print(f"[Monitor] 🔔 Hermes briefing for '{topic}'")
            else:
                parts = [f"[MONITOR_ALERT] {topic}", f"Headline: {title}"]
                if snippet:
                    parts.append(snippet)
                if source:
                    parts.append(f"Source: {source}")
                alerts.append("\n".join(parts))
                print(f"[Monitor] 🔔 New headline for '{topic}': {title[:60]}")

        except Exception as e:
            print(f"[Monitor] ⚠️ Check failed for '{topic}': {e}")

    if changed:
        _save(monitors)

    return alerts


def _hermes_briefing(topic: str, headline: str) -> str:
    """Ask Hermes to explain what actually happened, or "" to fall back.

    Every failure path here is deliberately silent and returns "": Hermes being
    absent, rate-limited or broken must never cost the user their alert. The
    headline-and-snippet version is a worse briefing, not a missing one.
    """
    try:
        from actions import hermes_agent
    except Exception:
        return ""
    if not hermes_agent.installed():
        return ""
    allowed, _why = hermes_agent.budget_check()
    if not allowed:
        return ""
    try:
        ok, text = hermes_agent.ask(
            f"News broke about {topic!r}. The top headline is: {headline!r}. "
            "Check what actually happened, then tell me in two or three "
            "sentences what is new and why it matters to someone following "
            "this. If the headline turns out to be routine or a rehash of old "
            "news, say exactly that instead.",
            max_turns=6,
        )
    except Exception:
        return ""
    return text.strip() if ok and text and text.strip() else ""
