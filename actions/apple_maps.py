"""Apple Maps control — directions, search, and dropping a pin.

Maps has no useful AppleScript dictionary, so everything here goes through the
documented `maps://` URL scheme and `open`. That is a fire-and-forget channel:
the app is handed a URL and takes over, so these functions can report that Maps
was asked for something but never that it found it.
"""
from __future__ import annotations

import platform
import subprocess
from urllib.parse import quote

# Apple's dirflg values. Anything not in here is not a mode Maps understands.
_MODES = {
    "drive": "d", "driving": "d", "car": "d", "d": "d",
    "walk": "w", "walking": "w", "foot": "w", "w": "w",
    "transit": "r", "public": "r", "bus": "r", "train": "r",
    "subway": "r", "metro": "r", "r": "r",
    "cycle": "b", "cycling": "b", "bike": "b", "bicycle": "b", "b": "b",
}
_MODE_NAMES = {"d": "driving", "w": "walking", "r": "transit", "b": "cycling"}


def _check_macos() -> str | None:
    if platform.system() != "Darwin":
        return "Apple Maps is only available on macOS."
    return None


def _open_url(url: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["open", url], capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip() or "open failed"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Maps did not respond in time."
    except Exception as exc:
        return False, str(exc)


def directions(
    destination: str, origin: str | None = None, mode: str = "drive"
) -> str:
    """Open Maps with a route to `destination`.

    Omitting `saddr` entirely is what makes Maps route from wherever you are —
    passing a placeholder like "Current Location" makes it search for a place
    by that name instead, which is why the origin is left out rather than
    filled in.
    """
    err = _check_macos()
    if err:
        return err
    if not destination or not destination.strip():
        return "Where would you like directions to?"

    flag = _MODES.get((mode or "drive").strip().lower(), "d")
    url = f"maps://?daddr={quote(destination.strip())}&dirflg={flag}"
    if origin and origin.strip() and origin.strip().lower() not in (
        "here", "current location", "my location", "me",
    ):
        url += f"&saddr={quote(origin.strip())}"
        from_txt = f" from {origin.strip()}"
    else:
        from_txt = " from your current location"

    ok, detail = _open_url(url)
    if not ok:
        return f"Could not open Maps: {detail}"
    return (
        f"Opened {_MODE_NAMES[flag]} directions to {destination.strip()}"
        f"{from_txt} in Maps."
    )


def search(query: str) -> str:
    """Search Maps for a place, business or category near you."""
    err = _check_macos()
    if err:
        return err
    if not query or not query.strip():
        return "What should I look for in Maps?"

    ok, detail = _open_url(f"maps://?q={quote(query.strip())}")
    if not ok:
        return f"Could not open Maps: {detail}"
    return f"Searching Maps for {query.strip()}."


def show(place: str) -> str:
    """Drop Maps on a specific place without starting a route."""
    err = _check_macos()
    if err:
        return err
    if not place or not place.strip():
        return "Which place should I show?"

    ok, detail = _open_url(f"maps://?address={quote(place.strip())}")
    if not ok:
        return f"Could not open Maps: {detail}"
    return f"Showing {place.strip()} in Maps."


def open_maps() -> str:
    err = _check_macos()
    if err:
        return err
    ok, detail = _open_url("maps://")
    return "Opened Maps." if ok else f"Could not open Maps: {detail}"


def apple_maps(parameters: dict, player=None) -> str:
    """Entry point — routes on parameters["action"]."""
    params = parameters or {}
    action = (params.get("action", "") or "").strip().lower()
    action = action.replace("-", "_").replace(" ", "_")

    if player:
        try:
            player.write_log(f"[AppleMaps] {action}")
        except Exception:
            pass

    if action in ("directions", "route", "navigate", "get_directions"):
        return directions(
            params.get("destination") or params.get("query") or "",
            params.get("origin"),
            params.get("mode") or "drive",
        )
    if action in ("search", "find", "nearby"):
        return search(params.get("query") or params.get("destination") or "")
    if action in ("show", "show_place", "pin", "location"):
        return show(params.get("query") or params.get("destination") or "")
    if action in ("open", "open_maps", ""):
        return open_maps()

    return (
        f"Unknown apple_maps action: '{action}'. "
        "Valid actions: directions, search, show, open."
    )
