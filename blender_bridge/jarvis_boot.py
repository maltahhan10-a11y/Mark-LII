"""In-Blender command server for MARK LII.

Blender is launched with `--python this_file`, which leaves the GUI running
normally and adds a loopback socket that accepts Python to execute.

The one hard constraint shapes everything below: **bpy may only be touched
from Blender's main thread.** Calling into it from a socket handler corrupts
state or crashes the process outright, with no useful error. So the socket
threads never execute anything; they put the code on a queue, and a timer
registered on the main thread drains that queue, runs the code, and hands the
result back through a per-request reply queue.

The execution namespace persists between requests, so a follow-up command can
refer to what an earlier one built ("make the fins bigger" works on the `fins`
that are still bound).
"""
from __future__ import annotations

import io
import json
import os
import queue
import socket
import sys
import threading
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import bpy

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARVIS_BLENDER_PORT", "8787"))
TICK = 0.05           # seconds between main-thread drains
MAX_MESSAGE = 4 << 20  # 4 MB of source is far more than any build needs

_jobs: "queue.Queue[tuple[str, queue.Queue]]" = queue.Queue()
_namespace: dict = {}


def _build_namespace() -> dict:
    """The globals a build script runs against."""
    import math

    ns: dict = {
        "__name__": "__jarvis_build__",
        "bpy": bpy,
        "math": math,
    }
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jarvis_lib

        for name in jarvis_lib.__all__:
            ns[name] = getattr(jarvis_lib, name)
        ns["jarvis_lib"] = jarvis_lib
    except Exception:
        print("[JarvisBridge] jarvis_lib unavailable:\n" + traceback.format_exc())
    return ns


def _execute(code: str) -> dict:
    """Run one request on the main thread. Never raises."""
    global _namespace
    if not _namespace:
        _namespace = _build_namespace()

    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            exec(compile(code, "<jarvis-build>", "exec"), _namespace)
    except Exception:
        return {
            "ok": False,
            "output": out.getvalue()[-4000:],
            "error": traceback.format_exc(limit=6)[-4000:],
        }
    return {"ok": True, "output": out.getvalue()[-4000:], "error": ""}


def _drain() -> float:
    """Main-thread pump. Returns the delay until Blender calls it again."""
    while True:
        try:
            code, reply = _jobs.get_nowait()
        except queue.Empty:
            break
        try:
            reply.put(_execute(code))
        except Exception:
            # A reply that cannot be delivered must not kill the timer, or the
            # bridge goes silently deaf for the rest of the session.
            try:
                reply.put({"ok": False, "output": "", "error": traceback.format_exc()})
            except Exception:
                pass
    return TICK


def _handle(conn: socket.socket) -> None:
    conn.settimeout(300.0)
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            if len(buf) > MAX_MESSAGE:
                raise ValueError("request too large")

        line, _, _rest = buf.partition(b"\n")
        request = json.loads(line.decode("utf-8"))
        command = request.get("command", "run")

        if command == "ping":
            response = {
                "ok": True,
                "output": "pong",
                "error": "",
                "blender": bpy.app.version_string,
                "pid": os.getpid(),
            }
        elif command == "reset_namespace":
            global _namespace
            _namespace = _build_namespace()
            response = {"ok": True, "output": "namespace reset", "error": ""}
        else:
            reply: queue.Queue = queue.Queue(maxsize=1)
            _jobs.put((request.get("code", ""), reply))
            try:
                # Generous: a boolean-heavy build or a subdivided mesh can take
                # a while, and the caller has its own shorter timeout anyway.
                response = reply.get(timeout=280.0)
            except queue.Empty:
                response = {
                    "ok": False,
                    "output": "",
                    "error": "Blender did not finish this in time.",
                }

        conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
    except Exception:
        try:
            conn.sendall(
                json.dumps(
                    {"ok": False, "output": "", "error": traceback.format_exc()}
                ).encode("utf-8")
                + b"\n"
            )
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as exc:
        print(f"[JarvisBridge] Could not bind {HOST}:{PORT} — {exc}")
        return
    srv.listen(8)
    print(f"[JarvisBridge] Listening on {HOST}:{PORT} (Blender {bpy.app.version_string})")
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def register() -> None:
    if not bpy.app.timers.is_registered(_drain):
        bpy.app.timers.register(_drain, persistent=True)
    threading.Thread(target=_serve, name="jarvis-bridge", daemon=True).start()


register()
