"""The agent roster the orb shows when you ask to pull up agents.

Jarvis has a lot of capabilities, but only a handful are *agents* in the sense
that matters here: you hand one a job, it goes away and works, and it comes
back with something. Those are worth a card with a button. The rest -- setting
a timer, opening an app -- are commands, and putting them here would turn a
short launcher into a menu of everything.

Every import is done inside the runner rather than at module scope. This is
loaded while the UI is drawing, and pulling Blender, face recognition and the
Hermes bridge into memory to render six lines of text would stall the orb on a
cold start.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    key: str
    name: str
    description: str          # one short line — the panel has ~34 characters
    prompt: str | None = None  # what to ask for when the agent needs input
    detail: str = ""          # a longer line, spoken rather than drawn


def catalogue() -> list[Agent]:
    """The agents, in the order they appear on screen."""
    return [
        Agent(
            key="plan",
            name="PLANNER",
            description="Work a goal in steps",
            prompt="What should I work through?",
            detail=("Runs a goal across several of my own tools, one step at a "
                    "time, deciding each move from what the last one found. "
                    "Local and free."),
        ),
        Agent(
            key="hermes",
            name="HERMES",
            description="Deep research, many steps",
            prompt="What should Hermes look into?",
            detail=("A research agent that works in several steps — searching, "
                    "reading and cross-checking. Rate limited so it cannot "
                    "spend the whole quota."),
        ),
        Agent(
            key="blender",
            name="BUILDER",
            description="Model something in Blender",
            prompt="What should I build?",
            detail="Builds a 3D model in Blender from a spoken description.",
        ),
        Agent(
            key="scan",
            name="FACE SCAN",
            description="Identify who is nearby",
            detail="Looks through the camera and tells you who it recognises.",
        ),
        Agent(
            key="monitor",
            name="WATCH",
            description="Topics being followed",
            detail=("The topics being watched in the background. New "
                    "developments are briefed by Hermes."),
        ),
        Agent(
            key="dev",
            name="DEV",
            description="Write and run code",
            prompt="What should the dev agent do?",
            detail="Writes, runs and fixes code in a working directory.",
        ),
        Agent(
            key="status",
            name="SYSTEM",
            description="Machine health right now",
            detail="Battery, memory, disk and network at a glance.",
        ),
    ]


def get(key: str) -> Agent | None:
    key = (key or "").strip().lower()
    for a in catalogue():
        if a.key == key:
            return a
    return None


def summary() -> str:
    """A spoken list, for when the user asks rather than looks."""
    return "; ".join(f"{a.name.title()} — {a.description.lower()}"
                     for a in catalogue())


def run(key: str, text: str = "", player=None) -> str:
    """Run one agent. Returns what to say. Never raises."""
    agent = get(key)
    if agent is None:
        return f"There is no agent called {key!r}."

    text = (text or "").strip()
    if agent.prompt and not text:
        return agent.prompt

    try:
        if agent.key == "plan":
            # Deliberately not run here: the harness dispatches Jarvis's tools
            # through the live session object, which only the voice loop owns.
            # Returning the goal lets the loop call run_plan with it.
            return f"Working through: {text}"
        if agent.key == "hermes":
            from actions import hermes_agent
            ok, answer = hermes_agent.ask(text, log=_logger(player))
            return answer
        if agent.key == "blender":
            from actions import blender_control
            return blender_control.blender_control(
                {"action": "create", "description": text}, player)
        if agent.key == "scan":
            from actions import face_id
            return face_id.scan(player)
        if agent.key == "monitor":
            from actions import background_monitor
            topics = background_monitor.list_monitors()
            if not topics:
                return "Nothing is being watched. Say 'monitor' and a topic to start."
            return "Watching: " + ", ".join(topics) + "."
        if agent.key == "dev":
            from actions import dev_agent
            return dev_agent.dev_agent({"task": text}, player)
        if agent.key == "status":
            from actions import system_monitor
            st = system_monitor.get_system_status()
            if isinstance(st, dict):
                bits = [f"{k} {v}" for k, v in list(st.items())[:5]]
                return "; ".join(bits)
            return str(st)
    except Exception as exc:
        return f"{agent.name.title()} could not run: {exc}"
    return f"{agent.name.title()} has nothing to do."


def _logger(player):
    if player is None:
        return None
    def log(msg):
        try:
            player.write_log(f"Jarvis: {msg}")
        except Exception:
            pass
    return log
