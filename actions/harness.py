"""An agentic harness: give Jarvis a goal and let it work in steps.

The voice loop is single-shot by nature. You ask, it picks one tool, it
answers, the turn ends. That is right for "what's the weather" and wrong for
"find the biggest files in Downloads, tell me which are installers, and bin
the ones older than a month" -- a goal that needs several tools in sequence,
where each step depends on what the last one returned.

This runs that shape: the model is handed the goal and Jarvis's own tools, and
keeps going until it says it is finished or the step budget runs out. It is
deliberately built on the tools that already exist rather than a second set,
so anything Jarvis can do by voice, it can also do inside a plan.

Two limits are hard, because an autonomous loop with none is how a bad
afternoon starts: a step ceiling, and a refusal to touch tools that end the
session or spend money without being asked for by name.
"""

from __future__ import annotations

import asyncio
import json
import time

from core import models

MAX_STEPS = 8
STEP_TIMEOUT = 120.0

# Tools the harness will not call on its own initiative. Shutting Jarvis down
# mid-plan, or placing a phone call because a plan decided one was implied, is
# never what the person meant when they described a goal.
BLOCKED = {
    "shutdown_jarvis", "sleep_mode", "make_call", "hermes",
    "reconfigure", "undo",
}

SYSTEM = (
    "You are Jarvis working through a goal on your own, one step at a time.\n"
    "Call one tool per step. After each result, decide whether the goal is "
    "met. When it is, reply with a short spoken summary and no tool call -- "
    "that is how you finish.\n"
    "Be economical: you have a small number of steps, so do not call a tool "
    "to confirm something you already know. If a step fails, adapt rather "
    "than repeating the same call. If the goal turns out to be impossible or "
    "was based on something untrue, say so plainly and stop instead of "
    "inventing a result.\n"
    "You are speaking the final summary aloud, so keep it to a sentence or "
    "two and never read out paths, lists or code."
)


def _shim(name: str, args: dict):
    """A stand-in for the function-call object _execute_tool expects."""
    class _FC:
        pass
    fc = _FC()
    fc.name = name
    fc.args = args
    return fc


async def run(goal: str, session, max_steps: int = MAX_STEPS, log=None) -> str:
    """Work a goal to completion. `session` is the live JarvisSession."""
    from google import genai
    from google.genai import types as gtypes

    def say(msg: str) -> None:
        print(f"[Harness] {msg}")
        if log:
            try:
                log(msg)
            except Exception:
                pass

    goal = (goal or "").strip()
    if not goal:
        return "There was no goal to work on."

    decls = _tool_declarations(session)
    if not decls:
        return "I could not read my own tool list, so I cannot plan with it."

    keys = models.api_keys()
    if not keys:
        return "There is no API key for the harness to use."

    contents = [gtypes.Content(role="user", parts=[gtypes.Part(text=goal)])]
    steps = max(1, min(int(max_steps or MAX_STEPS), MAX_STEPS))
    done = []

    for step in range(1, steps + 1):
        try:
            # models.call walks the whole candidate chain and every key, so a
            # single model returning 503 "high demand" does not end the plan.
            # Flash models do that often enough that a one-model harness dies
            # on its second step for reasons that have nothing to do with the
            # goal.
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    models.call, "text", contents,
                    {
                        "system_instruction": SYSTEM,
                        "tools": [{"function_declarations": decls}],
                        "temperature": 0.2,
                    },
                ),
                timeout=STEP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return f"Step {step} took too long, so I stopped. {_recap(done)}"
        except Exception as exc:
            return f"The plan stopped at step {step}: {exc}. {_recap(done)}"

        calls = _function_calls(reply)
        if not calls:
            # No tool call means the model considers the goal met; its text is
            # the answer the user actually hears.
            final = (_text_of(reply) or "").strip()
            return final or f"Finished. {_recap(done)}"

        fc = calls[0]
        name = getattr(fc, "name", "") or ""
        args = dict(getattr(fc, "args", {}) or {})

        if name in BLOCKED:
            outcome = (f"{name} is not something I will do inside a plan — "
                       "ask me for it directly.")
            say(outcome)
        else:
            say(f"Step {step}/{steps}: {name}")
            try:
                resp = await asyncio.wait_for(
                    session._execute_tool(_shim(name, args)), timeout=STEP_TIMEOUT)
                outcome = _response_text(resp)
            except asyncio.TimeoutError:
                outcome = f"{name} timed out."
            except Exception as exc:
                outcome = f"{name} failed: {exc}"

        done.append(f"{name}: {outcome[:120]}")
        # Append the model's own content rather than a rebuilt copy. Gemini 3
        # attaches a thought_signature to each function call and rejects the
        # next turn if it is missing, so a hand-made FunctionCall part fails
        # with 400 INVALID_ARGUMENT on the second step -- the first step always
        # looks fine, which is what makes it easy to miss.
        try:
            contents.append(reply.candidates[0].content)
        except Exception:
            contents.append(gtypes.Content(role="model", parts=[
                gtypes.Part(function_call=gtypes.FunctionCall(name=name, args=args))]))
        contents.append(gtypes.Content(role="user", parts=[
            gtypes.Part(function_response=gtypes.FunctionResponse(
                name=name, response={"result": outcome[:4000]}))]))

    return (f"I used all {steps} steps without finishing. {_recap(done)}")


def _tool_declarations(session) -> list:
    """Jarvis's own tool schemas, minus the ones a plan must not call."""
    try:
        import main
        decls = list(getattr(main, "TOOL_DECLARATIONS", []) or [])
    except Exception:
        return []
    try:
        decls += session._plugin_registry.get_tool_declarations()
    except Exception:
        pass
    return [d for d in decls if d.get("name") not in BLOCKED]


def _function_calls(reply) -> list:
    out = []
    try:
        for cand in (reply.candidates or []):
            for part in (cand.content.parts or []):
                if getattr(part, "function_call", None):
                    out.append(part.function_call)
    except Exception:
        pass
    return out


def _text_of(reply) -> str:
    try:
        return reply.text or ""
    except Exception:
        pass
    bits = []
    try:
        for cand in (reply.candidates or []):
            for part in (cand.content.parts or []):
                if getattr(part, "text", None):
                    bits.append(part.text)
    except Exception:
        pass
    return " ".join(bits)


def _response_text(resp) -> str:
    try:
        r = getattr(resp, "response", None)
        if isinstance(r, dict):
            return str(r.get("result", r))[:2000]
        return str(r or resp)[:2000]
    except Exception:
        return "done"


def _recap(done: list) -> str:
    if not done:
        return "Nothing was completed."
    return "Done so far: " + "; ".join(d.split(":")[0] for d in done) + "."
