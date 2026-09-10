"""Place a real phone call and let Jarvis hold the conversation.

HOW THE CALL ACTUALLY HAPPENS
-----------------------------
There is no software path on macOS or iOS that lets a program speak into a
live phone call. CallKit does not expose the audio stream, and no virtual
audio device can reach it. Every AI phone product works around this by
originating the call on a telephony provider's infrastructure instead.

That route is closed here for a specific reason: Saudi Arabia's CST requires
all PSTN interconnect to run through a licensed operator (STC, Mobily, Zain),
and presenting a +966 number over a foreign trunk is bypass routing, which is
prohibited and enforced. So the call is not originated in software at all.

Instead the iPhone places an ordinary cellular call on the user's own SIM, and
the audio is bridged to this machine through a physical audio interface:

    Mac output  ──► iPhone microphone input   (Jarvis speaks)
    iPhone earpiece ──► Mac input             (Jarvis listens)

That inversion is the whole trick, and it buys three things nothing else can:
the caller ID is genuinely the user's number with no verification to arrange,
the recipient receives a completely normal call, and there is no per-minute
cost beyond the plan already being paid for.

WHAT THIS MODULE OWNS
---------------------
Contact lookup, the confirmation gate, dialling, and the audio routing. It
deliberately owns no speech recognition, no synthesis and no language model:
Jarvis already runs a bidirectional Gemini Live session that does all three,
so the call reuses that rather than building a second, worse copy.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from core import models

BASE = Path(__file__).resolve().parent.parent
CONTACTS_PATH = BASE / "config" / "contacts.json"

# Dialable: + and digits, 7..15 of them, per E.164.
_E164 = re.compile(r"^\+?[0-9]{7,15}$")


# ── configuration ────────────────────────────────────────────────────────────

def _cfg() -> dict:
    return models.config()


def enabled() -> bool:
    return bool(_cfg().get("calling_enabled", False))


def caller_id() -> str:
    """The number the recipient sees. Informational only.

    Nothing here sets the caller ID -- the SIM does. It is stored so Jarvis can
    say which number it is calling from, and so a mismatch is visible rather
    than silent.
    """
    return str(_cfg().get("caller_id", "")).strip()


def confirm_before_call() -> bool:
    return bool(_cfg().get("confirm_before_call", True))


def bridge_devices() -> tuple[str, str]:
    """(input, output) device names for the phone bridge, '' when unset."""
    c = _cfg()
    return (str(c.get("call_bridge_input", "") or "").strip(),
            str(c.get("call_bridge_output", "") or "").strip())


def bridge_ready() -> tuple[bool, str]:
    """Whether the audio bridge is present, and what is missing if not."""
    from core import audio_devices
    in_name, out_name = bridge_devices()
    if not in_name or not out_name:
        return False, ("The audio bridge is not configured. Set "
                       "'call_bridge_input' and 'call_bridge_output' in "
                       "config/api_keys.json to the audio interface connected "
                       "to the phone.")
    if audio_devices.resolve(in_name, "input") is None:
        return False, f"The bridge input {in_name!r} is not plugged in."
    if audio_devices.resolve(out_name, "output") is None:
        return False, f"The bridge output {out_name!r} is not plugged in."
    return True, ""


# ── contacts ─────────────────────────────────────────────────────────────────

def contacts() -> dict:
    """Name -> number, from local config. Numbers never live in source."""
    try:
        raw = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for name, value in (raw or {}).items():
        number = value if isinstance(value, str) else (
            value.get("phone") or value.get("number") or "") if isinstance(value, dict) else ""
        if number:
            out[str(name)] = str(number)
    return out


def resolve_contact(who: str) -> tuple[str, str] | None:
    """(display name, number) for a spoken name or a raw number."""
    who = (who or "").strip()
    if not who:
        return None

    bare = who.replace(" ", "").replace("-", "")
    if _E164.match(bare):
        return (who, bare if bare.startswith("+") else bare)

    book = contacts()
    low = who.lower()
    for name, number in book.items():
        if name.lower() == low:
            return (name, number)
    # A spoken name arrives however the recogniser heard it, so fall back to
    # prefix and containment before giving up.
    for name, number in book.items():
        if name.lower().startswith(low) or low in name.lower():
            return (name, number)
    return None


# ── dialling ─────────────────────────────────────────────────────────────────

def dial(number: str) -> tuple[bool, str]:
    """Ask the phone to place a normal cellular call, via Continuity."""
    number = (number or "").strip()
    if not _E164.match(number.replace(" ", "").replace("-", "")):
        return False, f"{number!r} is not a dialable number."
    try:
        subprocess.run(["open", f"tel://{number}"], check=True,
                       capture_output=True, timeout=15)
    except Exception as exc:
        return False, (f"Could not start the call: {exc}. Continuity calling "
                       "must be on: iPhone Settings > Cellular > Calls on "
                       "Other Devices, with the same Apple ID and Wi-Fi.")
    return True, ""


def hangup() -> str:
    """End whatever call is up. FaceTime owns the call, so ask it to quit."""
    script = 'tell application "FaceTime" to quit'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return "Call ended."
    except Exception as exc:
        return f"Could not end the call: {exc}"


# ── the spoken brief ─────────────────────────────────────────────────────────

def call_instructions(display: str, purpose: str, owner: str = "") -> str:
    """The system prompt for the call session.

    The disclosure in the opening line is not decoration. A synthetic voice
    that lets someone believe they are talking to a person is the difference
    between an assistant and a deception, and several jurisdictions now require
    the disclosure outright.
    """
    owner = owner or "the person I work for"
    return (
        f"You are placing a phone call to {display} on behalf of {owner}. "
        "You are speaking out loud on a real telephone call, so keep every "
        "turn short -- one or two sentences -- and never read out lists, "
        "markdown or anything that only makes sense written down.\n\n"
        "Open the call with exactly this shape: greet them, say you are an AI "
        "assistant calling on behalf of "
        f"{owner}, then say why you are calling. Never imply you are a person. "
        "If you are asked whether you are human, say plainly that you are an "
        "AI assistant.\n\n"
        f"The reason for this call: {purpose}\n\n"
        "Listen properly. Let them finish. If they are busy, or ask to be "
        "called back, or sound like they do not want to talk, apologise, say "
        f"{owner} will follow up, and end the call. If they ask something you "
        "were not briefed on, say you will pass it on rather than guessing. "
        "When the reason for the call is finished, thank them and say goodbye."
    )


# ── the call ─────────────────────────────────────────────────────────────────

def call(who: str, purpose: str = "", player=None, confirm_override: bool | None = None) -> str:
    """Look up, confirm, dial, and hand over to the conversation loop."""
    def say(msg: str) -> None:
        print(f"[Call] {msg}")
        if player is not None:
            try:
                player.write_log(f"Jarvis: {msg}")
            except Exception:
                pass

    if not enabled():
        return ("Calling is switched off. Set 'calling_enabled' to true in "
                "config/api_keys.json to turn it on.")

    found = resolve_contact(who)
    if found is None:
        known = ", ".join(contacts()) or "nobody yet"
        return (f"I do not have a number for {who!r}. I know: {known}. "
                "Add them to config/contacts.json, or give me the number.")
    display, number = found

    wants_confirm = confirm_before_call() if confirm_override is None else confirm_override
    if wants_confirm:
        # Returned as a question rather than dialled. The caller (the voice
        # loop) asks it, and a "yes" comes back as a second call with
        # confirm_override False -- so a misheard name can never ring someone.
        return f"Should I call {display} at {number}?"

    ok, err = dial(number)
    if not ok:
        return err
    say(f"Calling {display} at {number}.")

    ready, why = bridge_ready()
    if not ready:
        return (f"Calling {display} now — you will need to speak, because "
                f"{why}")

    return converse(display, purpose, player)


def converse(display: str, purpose: str, player=None, timeout: float = 300.0) -> str:
    """Run the Gemini Live session over the phone bridge until the call ends."""
    try:
        from actions import call_session
    except Exception as exc:
        return f"The call is connected but I cannot talk on it: {exc}"
    return call_session.run(display, purpose, player, timeout)
