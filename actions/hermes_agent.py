"""Hermes — the long-horizon worker Jarvis hands slow jobs to.

Jarvis is a voice loop: you ask, it answers, and the whole exchange is over in
a few seconds. That shape is wrong for work that needs twenty steps of
searching, reading and cross-checking. Hermes is an agent built for exactly
that, so Jarvis stays the coordinator -- it hears the request, decides this is
a research job rather than a command, hands it over, and speaks the result.

It runs as a subprocess in its own virtualenv. Hermes wants a different openai
and httpx than Jarvis does, and importing it in-process would eventually break
the voice loop over a dependency nobody chose. A subprocess cannot.

Cost is the real risk. An agent that decides its own next step can spend a
whole day's quota on one badly-phrased question, so every call is bounded
before it starts (turns per call) and the day is bounded across calls (calls
per hour and per day). Both are checked before spending, never after.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core import models

BASE = Path(__file__).resolve().parent.parent
HERMES_DIR = BASE / "vendor" / "hermes"
HERMES_PY = BASE / "vendor" / "hermes-venv" / "bin" / "python"
STATE = Path.home() / "Documents" / "Jarvis Blender" / ".hermes_usage.json"

# Gemini speaks OpenAI's dialect at this address, which is the whole reason
# Hermes can run on the keys Jarvis already has instead of a second paid
# account. Hermes only ever sees an OpenAI-shaped endpoint.
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-3.6-flash"

# Budgets. An agent picks its own next step, so a vague question can otherwise
# run until the quota is gone; these are deliberately tight and can be raised
# in config/api_keys.json under "hermes_limits".
LIMITS = {
    "turns_per_call": 12,     # hard ceiling on API round-trips in one job
    "calls_per_hour": 6,
    "calls_per_day": 30,
    "cooldown_seconds": 20,   # stops a stuck loop hammering the API
    "timeout_seconds": 300,
}

# No terminal, no process control. Hermes is being handed questions from a
# voice loop, where a misheard word becomes a shell command nobody typed.
SAFE_TOOLSETS = "safe"
BLOCKED_TOOLSETS = "terminal,process_manage"


def limits() -> dict:
    merged = dict(LIMITS)
    override = models.config().get("hermes_limits")
    if isinstance(override, dict):
        for k, v in override.items():
            if k in merged and isinstance(v, (int, float)):
                merged[k] = type(merged[k])(v)
    return merged


def installed() -> bool:
    return HERMES_PY.exists() and (HERMES_DIR / "run_agent.py").exists()


def _load_usage() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"calls": []}


def _save_usage(usage: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(usage), encoding="utf-8")
    except Exception:
        pass


def _recent(usage: dict, seconds: float) -> int:
    cutoff = time.time() - seconds
    return sum(1 for t in usage.get("calls", []) if t >= cutoff)


def budget_check() -> tuple[bool, str]:
    """Whether a call is allowed right now, and why not when it is not."""
    lim = limits()
    usage = _load_usage()
    calls = usage.get("calls", [])

    if calls:
        since = time.time() - max(calls)
        if since < lim["cooldown_seconds"]:
            wait = int(lim["cooldown_seconds"] - since) + 1
            return False, f"Hermes just ran — give it {wait} more seconds."

    hour = _recent(usage, 3600)
    if hour >= lim["calls_per_hour"]:
        return False, (f"Hermes has run {hour} times this hour, which is its "
                       f"limit of {lim['calls_per_hour']}. It will free up shortly.")

    day = _recent(usage, 86400)
    if day >= lim["calls_per_day"]:
        return False, (f"Hermes has used its {lim['calls_per_day']} runs for today. "
                       "That cap is there so it cannot spend the whole quota.")
    return True, ""


def _record_call() -> None:
    usage = _load_usage()
    calls = [t for t in usage.get("calls", []) if t >= time.time() - 86400]
    calls.append(time.time())
    usage["calls"] = calls
    _save_usage(usage)


def usage_summary() -> str:
    lim = limits()
    usage = _load_usage()
    return (f"Hermes has run {_recent(usage, 3600)} of {lim['calls_per_hour']} times "
            f"this hour and {_recent(usage, 86400)} of {lim['calls_per_day']} today, "
            f"with up to {lim['turns_per_call']} steps per job.")


def ask(query: str, max_turns: int | None = None, toolsets: str = SAFE_TOOLSETS,
        log=None) -> tuple[bool, str]:
    """Hand one job to Hermes. Returns (ok, text). Never raises."""
    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    query = (query or "").strip()
    if not query:
        return False, "There was nothing to ask Hermes."
    if not installed():
        return False, ("Hermes is not installed. Its files belong in "
                       "vendor/hermes with a virtualenv at vendor/hermes-venv.")

    allowed, why = budget_check()
    if not allowed:
        return False, why

    keys = models.api_keys()
    if not keys:
        return False, "There is no API key for Hermes to use."

    lim = limits()
    turns = int(max_turns or lim["turns_per_call"])
    turns = max(1, min(turns, lim["turns_per_call"]))   # never above the cap

    runner = (
        "import sys; sys.path.insert(0, '.')\n"
        "import run_agent\n"
        "run_agent.main(query=sys.argv[1], model=sys.argv[2], api_key=sys.argv[3],\n"
        "               base_url=sys.argv[4], max_turns=int(sys.argv[5]),\n"
        "               enabled_toolsets=sys.argv[6], disabled_toolsets=sys.argv[7])\n"
    )
    env = dict(os.environ)
    # Keep Hermes' own state out of the user's home, and stop it inheriting an
    # OPENAI_API_KEY that belongs to something else.
    env["HERMES_HOME"] = str(BASE / "vendor" / ".hermes_home")
    env["OPENAI_API_KEY"] = keys[0]
    env["OPENAI_BASE_URL"] = GEMINI_OPENAI_BASE

    say(f"Handing this to Hermes (up to {turns} steps).")
    _record_call()          # spend the budget before the call, not after, so a
                            # crash mid-run cannot become a free retry loop
    try:
        proc = subprocess.run(
            [str(HERMES_PY), "-c", runner, query, DEFAULT_MODEL, keys[0],
             GEMINI_OPENAI_BASE, str(turns), toolsets, BLOCKED_TOOLSETS],
            cwd=str(HERMES_DIR), env=env, capture_output=True, text=True,
            timeout=lim["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        return False, (f"Hermes ran past its {lim['timeout_seconds']} second limit "
                       "and was stopped.")
    except Exception as exc:
        return False, f"Hermes could not start: {exc}"

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip().splitlines()
        return False, f"Hermes failed: {err[-1] if err else 'no output'}"
    return True, _final_answer(out)


def _final_answer(output: str) -> str:
    """Pull the answer out of Hermes' progress log.

    The runner narrates its whole trajectory -- banners, token counts, per-call
    timings -- and ends with a clearly marked final response. Speaking the
    trajectory aloud would be unbearable, so take the marked section and fall
    back to filtering only if the format ever changes.
    """
    lines = [ln.rstrip() for ln in output.splitlines()]

    for i in range(len(lines) - 1, -1, -1):
        if "FINAL RESPONSE" in lines[i]:
            tail = []
            for ln in lines[i + 1:]:
                s = ln.strip()
                if not s or set(s) <= set("-=_"):
                    continue
                if s.startswith(("👋", "🎉", "📋", "=" * 3)):
                    break
                tail.append(s)
            answer = " ".join(tail).strip()
            if answer:
                return answer

    # Fallback: drop the decorated progress lines and keep the prose.
    keep = []
    for ln in lines:
        s = ln.strip()
        if not s or set(s) <= set("-=_"):
            continue
        if s[0] in "\U0001F300\u2699\u23F1\u2705\u274C[" or s.startswith(
                ("Iteration", "Tool ", "Calling ", "DEBUG", "INFO", "WARNING")):
            continue
        keep.append(s)
    text = " ".join(keep[-10:]).strip()
    return text or "Hermes finished without saying anything."
