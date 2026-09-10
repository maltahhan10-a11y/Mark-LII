"""Blender integration for MARK LII — open it, and build what was asked for.

The model does not write raw bpy. It writes against `blender_bridge/jarvis_lib.py`,
a small builder API that pins down the parts of Blender's Python that move
between releases and bakes in the things that make a build look made rather
than assembled from default primitives. The API reference in the prompt is
generated from that file's own source, so the two cannot drift apart.

Flow for "build me a rocket":

    1. Launch Blender (GUI) with the bridge script, unless it is already up.
    2. Ask Gemini for a build script written against jarvis_lib.
    3. Execute it inside Blender over the loopback bridge.
    4. On a traceback, hand the error back to the model and let it fix its own
       code, up to MAX_ATTEMPTS times.
    5. Frame the camera and the viewport, and report what was built.

Generated code runs with the full privileges of the Blender process. It is
screened for the obviously destructive calls below, which catches a model
reaching for the filesystem by mistake; it is not a sandbox, and a determined
prompt injection would get through. Treat it the way you would treat running a
downloaded .blend with scripting enabled.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import time
from pathlib import Path

from core import models

BASE_DIR = Path(__file__).resolve().parent.parent
BRIDGE_DIR = BASE_DIR / "blender_bridge"
BOOT_SCRIPT = BRIDGE_DIR / "jarvis_boot.py"
LIB_SCRIPT = BRIDGE_DIR / "jarvis_lib.py"
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROJECT_DIR = Path.home() / "Documents" / "Jarvis Blender"

DEFAULT_PORT = 8787
LAUNCH_TIMEOUT = 90.0        # Blender's first cold start is not quick
CALL_TIMEOUT = 240.0
MAX_ATTEMPTS = 3

# The code-generation candidates now come from the central registry rather
# than a list pinned here, so changing the model that writes build scripts is
# one config edit instead of a code change. Ordering and the "no Pro models"
# rule (they answer 404 or 429 on a standard key, and burning two failed calls
# before reaching a working model cost twenty seconds on every build) live in
# core/models.py.

# A spike is worth waiting out; a 404 or an exhausted daily allowance is not.
_TRANSIENT = ("503", "UNAVAILABLE", "overloaded", "500", "INTERNAL")

# Modules and calls a modelling script has no business reaching for. Blocking
# them stops a model that has wandered off from touching the filesystem or the
# network; it is a guard rail, not a sandbox.
_BANNED_MODULES = frozenset({
    "os", "sys", "shutil", "subprocess", "socket", "urllib", "requests",
    "httpx", "pathlib", "glob", "tempfile", "pickle", "ctypes", "importlib",
    "webbrowser", "http", "ftplib", "smtplib",
})
_BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "open", "__import__", "input", "breakpoint",
})
_BANNED_ATTRS = frozenset({
    "bpy.ops.wm.quit_blender", "bpy.ops.wm.read_factory_settings",
})


def _config() -> dict:
    return models.config()


def CODE_MODELS() -> tuple[str, ...]:
    """The build-script models to try, best first. Resolved per call so a
    config edit lands without a restart."""
    return models.candidates("code")


def _port() -> int:
    try:
        return int(_config().get("blender_port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


# ── locating Blender ────────────────────────────────────────────────────────

def find_blender() -> str | None:
    """The Blender executable, or None. `blender_path` in config wins."""
    configured = (_config().get("blender_path") or "").strip()
    if configured and Path(configured).exists():
        return configured

    system = platform.system()
    candidates: list[Path] = []
    if system == "Darwin":
        candidates += [
            Path("/Applications/Blender.app/Contents/MacOS/Blender"),
            Path.home() / "Applications/Blender.app/Contents/MacOS/Blender",
        ]
        candidates += sorted(
            Path("/Applications").glob("Blender*.app/Contents/MacOS/Blender"),
            reverse=True,
        )
    elif system == "Windows":
        for root in (r"C:\Program Files\Blender Foundation",
                     r"C:\Program Files (x86)\Blender Foundation"):
            candidates += sorted(Path(root).glob("Blender*/blender.exe"), reverse=True)
    else:
        candidates += [Path("/usr/bin/blender"), Path("/usr/local/bin/blender"),
                       Path("/snap/bin/blender")]

    for path in candidates:
        if path.exists():
            return str(path)

    from shutil import which
    return which("blender")


# ── the bridge ──────────────────────────────────────────────────────────────

def _call(payload: dict, timeout: float = CALL_TIMEOUT) -> dict:
    """One request/response against the in-Blender server."""
    try:
        conn = socket.create_connection(("127.0.0.1", _port()), timeout=5.0)
    except OSError as exc:
        return {"ok": False, "output": "", "error": f"not connected: {exc}",
                "offline": True}
    try:
        conn.settimeout(timeout)
        conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return {"ok": False, "output": "", "error": "Blender closed the connection."}
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except socket.timeout:
        return {"ok": False, "output": "", "error": "Blender did not answer in time."}
    except Exception as exc:
        return {"ok": False, "output": "", "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def is_running() -> bool:
    return _call({"command": "ping"}, timeout=4.0).get("ok", False)


def launch(wait: float = LAUNCH_TIMEOUT) -> tuple[bool, str]:
    """Start Blender with the bridge, or confirm one is already listening."""
    if is_running():
        return True, "Blender is already open."

    exe = find_blender()
    if not exe:
        return False, (
            "I can't find Blender on this machine. Install it from blender.org, "
            "or set \"blender_path\" in config/api_keys.json to its executable."
        )
    if not BOOT_SCRIPT.exists():
        return False, f"The Blender bridge script is missing: {BOOT_SCRIPT}"

    env = dict(os.environ)
    env["JARVIS_BLENDER_PORT"] = str(_port())
    try:
        subprocess.Popen(
            [exe, "--python", str(BOOT_SCRIPT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return False, f"Could not start Blender: {exc}"

    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running():
            return True, "Blender is open."
        time.sleep(0.5)
    return False, (
        "Blender was launched but its bridge never came up. It may still be "
        "starting, or another program is using the bridge port."
    )


def run_code(code: str, timeout: float = CALL_TIMEOUT) -> dict:
    """Execute a build script inside Blender."""
    blocked = _screen(code)
    if blocked:
        if blocked.startswith("invalid Python"):
            # Truncated or malformed output — a fact to feed back, not a refusal.
            message = f"The script did not parse: {blocked[15:]}"
        else:
            message = (f"Refused: this script uses {blocked}, which a "
                       f"modelling script has no reason to do.")
        return {"ok": False, "output": "", "error": message}
    return _call({"command": "run", "code": code}, timeout=timeout)


def _dotted(node) -> str:
    """The full dotted path of an attribute chain, e.g. bpy.ops.wm.save."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _screen(code: str) -> str | None:
    """Reject a script on structure, not on the words it happens to contain.

    Matching text was worse than useless here: a desk lamp has a bulb socket,
    so a variable honestly named `socket` got the whole build refused. Reading
    the syntax tree asks the question that actually matters — does this script
    *import* something dangerous or *call* it — and an ordinary identifier
    that shares a name with a module sails through.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"invalid Python: {exc.msg} (line {exc.lineno})"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BANNED_MODULES:
                    return f"the {root} module"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED_MODULES:
                return f"the {root} module"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
                return f"{node.func.id}()"
            if isinstance(node.func, ast.Attribute):
                path = _dotted(node.func)
                if path in _BANNED_ATTRS:
                    return path
                root = path.split(".")[0]
                if root in _BANNED_MODULES:
                    return path
    return None


# ── the builder prompt ──────────────────────────────────────────────────────

def api_reference() -> str:
    """Signatures and one-line summaries, read out of jarvis_lib's own source.

    Generated rather than written by hand so the prompt cannot describe an API
    the library no longer has — the failure mode that makes generated code
    fail on its first line.
    """
    try:
        tree = ast.parse(LIB_SCRIPT.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"(API reference unavailable: {exc})"

    exported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    exported = [
                        el.value for el in node.value.elts
                        if isinstance(el, ast.Constant)
                    ]

    lines: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in exported:
            continue
        doc = (ast.get_docstring(node) or "").strip().split("\n")[0]
        try:
            sig = ast.unparse(node.args)
        except Exception:
            sig = "..."
        lines.append(f"  {node.name}({sig})" + (f"\n      # {doc}" if doc else ""))
    return "\n".join(lines)


_SYSTEM_PROMPT = """You write Blender build scripts for a voice assistant.

Output ONLY Python. No markdown fences, no prose, no explanation. The script
runs inside Blender {version} with these names already imported and no import
statements needed: bpy, math, and the builder API below.

THE BUILDER API (use this; do not hand-roll bpy operator calls):
{api}

Angles are DEGREES. Lengths are metres. +Z is up. Colours are (r, g, b) floats
0..1.

You may also use raw bpy for anything the API does not cover — the context is
intact, so `bpy.ops.mesh.primitive_grid_add(); ob = bpy.context.object` works
normally. Prefer `add(bpy.ops.mesh.primitive_grid_add, ...)`, which returns the
new object directly. Mesh vertices can be edited straight through
`ob.data.vertices` when a shape needs it.

HOW TO BUILD SOMETHING GOOD:
- Start with reset_scene(). That makes the new, empty file.
- Work out real proportions first, in metres, and comment them. A thing built
  from plausible measurements reads as that thing; one built from round
  numbers reads as a pile of primitives.
- Get the well-known ratios right before adding any detail. A guitar body is
  roughly a third of the instrument's length and much wider than its neck; a
  car is about twice as long as it is wide and the cabin sits in the middle
  third. A wrong silhouette is far more damaging than a missing detail, and no
  amount of small parts rescues it.
- Build from many simple parts placed precisely, not from one clever shape.
  Ten well-placed primitives beat one boolean.
- Give every visually distinct part its own material. Vary metallic and
  roughness: metal is metallic=0.9 roughness=0.15-0.35, plastic is
  metallic=0.0 roughness=0.4-0.6, glass is low roughness with alpha, glowing
  parts use emission.
- Use bevel= on anything with a hard edge. Nothing real has a perfect edge,
  and unbevelled boxes are the single clearest sign of an automated build.
- Detail means more parts, not smoother parts. The difference between a shape
  that suggests the subject and one that is it, is almost always the count of
  distinct pieces: seams, panel gaps, bolts, claws, teeth, scales, plates,
  buckles, vents, trim, straps, rivets. When a model looks unfinished, add
  twenty more small objects -- never inflate the ones already there.
- Do not reach for subdivide() to add detail. It is a smoothing modifier: on a
  box or a cylinder it melts the corners and you lose the form you built. Use
  it only on something already dense and deliberately shaped, like a lofted
  hull or a sculpted mass. The primitives are smooth-shaded and dense enough
  on their own -- a rounded blob is not more detailed than a crisp one, it is
  less.
- Never leave a flat plane standing in for a thin object. Wings, fins, leaves,
  sails, capes and blades have curve and thickness: loft through three or four
  cross-sections and solidify(ob, 0.02). Give a wing internal structure too --
  fingers, ribs, a folded edge -- or it reads as a kite.
- Keep parts anchored. A limb must overlap the body it grows from, a foot must
  overlap its leg, a plate must sit on the surface it armours. Floating and
  severed parts are the most common failure here, so overlap every joint
  deliberately rather than butting parts end to end.
- Use radial_array for anything repeating around an axis (fins, legs, blades,
  spokes) and linear_array along a line (fence posts, stairs, windows, teeth).
- Primitives cannot do curved or flowing things. For anything bent — cables,
  hoses, handles, branches, horns, rails, straps, vines — use curve_tube
  through a list of points. Use helix for springs, coils and threads,
  icosphere for organic masses, and taper to narrow a blade or a trunk.
- If a part is a surface of revolution, use revolve() with its silhouette.
  That is bottles, vases, glasses, bowls, domes, wheels, tyres, columns,
  barrels, lampshades, chess pieces, nose cones — far more things than it
  first appears, and revolve gets them exactly right where stacked cylinders
  never do.
- If a part is one continuous curved shell, use loft() through cross-sections:
  car bodies, boat hulls, aircraft fuselages, guitar bodies, helmets, shoes.
  A car built from boxes looks like boxes; a car lofted through five
  cross-sections looks like a car. Use mesh_from() for a shape that is
  neither.
- Use text3d for any lettering, number or logo the subject actually has.
- boolean() cuts real holes: windows, doorways, slots, hollows.
- Name every object something a human would recognise in the outliner.
- join() the parts that are one rigid piece; leave separately-coloured or
  moving parts alone.
- Sit the object on z=0 unless it should be flying. Do not let it sink through
  the floor.
- Stand the subject in its natural upright orientation, facing -Y. A person,
  robot, bottle or building stands up; a car sits on its wheels. Never leave
  the subject lying down unless that is what was asked for.
- End with frame_camera() and frame_viewport().
- Add ground() only if the subject belongs on a surface.

CONSTRAINTS:
- No file, network, or subprocess access of any kind.
- Do not quit Blender or save files.
- Keep it under about 250 lines.
- Build the whole subject, not a simplified stand-in. If it has ten
  recognisable parts, model ten parts.
- The script must run top to bottom with no input.
"""


def _build_prompt(version: str = "5.x") -> str:
    return _SYSTEM_PROMPT.format(version=version, api=api_reference())


def _retry_after(text: str) -> float | None:
    """Seconds to wait before retrying this model, or None to give up on it.

    A per-minute rate limit and an exhausted daily allowance are both 429, and
    treating them the same wastes either a minute or a working fallback. The
    quota id says which: anything "PerDay" will not recover today, so move on
    to the next model instead of sleeping.
    """
    if "PerDay" in text or "per day" in text.lower():
        return None
    if any(token in text for token in _TRANSIENT):
        return 2.0
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", text)
        if match:
            delay = float(match.group(1))
            return delay + 1.0 if delay <= 20.0 else None
        return 5.0
    return None


def _readable_error(model: str, text: str) -> str:
    """Turn a provider exception into something worth reading aloud."""
    if "PerDay" in text or "per day" in text.lower():
        return f"{model}: daily free-tier quota used up"
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return f"{model}: rate limited"
    if "404" in text or "NOT_FOUND" in text:
        return f"{model}: not available on this key"
    return f"{model}: {text.splitlines()[0][:100]}"


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def generate_code(description: str, previous: str = "", error: str = "") -> tuple[str, str]:
    """Ask Gemini for a build script. Returns (code, error_message)."""
    key = (_config().get("gemini_api_key") or "").strip()
    if not key:
        return "", "No Gemini API key is configured, so I can't write the build script."

    try:
        from google import genai
    except ImportError:
        return "", "The google-genai package is not installed."

    if error:
        user = (
            f"This script was supposed to build: {description}\n\n"
            f"It failed with:\n{error}\n\n"
            f"Here is the script:\n{previous}\n\n"
            "Return the whole corrected script. Fix the actual cause; do not "
            "delete the failing part unless nothing else can work."
        )
    else:
        user = f"Build this in Blender: {description}"

    client = models.client(key)
    config = {
        "system_instruction": _build_prompt(),
        "temperature": 0.35,
        # Detailed builds run long; 8192 truncated them mid-expression and
        # the script arrived with an unclosed bracket.
        "max_output_tokens": 32768,
    }

    failures: list[str] = []
    for model in CODE_MODELS():
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model, contents=user, config=config
                )
                code = _strip_fences(getattr(response, "text", "") or "")
                finish = ""
                try:
                    finish = str(response.candidates[0].finish_reason or "")
                except Exception:
                    pass
                if "MAX_TOKENS" in finish:
                    # A truncated script is worse than none: it fails on a
                    # syntax error that tells the next attempt nothing useful.
                    failures.append(f"{model} ran out of output budget")
                    break
                if code:
                    return code, ""
                failures.append(f"{model} returned nothing")
                break
            except Exception as exc:
                text = str(exc)
                wait = _retry_after(text)
                if attempt == 0 and wait is not None:
                    time.sleep(wait)
                    continue
                failures.append(_readable_error(model, text))
                break

    detail = "; ".join(failures)
    if all("quota" in f or "rate limited" in f for f in failures):
        return "", (
            "I've used up the free Gemini quota for today, so I can't write "
            f"the build script right now. ({detail})"
        )
    return "", f"Could not generate the build script. {detail}"


# ── actions ─────────────────────────────────────────────────────────────────

def _render_bytes(scale: int = 50) -> bytes | None:
    """Render the current camera view and read it back for inspection."""
    out = PROJECT_DIR / "_check.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import bpy\n"
        "scene = bpy.context.scene\n"
        "if scene.camera is None:\n"
        "    frame_camera()\n"
        "_pct = scene.render.resolution_percentage\n"
        f"scene.render.resolution_percentage = {int(scale)}\n"
        f"scene.render.filepath = {str(out)!r}\n"
        "try:\n"
        "    bpy.ops.render.render(write_still=True)\n"
        "finally:\n"
        "    scene.render.resolution_percentage = _pct\n"
    )
    if not _call({"command": "run", "code": code}, timeout=240.0).get("ok"):
        return None
    try:
        return out.read_bytes()
    except Exception:
        return None


def _refine(description: str, code: str, image: bytes) -> tuple[str, str]:
    """Show the model its own render and ask for a corrected script.

    Generated code fails in ways a traceback cannot report — a guitar with the
    right parts in the wrong proportions, strings floating beside the neck, a
    part left inside another. None of that raises; it just looks wrong. The
    render is the only place those errors are visible, so this is the pass that
    catches them.
    """
    key = (_config().get("gemini_api_key") or "").strip()
    if not key:
        return "", ""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "", ""

    prompt = (
        "This is a render of the model your script built. It was meant to be: "
        f"{description}\n\n"
        "Critique it the way a modeller reviewing a junior's work would — "
        "assume something is wrong, because something usually is. Check: are "
        "the proportions right against the real subject? Is it standing in a "
        "sensible orientation rather than lying over? Is anything floating "
        "detached, buried inside another part, or sunk through the floor? Are "
        "obvious features of the subject missing? Do the colours match the "
        "brief? Does the silhouette read as the subject at a glance?\n\n"
        "Reply in exactly this format:\n"
        "FAULTS:\n"
        "- one line per fault, most serious first\n"
        "SCRIPT:\n"
        "<the complete corrected script>\n\n"
        "The script must be the whole thing starting at reset_scene(), not a "
        "patch, and must keep everything that already works. Only write "
        "'- none' under FAULTS if the render is genuinely excellent; a merely "
        "acceptable result has faults worth fixing.\n\n"
        f"The script that produced it:\n{code}"
    )

    client = models.client(key)
    for model in CODE_MODELS():
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=image, mime_type="image/png"),
                    prompt,
                ],
                config={
                    "system_instruction": _build_prompt(),
                    "temperature": 0.3,
                    "max_output_tokens": 32768,
                },
            )
            reply = getattr(response, "text", "") or ""
            faults, revised = _split_critique(reply)
            if revised:
                return revised, faults
        except Exception as exc:
            if _retry_after(str(exc)) is None:
                continue            # this model is done for now; try the next
    return "", ""


def _split_critique(reply: str) -> tuple[str, str]:
    """Separate the FAULTS list from the corrected script."""
    marker = re.search(r"^\s*SCRIPT:\s*$", reply, re.MULTILINE)
    if not marker:
        # No structure came back; treat the whole reply as the script.
        return "", _strip_fences(reply)
    faults = reply[: marker.start()]
    faults = re.sub(r"^\s*FAULTS:\s*", "", faults.strip(), flags=re.MULTILINE)
    return faults.strip(), _strip_fences(reply[marker.end():])


def _autosave(description: str) -> str:
    """Save the .blend for a finished build. Never raises.

    Returns a fragment to append to the spoken reply, or "" if saving failed --
    a build that succeeded should still be reported as a success even if the
    file could not be written.
    """
    try:
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        # Same hash suffix the script cache uses. Without it two different
        # descriptions that share a slug after truncation -- easy with long,
        # detailed prompts -- silently overwrite each other's .blend.
        target = PROJECT_DIR / f"{_cache_path(description).stem}.blend"
        result = save_file(str(target))
        if result.startswith("Saved"):
            return f" Saved to {target}."
        print(f"[Blender] Autosave failed: {result}")
    except Exception as exc:
        print(f"[Blender] Autosave failed: {exc}")
    return ""


def create(description: str, player=None, quality: str = "best",
           reuse: bool = True) -> str:
    """Open Blender, start a new file, build what was described, and save it.

    quality="best" adds a pass where the model looks at a render of its own
    work and corrects what it sees. It roughly doubles both the time and the
    API calls, which matters on a free-tier key, so it is not the default.

    reuse=True replays a cached script for an identical description instead of
    calling the model. Pass reuse=False to force a fresh build.

    A successful build is written to disk before this returns -- a build that
    only exists in an unsaved Blender session is one crash away from being
    gone, and it cost real API quota to produce.
    """
    if not description or not description.strip():
        return "What would you like me to build?"

    def log(msg: str) -> None:
        print(f"[Blender] {msg}")
        if player is not None:
            try:
                player.write_log(f"SYS: {msg}")
            except Exception:
                pass

    ok, message = launch()
    if not ok:
        return message
    log(message)

    attempts: list[str] = []
    code = ""
    error = ""

    # Replay a script that already built this exact thing rather than paying
    # the model to write it again. If it no longer runs (a Blender upgrade
    # moved something under it) we fall straight through to generation, so a
    # stale entry costs one local execution, not a failed build.
    replay = "" if reuse is False else cached_script(description)
    if replay:
        log("Reusing the saved build script for this one (no model call).")
        result = run_code(replay)
        if result.get("ok"):
            code = replay
            _ensure_framed()
            summary = _describe_scene()
            log(f"Built from cache: {summary}")
            saved = _autosave(description)
            return f"Built {description} in Blender from the saved script. {summary}{saved}"
        log("The saved script no longer runs — writing a fresh one.")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"Writing the build script for “{description}” (attempt {attempt})…")
        code, gen_error = generate_code(description, code, error)
        if gen_error:
            return gen_error

        result = run_code(code)
        if result.get("offline"):
            return "Blender closed before I could build anything."
        if result.get("ok"):
            _ensure_framed()
            if str(quality).lower() in ("best", "high", "refine"):
                code = _refine_pass(description, code, log)
            summary = _describe_scene()
            log(f"Built: {summary}")
            _save_script(description, code)
            saved = _autosave(description)
            note = "" if attempt == 1 else f" (took {attempt} attempts)"
            return f"Built {description} in Blender{note}. {summary}{saved}"

        error = (result.get("error") or "").strip()
        attempts.append(error.splitlines()[-1] if error else "unknown error")
        log(f"Attempt {attempt} failed: {attempts[-1]}")

    return (
        f"I couldn't get a working build for “{description}” after "
        f"{MAX_ATTEMPTS} attempts. The last error was: {attempts[-1]}"
    )


# ── generated meshes (TRELLIS) ──────────────────────────────────────────────

# Imported straight from a generator, a mesh arrives in whatever scale and
# orientation the model felt like and with its origin wherever the bounding box
# happened to start. None of that is wrong exactly, but it makes the asset
# annoying to work with -- so normalise it on the way in: sit it on the floor,
# centre it on the origin, and scale it to a predictable size. That is the
# difference between "a mesh appeared" and "a model I can build with".
_IMPORT_GLB = """
import bpy, math
from mathutils import Vector

path = {path!r}
target_size = {size!r}
join_parts = {join!r}

before = {{o.name for o in bpy.context.scene.objects}}
bpy.ops.import_scene.gltf(filepath=path)
fresh = [o for o in bpy.context.scene.objects if o.name not in before]
meshes = [o for o in fresh if o.type == 'MESH']
if not meshes:
    raise RuntimeError('the file imported no meshes')

# glTF is Y-up, so the importer parents everything to an empty carrying the
# conversion to Blender's Z-up. Deleting that empty would silently tip the
# model on its side, and leaving it makes every later transform relative to a
# container the user cannot see -- so bake it into the meshes and then drop it.
bpy.ops.object.select_all(action='DESELECT')
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
if any(o.parent is not None for o in meshes):
    bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')

# Names, not references: joining frees the objects it merges, and touching one
# afterwards raises ReferenceError rather than returning anything useful.
leftovers = [o.name for o in fresh if o.type != 'MESH']

# A generator emits one shell per material; joining makes it a single asset
# that moves, scales and gets edited as one thing.
if join_parts and len(meshes) > 1:
    bpy.ops.object.join()
ob = bpy.context.view_layer.objects.active
ob.name = {name!r}

for stale in leftovers:
    victim = bpy.data.objects.get(stale)
    if victim is not None and not victim.children:
        bpy.data.objects.remove(victim, do_unlink=True)

bpy.ops.object.select_all(action='DESELECT')
ob.select_set(True)
bpy.context.view_layer.objects.active = ob
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

# Scale so the longest edge is target_size, then sit it on the floor centred
# on the origin -- the state every other tool here assumes.
dims = ob.dimensions
longest = max(dims.x, dims.y, dims.z)
if longest > 1e-6:
    ob.scale = (target_size / longest,) * 3
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
ob.location = (0.0, 0.0, 0.0)
bpy.context.view_layer.update()
low = min((ob.matrix_world @ Vector(c)).z for c in ob.bound_box)
ob.location.z -= low

# Autosmooth: a generated surface is dense, and flat shading wastes that
# detail -- but smoothing everything rounds off edges that should stay sharp,
# so shade smooth and let an angle threshold keep the creases.
for poly in ob.data.polygons:
    poly.use_smooth = True
try:
    mod = ob.modifiers.new('SharpEdges', 'SMOOTH_BY_ANGLE')
    mod.angle = math.radians(40.0)
except Exception:
    # Older Blender: the same control lived on the mesh, not a modifier.
    try:
        ob.data.use_auto_smooth = True
        ob.data.auto_smooth_angle = math.radians(40.0)
    except Exception:
        pass

verts = len(ob.data.vertices)
tris = sum(max(0, len(p.vertices) - 2) for p in ob.data.polygons)
mats = ','.join(m.name for m in ob.data.materials if m) or '-'
print(f"RESULT|{{ob.name}}|{{verts}}|{{tris}}|{{mats}}")
"""


def import_glb(path: str | Path, name: str = "Asset", size: float = 4.0,
               join: bool = True) -> dict:
    """Bring a .glb into the open Blender scene, normalised. Never raises."""
    code = _IMPORT_GLB.format(
        path=str(path), size=float(size), join=bool(join), name=name
    )
    return _call({"command": "run", "code": code}, timeout=300.0)


# Things whose shape *is* a structure: they have named parts in a fixed
# arrangement, and a script that places those parts gets the proportions right
# and stays editable afterwards. A generated mesh of a bridge is a single
# frozen shell whose spans are whatever the reference photo happened to show.
_STRUCTURAL = (
    "bridge", "building", "skyscraper", "tower", "house", "cabin", "hut",
    "stadium", "arena", "warehouse", "factory", "hangar", "terminal",
    "station", "airport", "room", "kitchen", "office", "interior", "floor plan",
    "floorplan", "city", "town", "street", "neighbourhood", "neighborhood",
    "staircase", "stairs", "roof", "wall", "fence", "pier", "dock", "dam",
    "tunnel", "viaduct", "aqueduct", "pyramid", "temple", "castle", "fort",
    "windmill", "lighthouse", "crane", "scaffold", "railway", "track",
    "solar farm", "wind farm", "pipeline", "rig", "platform",
)

# Things nobody has modelled because they do not exist until you say them.
# A library cannot help here at any price, so these go straight to a generator.
_INVENTED = (
    "abstract", "surreal", "surrealist", "melting", "dreamlike", "impossible",
    "invented", "imaginary", "fictional", "made up", "cross between",
    "hybrid of", "mix of", "fusion of", "crossed with", "half ", "part ",
    "reimagined", "as if", "in the style of", "inspired by", "version of",
    "but made of", "made entirely of", "shaped like", "twisted", "warped",
    "distorted", "organic form", "alien", "eldritch", "otherworldly",
)


def _looks_invented(low: str) -> bool:
    if any(w in low for w in _INVENTED):
        return True
    # "a X made of Y" is invented when Y is not what X is normally made of.
    # Detecting that properly needs world knowledge, so the cheap signal is
    # simply that the speaker bothered to specify a material at all.
    return bool(re.search(r"\bmade (?:out )?of\b", low))


def find_model(description: str, player=None, index: int = 0,
               commercial: bool = True, fresh_scene: bool = True) -> str:
    """Search Sketchfab for a ready-made model and import the best match."""
    from actions import sketchfab as sk

    def say(msg: str) -> None:
        print(f"[Library] {msg}")
        if player is not None:
            try:
                player.write_log(f"Jarvis: {msg}")
            except Exception:
                pass

    ok, note = launch()
    if not ok:
        return note

    licences = sk.OPEN_LICENSES if commercial else sk.ALL_FREE_LICENSES
    hits = sk.find(description, licences)
    if not hits and commercial:
        # Widening to NonCommercial finds far more, but the user is now holding
        # something they cannot ship, so it is said out loud rather than
        # quietly swapped in.
        hits = sk.find(description, sk.ALL_FREE_LICENSES)
        if hits:
            say("Nothing under a commercial licence, so this one is "
                "non-commercial — fine personally, not for anything you sell.")
    if not hits:
        return (f"I could not find a ready-made model of {description} in the "
                "library. I can generate one instead if you want.")

    if index >= len(hits):
        index = 0
    model = hits[index]
    faces = int(model.get("faceCount") or 0)
    say(f"Found \"{model['name']}\" — {faces:,} triangles. Downloading.")

    if not sk.configured():
        return ("I found models but I need a Sketchfab token to download them. "
                "Make a free account, copy the token from "
                "https://sketchfab.com/settings/password, and I will save it.")

    try:
        path, kind = sk.fetch(model)
    except Exception as exc:
        return f"The download failed: {exc}"

    if fresh_scene:
        run_code("import bpy\nreset_scene()")
    result = import_glb(path, name=model.get("name", "Asset")[:60])
    if not result.get("ok"):
        return f"I downloaded it but Blender could not import it: {result.get('error','')}"

    _ensure_framed()
    saved = _autosave(description)

    # CC-BY and CC-BY-SA require credit as a condition of the licence, so the
    # line is written next to the .blend where it survives the conversation.
    line = sk.credit(model)
    if sk.needs_credit(model):
        try:
            creds = Path(saved).parent / "CREDITS.txt" if saved else None
            if creds is not None:
                prior = creds.read_text() if creds.exists() else ""
                if line not in prior:
                    creds.write_text(prior + line + "\n")
        except Exception:
            pass

    others = ", ".join(m["name"][:28] for m in hits[1:4])
    tail = f' Other matches: {others}.' if others else ""
    note = f" Credit required: {line}" if sk.needs_credit(model) else " Public domain, no credit needed."
    return (f'I imported "{model["name"]}" — {faces:,} triangles.{note}{tail}')


def pick_method(description: str) -> str:
    """'library', 'script' or 'trellis' for a description. Used by method='auto'.

    Order matters and it is not the obvious one. The library is tried for
    almost everything concrete -- including dragons and other creatures, which
    people have modelled thousands of times -- because it is free, instant, and
    returns better topology than a generator. Generation is the last resort,
    reserved for things no one has made because they did not exist until the
    user described them.
    """
    low = f" {(description or '').lower()} "
    if _looks_invented(low):
        return "trellis"
    if any(w in low for w in _STRUCTURAL):
        return "script"
    return "library"


def create_mesh(description: str, player=None, quality: str = "standard",
                prefer: str = "auto", reuse: bool = True,
                fresh_scene: bool = True) -> str:
    """Generate a 3D model with TRELLIS.2 and drop it into Blender."""
    def log(msg: str) -> None:
        print(f"[Blender] {msg}")
        if player is not None:
            try:
                player.write_log(f"SYS: {msg}")
            except Exception:
                pass

    if not description or not description.strip():
        return "What would you like me to make?"

    from actions import trellis
    glb, note = trellis.build(description, quality, prefer, reuse, log=log)
    if glb is None:
        return note

    ok, message = launch()
    if not ok:
        return message

    if fresh_scene:
        _call({"command": "run", "code":
               "import bpy\ntry:\n    reset_scene()\nexcept Exception as e:\n"
               "    print('reset skipped:', e)\n"}, timeout=60.0)

    log("importing the mesh into Blender...")
    result = import_glb(glb, name=_slug(description)[:40] or "Asset")
    if result.get("offline"):
        return "Blender closed before I could import the model."
    if not result.get("ok"):
        detail = (result.get("error") or "").splitlines()[-1]
        return f"I generated the model but Blender could not import it: {detail}"

    # The importer logs to stdout, so pick out the tagged line rather than
    # trusting the last one.
    tagged = [ln for ln in (result.get("output") or "").splitlines()
              if ln.startswith("RESULT|")]
    summary = ""
    if tagged:
        _tag, _n, verts, tris, mats = tagged[-1].split("|", 4)
        summary = f" {int(verts):,} vertices, {int(tris):,} triangles"
        if mats and mats != "-":
            summary += f", materials: {mats}"
        summary += "."

    _ensure_framed()
    saved = _autosave(description)
    origin = "from the mesh I already had" if note == "from cache" else f"reference: {note}"
    return f"Built {description} in Blender ({origin}).{summary}{saved}"


def _ensure_framed() -> None:
    """Guarantee a usable view, whatever the build script did.

    A script that forgets frame_camera() leaves the scene with no camera at
    all, and one that framed before adding its last part leaves the subject
    half out of shot. Neither is worth a retry — but both look like a broken
    build to whoever is watching, so re-frame from here and always zoom the
    viewport, which is what the user is actually looking at in Blender.
    """
    _call({"command": "run", "code": _FRAMING_CODE}, timeout=60.0)


# frame_camera() fits the bounding sphere of everything in the scene. As soon
# as a build includes ground or water -- any landscape, any piece of
# architecture -- those planes are far larger than the subject, and the thing
# the user asked for renders as a speck in the middle of an empty field.
#
# So frame on the *structure*: drop meshes that are large and flat enough to be
# scenery, and aim at what is left. The test is geometric rather than a list of
# names, because the model names its ground plane something different every
# time. If nothing survives the filter the build is its own scenery (a terrain,
# say), so fall back to the stock framing rather than pointing at nothing.
_FRAMING_CODE = """
import bpy, math
from mathutils import Vector


def _structure_bounds():
    # Bounds that hold the bulk of the geometry, ignoring backdrop.
    #
    # Classifying scenery by shape does not work: the first attempt dropped
    # anything large and flat, which threw away a bridge deck (190 long, 0.18
    # thick) while keeping the shorelines. A deck is flat by nature.
    #
    # Vertex density separates them cleanly instead. A ground or water plane
    # is four vertices however big it is, while the subject carries thousands,
    # so trimming a small percentile off each axis discards the backdrop's
    # corners and keeps everything the build actually spent geometry on. It
    # needs no names, no shape rules, and it degrades gracefully: when the
    # scenery IS the subject, its vertices dominate and it frames normally.
    pts = []
    for o in bpy.context.scene.objects:
        if o.type != 'MESH' or not o.data.vertices:
            continue
        m = o.matrix_world
        pts.extend((m @ v.co) for v in o.data.vertices)
    if not pts:
        return None

    # A dense build can carry a lot of vertices; framing does not need all of
    # them, and sorting three axes of a million points is not free.
    if len(pts) > 40000:
        step = len(pts) // 40000 + 1
        pts = pts[::step]

    def trimmed(values):
        values.sort()
        n = len(values)
        k = int(n * 0.015)          # 1.5% off each end
        if n - 2 * k < 2:
            return values[0], values[-1]
        return values[k], values[n - 1 - k]

    xlo, xhi = trimmed([p.x for p in pts])
    ylo, yhi = trimmed([p.y for p in pts])
    zlo, zhi = trimmed([p.z for p in pts])
    lo, hi = Vector((xlo, ylo, zlo)), Vector((xhi, yhi, zhi))
    if (hi - lo).length < 1e-4:
        return None
    return lo, hi


try:
    found = _structure_bounds()
    if found is None:
        frame_camera()
    else:
        lo, hi = found
        centre = (lo + hi) / 2
        size = hi - lo
        cam = bpy.data.objects.get('JarvisCamera')
        if cam is None:
            cam = bpy.data.objects.new('JarvisCamera', bpy.data.cameras.new('JarvisCamera'))
            bpy.context.collection.objects.link(cam)
        lens = 55.0
        cam.data.lens = lens
        scene = bpy.context.scene
        res_x = scene.render.resolution_x * scene.render.pixel_aspect_x
        res_y = scene.render.resolution_y * scene.render.pixel_aspect_y
        sensor = cam.data.sensor_width
        tan_x = sensor / (2.0 * lens)
        tan_y = (sensor * res_y / res_x) / (2.0 * lens)

        # Three-quarter on and slightly above -- the angle that reads as a
        # photograph of a thing rather than a plan of it.
        a = math.radians(38)
        elev = math.radians(16)
        # Unit vector pointing from the camera toward the subject.
        fwd = Vector((-math.cos(a) * math.cos(elev),
                      math.sin(a) * math.cos(elev),
                      -math.sin(elev))).normalized()
        right = fwd.cross(Vector((0.0, 0.0, 1.0)))
        right = right.normalized() if right.length > 1e-6 else Vector((1.0, 0.0, 0.0))
        up = right.cross(fwd).normalized()

        # Fit the bounding BOX as projected onto the screen axes, not its
        # bounding sphere. A sphere fit is what makes a long, shallow subject
        # -- a bridge, a train, a street -- render as a speck: its sphere is
        # as tall as the thing is long. Solve, per corner, the distance at
        # which that corner just lands inside each field of view, and take the
        # largest; the +depth term accounts for near corners needing more room
        # than far ones under perspective.
        dist = 1.0
        for c in (Vector((x, y, z)) for x in (lo.x, hi.x)
                                    for y in (lo.y, hi.y)
                                    for z in (lo.z, hi.z)):
            off = c - centre
            depth = off.dot(fwd)
            need_x = abs(off.dot(right)) / tan_x - depth
            need_y = abs(off.dot(up)) / tan_y - depth
            dist = max(dist, need_x, need_y)
        dist *= 1.06          # a little air around the subject

        cam.location = centre - fwd * dist
        cam.rotation_euler = fwd.to_track_quat('-Z', 'Y').to_euler()
        scene.camera = cam
    frame_viewport()
except Exception as exc:
    print('framing skipped:', exc)
"""


def _refine_pass(description: str, code: str, log) -> str:
    """One look-and-fix cycle. Returns whichever script is currently live."""
    log("Checking the result against what was asked for…")
    image = _render_bytes()
    if not image:
        return code

    revised, faults = _refine(description, code, image)
    if faults:
        first = [ln.strip("- ").strip() for ln in faults.splitlines() if ln.strip()]
        if first and first[0].lower() not in ("none", "no faults"):
            log("Noticed: " + "; ".join(first[:3]))
    if not revised or revised.strip() == code.strip():
        log("The first build already looked right.")
        return code

    result = run_code(revised)
    if result.get("ok"):
        _ensure_framed()
        log("Applied corrections from the visual check.")
        return revised

    # The correction broke; put the working build back rather than leaving a
    # half-built scene on screen.
    log(f"Correction failed ({(result.get('error') or '').splitlines()[-1][:80]}); keeping the first build.")
    if run_code(code).get("ok"):
        _ensure_framed()
    return code


def _describe_scene() -> str:
    """Ask Blender what actually ended up in the scene."""
    # Count the evaluated mesh, not the base one. Modifiers -- subdivision
    # above all -- are where a lot of the real geometry lives, and reading
    # ob.data.vertices reports the cage instead, understating a subdivided
    # model by several times and making a finished build look unfinished.
    probe = """
import bpy
dg = bpy.context.evaluated_depsgraph_get()
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
verts = 0
for o in meshes:
    try:
        ev = o.evaluated_get(dg)
        verts += len(ev.to_mesh().vertices)
        ev.to_mesh_clear()
    except Exception:
        verts += len(o.data.vertices)
mats = sorted({m.name for o in meshes for m in o.data.materials if m})
print(f"{len(meshes)}|{verts}|" + ','.join(mats[:8]))
"""
    result = _call({"command": "run", "code": probe}, timeout=30.0)
    if not result.get("ok"):
        return ""
    try:
        count, verts, mats = result["output"].strip().split("|", 2)
        parts = f"{count} object{'s' if count != '1' else ''}, {verts} vertices"
        return f"{parts}, materials: {mats}." if mats else f"{parts}."
    except Exception:
        return ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "build"


CACHE_DIR = PROJECT_DIR / ".scripts"


def _cache_path(description: str) -> Path:
    """Where a known-good script for `description` lives.

    Keyed on a hash of the normalised description rather than the slug alone,
    so two builds whose slugs collide after truncation do not share a script.
    """
    key = hashlib.sha1(" ".join(description.lower().split()).encode()).hexdigest()[:12]
    return CACHE_DIR / f"{_slug(description)}-{key}.py"


def cached_script(description: str) -> str:
    """A script that has already built this exact thing, or "".

    This is the quota guard. Every generate_code() call spends against a free
    tier that runs out, and re-asking the model to rebuild something it has
    already built correctly is the easiest way to waste it -- which matters
    most while testing, when the same prompt gets run over and over. A build
    that succeeded once is replayed from disk for nothing.
    """
    try:
        path = _cache_path(description)
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        return ""


def _save_script(description: str, code: str) -> None:
    """Keep the script that built it, so a good result can be reproduced.

    Two copies: a timestamped one the user can browse, and the cache entry
    cached_script() replays.
    """
    try:
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        path = PROJECT_DIR / f"{_slug(description)}-{time.strftime('%Y%m%d-%H%M%S')}.py"
        path.write_text(code, encoding="utf-8")
    except Exception as exc:
        print(f"[Blender] Could not save the build script: {exc}")
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(description).write_text(code, encoding="utf-8")
    except Exception as exc:
        print(f"[Blender] Could not cache the build script: {exc}")


def _scene_inventory() -> str:
    """What is actually in the scene, named, for a targeted edit.

    Editing blind is how "make the nose cone blue" repaints the whole model:
    without this, the model cannot know that the nose and the fins share one
    material, so it edits the material and recolours both.
    """
    probe = """
import bpy
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
for o in meshes[:60]:
    mats = ','.join(m.name for m in o.data.materials if m) or '-'
    dims = tuple(round(v, 2) for v in o.dimensions)
    print(f"{o.name} | materials: {mats} | size: {dims}")
users = {}
for o in meshes:
    for m in o.data.materials:
        if m:
            users.setdefault(m.name, []).append(o.name)
print('---')
for name, objs in users.items():
    print(f"material {name} is used by: {', '.join(objs[:8])}")
"""
    result = _call({"command": "run", "code": probe}, timeout=30.0)
    return (result.get("output") or "").strip() if result.get("ok") else ""


def modify(instruction: str, player=None) -> str:
    """Change what is already in the scene, without starting over."""
    if not instruction.strip():
        return "What should I change?"
    if not is_running():
        return "Blender isn't open yet. Ask me to build something first."

    inventory = _scene_inventory()
    description = (
        "The scene ALREADY contains a finished model. Apply exactly this "
        f"change and nothing else: {instruction}\n\n"
        "Here is what is in the scene right now:\n"
        f"{inventory or '(inventory unavailable)'}\n\n"
        "RULES FOR AN EDIT:\n"
        "- Do NOT call reset_scene(), and do not rebuild anything that already "
        "exists.\n"
        "- Change ONLY what was asked for. Leave every other object, material "
        "and colour exactly as it is.\n"
        "- Note which objects share a material before you edit one. Changing a "
        "shared material recolours every object using it; if the change should "
        "affect only one object, give that object its own new material "
        "instead.\n"
        "- Reach existing things by name: bpy.data.objects['Name'], "
        "bpy.data.materials['Name'].\n"
        "- The builder API is still available if you need to add something."
    )
    code, gen_error = generate_code(description)
    if gen_error:
        return gen_error
    result = run_code(code)
    if result.get("ok"):
        return f"Done — {instruction}."
    return f"That change failed: {(result.get('error') or '').splitlines()[-1]}"


def save_file(path: str = "") -> str:
    if not is_running():
        return "Blender isn't open."
    if path:
        target = Path(path).expanduser()
    else:
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        target = PROJECT_DIR / f"build-{time.strftime('%Y%m%d-%H%M%S')}.blend"
    target.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import bpy\n"
        f"bpy.ops.wm.save_as_mainfile(filepath={str(target)!r})\n"
        f"print('saved')\n"
    )
    result = _call({"command": "run", "code": code}, timeout=120.0)
    if result.get("ok"):
        return f"Saved to {target}."
    return f"Could not save: {(result.get('error') or '').splitlines()[-1]}"


def preview(player=None) -> str:
    """Render the current camera view and show it in the Jarvis UI."""
    if not is_running():
        return "Blender isn't open."
    out = PROJECT_DIR / "preview.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Halve the resolution for speed, then put it back: leaving the scene at
    # 50% would quietly downgrade every render the user does afterwards.
    code = (
        "import bpy\n"
        "scene = bpy.context.scene\n"
        "if scene.camera is None:\n"
        "    frame_camera()\n"
        "_pct = scene.render.resolution_percentage\n"
        "scene.render.resolution_percentage = 50\n"
        f"scene.render.filepath = {str(out)!r}\n"
        "try:\n"
        "    bpy.ops.render.render(write_still=True)\n"
        "finally:\n"
        "    scene.render.resolution_percentage = _pct\n"
        "print('rendered')\n"
    )
    result = _call({"command": "run", "code": code}, timeout=240.0)
    if not result.get("ok"):
        return f"Could not render: {(result.get('error') or '').splitlines()[-1]}"
    if player is not None and out.exists():
        try:
            player.show_camera_frame(out.read_bytes())
        except Exception:
            pass
    return f"Rendered a preview to {out}."


def status() -> str:
    ping = _call({"command": "ping"}, timeout=4.0)
    if not ping.get("ok"):
        exe = find_blender()
        where = f"Blender is installed at {exe}." if exe else "Blender is not installed."
        return f"Blender isn't open. {where}"
    scene = _describe_scene()
    return f"Blender {ping.get('blender', '')} is open. {scene}".strip()


def blender_control(parameters: dict, player=None) -> str:
    """Entry point — routes on parameters["action"]."""
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()
    action = action.replace("-", "_").replace(" ", "_")
    description = (
        params.get("description") or params.get("prompt")
        or params.get("instruction") or ""
    )

    if player is not None:
        try:
            player.write_log(f"[Blender] {action}")
        except Exception:
            pass

    if action in ("create", "build", "make", "model", "new", "generate"):
        # "rebuild"/"from scratch"/"try again" means the user wants a fresh
        # result, not the cached one that produced what they just rejected.
        reuse = params.get("reuse", True)
        if isinstance(reuse, str):
            reuse = reuse.strip().lower() not in ("false", "no", "0", "off")
        if re.search(r"\b(rebuild|from scratch|start over|try again|redo)\b",
                     description, re.I):
            reuse = False

        method = (params.get("method") or "auto").strip().lower()
        if method in ("mesh", "generate", "generated", "ai", "trellis2", "trellis.2"):
            method = "trellis"
        if method in ("procedural", "code", "build"):
            method = "script"
        if method in ("sketchfab", "search", "find", "download", "asset"):
            method = "library"
        if method not in ("script", "trellis", "library"):
            method = pick_method(description)

        if method == "library":
            from actions import sketchfab as sk
            # Without a token the library can search but never download, so
            # routing there would dead-end on a request the script builder
            # could have answered. Only take the library path if it can finish.
            if sk.configured() and sk.find(description):
                return find_model(
                    description, player,
                    index=int(params.get("index") or 0),
                    commercial=str(params.get("commercial", "true")).lower()
                               not in ("false", "no", "0"),
                )
            # Nothing usable in the library. Generation is the only honest
            # answer for an invented thing, but it costs money and may not be
            # set up -- so fall back to the script builder, which always works.
            from actions import trellis as _t
            if _looks_invented(f" {description.lower()} ") and _t.configured():
                method = "trellis"
            else:
                method = "script"

        if method == "trellis":
            from actions import trellis
            if trellis.configured():
                return create_mesh(
                    description, player,
                    params.get("quality", "standard"),
                    params.get("prefer", "auto"), reuse,
                )
            # No key: say so once, then still build something rather than
            # failing outright. The script builder is a real fallback, not a
            # consolation -- it is what produced everything before this.
            print("[Blender] No fal key; falling back to the script builder.")
        return create(description, player, params.get("quality", "best"), reuse)
    if action in ("modify", "change", "edit", "adjust"):
        return modify(description, player)
    if action in ("open", "launch", "start"):
        return launch()[1]
    if action in ("save", "save_file"):
        return save_file(params.get("path", ""))
    if action in ("preview", "render", "screenshot"):
        return preview(player)
    if action == "status":
        return status()
    if action in ("run_code", "code", "script"):
        result = run_code(params.get("code", ""))
        if result.get("ok"):
            return (result.get("output") or "Done.").strip()[:600]
        return f"Script failed: {(result.get('error') or '').splitlines()[-1]}"

    return (
        f"Unknown blender action: '{action}'. "
        "Valid actions: create | modify | open | save | preview | status | "
        "run_code."
    )
