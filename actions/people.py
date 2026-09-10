"""Personal profiles for recognised faces.

`config/faces.json` knows what a face looks like; `memory/long_term.json`
knows who that is. This module joins the two and decides what gets said out
loud versus what gets offered.

Two problems sit between the stores. The profile facts are spread across
memory categories with no grouping -- "mohamed_courses" lives under
relationships, "mohamed_age" under identity, "kareem_favorite_color" under
preferences -- so a person's details have to be gathered by key prefix rather
than read from one place. And the two stores spell people differently: the
face store has "Muhammad" and "Masa" while memory has "mohamed_*",
"muhammad_*" and "meisa_*". Matching is therefore fuzzy and deliberately
generous, because the cost of missing a match (saying nothing about someone
you know) is far worse than the cost of a loose one.

What comes back is ordered by how much it identifies the person:

    tier 1  name, then age, then what they do (job, employer, school)
            -- always spoken
    tier 2  everything else -- never spoken unprompted, offered as a
            follow-up question instead

so a scan leads with who someone is rather than burying it behind their
favourite colour.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()

# ── what gets said, and in what order ───────────────────────────────────────
# Tier 1 is spoken on every scan, in this order. Several spellings map to the
# same slot because the memory keys were written by hand over time.
AGE_KEYS = ("age",)
# Ordered, and each with the phrasing that reads as speech. "university:
# Al-Faris University" is how a database prints; "studies at Al-Faris
# University" is how a person says it.
DOING_KEYS: tuple[tuple[str, str], ...] = (
    ("occupation", "{v}"),
    ("job", "{v}"),
    ("profession", "{v}"),
    ("employment", "works at {v}"),
    ("employer", "works at {v}"),
    ("company", "works at {v}"),
    ("work", "works at {v}"),
    ("employment_status", "{v}"),
    ("previous_employer", "previously at {v}"),
    ("school", "goes to {v}"),
    ("university", "studies at {v}"),
    ("college", "studies at {v}"),
    ("grade", "in {v}"),
    ("studies", "studies {v}"),
    ("study", "studies {v}"),
)

# Tier 2 phrasing. A fact that is a list of things ("courses", "hobbies") is
# offered as "would you like to hear ...", because you hear a list. A single
# general fact is offered as "would you like more information about ...".
_LIST_LIKE = (
    "courses", "classes", "subjects", "hobbies", "interests", "watching",
    "shows", "movies", "music", "sports", "languages",
)

# How a key reads when spoken. Anything absent is de-slugged automatically.
_LABELS = {
    "age": "age",
    "last_name": "surname",
    "birthday": "birthday",
    "employment": "employer",
    "employment_status": "employment status",
    "previous_employer": "previous employer",
    "school": "school",
    "university": "university",
    "courses": "courses",
    "marital_status": "marital status",
    "favorite_color": "favourite colour",
    "favourite_color": "favourite colour",
    "watching": "what they are watching",
    "favorite_tv_show": "favourite show",
    "hobbies": "hobbies",
    "children": "children",
    "mother": "mother",
}

# Verbs that make a tier-2 offer read naturally: "the courses Mohammad takes"
# rather than "the courses Mohammad".
_VERBS = {
    "courses": "takes",
    "classes": "takes",
    "subjects": "takes",
    "hobbies": "has",
    "watching": "is watching",
    "shows": "watches",
    "movies": "watches",
    "music": "listens to",
    "sports": "plays",
    "children": "has",
    "pets": "has",
}


def _norm(name: str) -> str:
    """Lowercase, letters only -- the form names are compared in."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _phonetic(name: str) -> str:
    """A crude spelling-insensitive key.

    Enough to fold Muhammad/Mohamed/Mohammed/Mohammad onto one another, and
    Masa/Meisa/Maisa: drop vowels after the first letter, collapse doubles,
    and treat the interchangeable consonants as one. Not Soundex -- Soundex
    keeps the first letter and a fixed length, which splits Muhammad from
    Mohamed on the vowel that follows the M.
    """
    s = _norm(name)
    if not s:
        return ""
    head, tail = s[0], s[1:]
    tail = re.sub(r"[aeiouyh]", "", tail)
    s = head + tail
    s = s.replace("ph", "f").replace("ck", "k").replace("z", "s")
    return re.sub(r"(.)\1+", r"\1", s)


def _load_memory() -> dict:
    try:
        from memory.memory_manager import load_memory
        return load_memory() or {}
    except Exception:
        try:
            import json
            return json.loads(
                (BASE_DIR / "memory" / "long_term.json").read_text(encoding="utf-8")
            )
        except Exception:
            return {}


def _entry_value(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("value", "")).strip()
    return str(entry or "").strip()


def facts_for(name: str, memory: dict | None = None) -> dict[str, str]:
    """Every stored fact about `name`, as {attribute: value}.

    Keys are matched on the person prefix, so "mohamed_courses" and
    "muhammad_age" both land under Muhammad even though only one of them is
    spelled the way the face store spells him.
    """
    if not name:
        return {}
    mem = memory if memory is not None else _load_memory()
    want, want_ph = _norm(name), _phonetic(name)
    if not want:
        return {}

    out: dict[str, str] = {}
    for category in mem.values():
        if not isinstance(category, dict):
            continue
        for key, entry in category.items():
            if "_" not in key:
                continue
            person, _, attr = key.partition("_")
            pn = _norm(person)
            if not pn or not attr:
                continue
            # Exact, then prefix (Mo -> Mohamed), then phonetic.
            hit = (
                pn == want
                or (len(pn) >= 3 and len(want) >= 3
                    and (pn.startswith(want) or want.startswith(pn)))
                or (want_ph and _phonetic(person) == want_ph)
            )
            if not hit:
                continue
            value = _entry_value(entry)
            # A fact recorded as "None" is an answer ("no school"), but it is
            # not worth announcing, and it must not win a slot over a real
            # value stored under a different spelling of the same person.
            if not value or value.lower() in ("none", "n/a", "unknown", "-"):
                continue
            out.setdefault(attr.lower(), value)
    return out


def _label(attr: str) -> str:
    return _LABELS.get(attr, attr.replace("_", " "))


# Where the generic templates read badly, say it properly instead.
_PHRASES = {
    "watching": "Would you like to hear what {name} is watching?",
    "hobbies": "Would you like to hear what {name} does for fun?",
    "birthday": "Would you like to know when {name}'s birthday is?",
}


def _offer(attr: str, name: str, value: str = "") -> str:
    """The follow-up question for one tier-2 fact.

    A fact you *hear* -- a list of things -- gets "would you like to hear";
    a single general fact gets "would you like more information about". The
    list test looks at the value as well as the name, since a comma-separated
    value is a list whatever its key is called.
    """
    special = _PHRASES.get(attr)
    if special:
        return special.format(name=name)

    label = _label(attr)
    listy = attr in _LIST_LIKE or "," in value
    if listy:
        verb = _VERBS.get(attr)
        subject = f"the {label} {name} {verb}" if verb else f"{name}'s {label}"
        return f"Would you like to hear {subject}?"
    return f"Would you like more information about {name}'s {label}?"


def summary(name: str, memory: dict | None = None) -> tuple[str, str, list[str]]:
    """What to say about `name`.

    Returns (spoken, follow_up, remaining_attrs):
        spoken       tier 1 -- name, age, what they do. Always said.
        follow_up    one question offering the most interesting tier-2 fact.
        remaining    the other tier-2 attributes, for a second question.
    """
    facts = facts_for(name, memory)
    if not facts:
        return name, "", []

    # Prefer a recorded surname over the face-store label alone.
    full = name
    surname = facts.get("last_name") or facts.get("surname")
    if surname and _norm(surname) not in _norm(name):
        full = f"{name} {surname}"

    bits = [full]
    if any(k in facts for k in AGE_KEYS):
        age = next(facts[k] for k in AGE_KEYS if k in facts)
        bits.append(f"{age} years old")

    used = {"last_name", "surname", *AGE_KEYS}
    doing: list[str] = []
    for key, template in DOING_KEYS:
        if key in facts and key not in used:
            used.add(key)
            doing.append(template.format(v=facts[key]))
    if doing:
        bits.append(", ".join(doing))

    spoken = ", ".join(bits)

    rest = [a for a in facts if a not in used]
    # Offer the list-like facts first -- they carry the most to talk about.
    rest.sort(key=lambda a: (not (a in _LIST_LIKE or "," in facts[a]), a))
    follow_up = _offer(rest[0], name, facts[rest[0]]) if rest else ""
    return spoken, follow_up, rest


def details(name: str, attr: str, memory: dict | None = None) -> str:
    """Answer a follow-up: the stored value for one attribute."""
    facts = facts_for(name, memory)
    if not facts:
        return f"I don't have anything saved about {name}."
    want = _norm(attr)
    for key, value in facts.items():
        if _norm(key) == want or want in _norm(key) or _norm(key) in want:
            return f"{name}'s {_label(key)}: {value}."
    return f"I don't have {attr} saved for {name}."


def known_people(memory: dict | None = None) -> list[str]:
    """Every person memory holds facts about."""
    mem = memory if memory is not None else _load_memory()
    seen: dict[str, str] = {}
    for category in mem.values():
        if not isinstance(category, dict):
            continue
        for key in category:
            person, _, attr = key.partition("_")
            if attr and _norm(person):
                seen.setdefault(_phonetic(person), person.title())
    return sorted(seen.values())
