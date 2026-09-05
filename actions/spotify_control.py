"""
Spotify playback control for Mark-LII.

Uses the spotipy library with SpotifyOAuth for full playback control,
including now-playing, play/pause, skip, search, volume, and playlists.

Config keys required in config/api_keys.json:
    spotify_client_id, spotify_client_secret, spotify_redirect_uri
"""

import json
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


API_CONFIG_PATH = _base_dir() / "config" / "api_keys.json"


def _load_config() -> dict:
    """Load config/api_keys.json and return the full dict."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_spotify_client():
    """Build and return an authenticated spotipy.Spotify client."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    cfg = _load_config()
    client_id = cfg.get("spotify_client_id", "")
    client_secret = cfg.get("spotify_client_secret", "")
    redirect_uri = cfg.get("spotify_redirect_uri", "http://localhost:8888/callback")

    if not client_id or not client_secret:
        raise ValueError(
            "Spotify credentials not configured. "
            "Set spotify_client_id and spotify_client_secret in config/api_keys.json."
        )

    scope = (
        "user-read-playback-state "
        "user-modify-playback-state "
        "user-read-currently-playing "
        "playlist-read-private "
        "playlist-read-collaborative"
    )

    cache_path = _base_dir() / ".spotify_cache"

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_path=str(cache_path),
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def _log(message: str, player=None) -> None:
    print(f"[Spotify] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


# ── Individual action functions ──────────────────────────────────────────────


def spotify_now_playing(parameters: dict, player=None) -> str:
    """Return info about the currently playing track."""
    sp = _get_spotify_client()
    current = sp.current_playback()

    if not current or not current.get("item"):
        msg = "Nothing is currently playing on Spotify, sir."
        _log(msg, player)
        return msg

    item = current["item"]
    track_name = item.get("name", "Unknown")
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    album = item.get("album", {}).get("name", "Unknown")
    is_playing = current.get("is_playing", False)
    progress_ms = current.get("progress_ms", 0)
    duration_ms = item.get("duration_ms", 0)

    progress_s = progress_ms // 1000
    duration_s = duration_ms // 1000
    progress_fmt = f"{progress_s // 60}:{progress_s % 60:02d}"
    duration_fmt = f"{duration_s // 60}:{duration_s % 60:02d}"
    state = "Playing" if is_playing else "Paused"

    msg = (
        f"{state}: \"{track_name}\" by {artists} "
        f"from \"{album}\" [{progress_fmt}/{duration_fmt}]"
    )
    _log(msg, player)
    return msg


def spotify_play_pause(parameters: dict, player=None) -> str:
    """Toggle playback between play and pause."""
    sp = _get_spotify_client()
    current = sp.current_playback()

    if not current:
        msg = "No active Spotify device found. Please open Spotify on a device first, sir."
        _log(msg, player)
        return msg

    if current.get("is_playing"):
        sp.pause_playback()
        msg = "Spotify playback paused, sir."
    else:
        sp.start_playback()
        msg = "Spotify playback resumed, sir."

    _log(msg, player)
    return msg


def spotify_next_track(parameters: dict, player=None) -> str:
    """Skip to the next track."""
    sp = _get_spotify_client()
    current = sp.current_playback()

    if not current:
        msg = "No active Spotify device found. Please open Spotify on a device first, sir."
        _log(msg, player)
        return msg

    sp.next_track()
    msg = "Skipped to the next track, sir."
    _log(msg, player)
    return msg


def spotify_previous_track(parameters: dict, player=None) -> str:
    """Go back to the previous track."""
    sp = _get_spotify_client()
    current = sp.current_playback()

    if not current:
        msg = "No active Spotify device found. Please open Spotify on a device first, sir."
        _log(msg, player)
        return msg

    sp.previous_track()
    msg = "Went back to the previous track, sir."
    _log(msg, player)
    return msg


def spotify_search(parameters: dict, player=None) -> str:
    """Search Spotify for tracks, artists, albums, or playlists."""
    query = parameters.get("query", "").strip()
    search_type = parameters.get("type", "track").strip().lower()

    if not query:
        msg = "No search query provided, sir."
        _log(msg, player)
        return msg

    valid_types = ("track", "artist", "album", "playlist")
    if search_type not in valid_types:
        search_type = "track"

    sp = _get_spotify_client()
    results = sp.search(q=query, type=search_type, limit=5)

    key = search_type + "s"
    items = results.get(key, {}).get("items", [])

    if not items:
        msg = f"No {search_type} results found for \"{query}\", sir."
        _log(msg, player)
        return msg

    lines = [f"Top {search_type} results for \"{query}\":"]
    for i, item in enumerate(items, 1):
        name = item.get("name", "Unknown")
        if search_type == "track":
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            lines.append(f"  {i}. \"{name}\" by {artists}")
        elif search_type == "artist":
            followers = item.get("followers", {}).get("total", 0)
            lines.append(f"  {i}. {name} ({followers:,} followers)")
        elif search_type == "album":
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            year = (item.get("release_date") or "")[:4]
            lines.append(f"  {i}. \"{name}\" by {artists} ({year})")
        elif search_type == "playlist":
            owner = item.get("owner", {}).get("display_name", "Unknown")
            total = item.get("tracks", {}).get("total", 0)
            lines.append(f"  {i}. \"{name}\" by {owner} ({total} tracks)")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


def spotify_play_track(parameters: dict, player=None) -> str:
    """Search for a track/song and play it immediately."""
    query = parameters.get("query", "").strip()

    if not query:
        msg = "No track query provided, sir."
        _log(msg, player)
        return msg

    sp = _get_spotify_client()

    # Check for an active device first
    current = sp.current_playback()
    if not current and not sp.devices().get("devices"):
        msg = "No active Spotify device found. Please open Spotify on a device first, sir."
        _log(msg, player)
        return msg

    results = sp.search(q=query, type="track", limit=1)
    tracks = results.get("tracks", {}).get("items", [])

    if not tracks:
        msg = f"No tracks found for \"{query}\", sir."
        _log(msg, player)
        return msg

    track = tracks[0]
    track_uri = track["uri"]
    track_name = track.get("name", "Unknown")
    artists = ", ".join(a["name"] for a in track.get("artists", []))

    sp.start_playback(uris=[track_uri])
    msg = f"Now playing \"{track_name}\" by {artists}, sir."
    _log(msg, player)
    return msg


def spotify_set_volume(parameters: dict, player=None) -> str:
    """Set Spotify playback volume (0-100)."""
    level = parameters.get("level")

    if level is None:
        msg = "No volume level provided, sir. Please specify a value between 0 and 100."
        _log(msg, player)
        return msg

    try:
        level = int(level)
    except (ValueError, TypeError):
        msg = "Invalid volume level. Please provide a number between 0 and 100, sir."
        _log(msg, player)
        return msg

    level = max(0, min(100, level))

    sp = _get_spotify_client()
    current = sp.current_playback()

    if not current:
        msg = "No active Spotify device found. Please open Spotify on a device first, sir."
        _log(msg, player)
        return msg

    sp.volume(level)
    msg = f"Spotify volume set to {level}%, sir."
    _log(msg, player)
    return msg


def spotify_get_playlists(parameters: dict, player=None) -> str:
    """List the user's Spotify playlists."""
    sp = _get_spotify_client()
    results = sp.current_user_playlists(limit=20)
    items = results.get("items", [])

    if not items:
        msg = "No playlists found on your Spotify account, sir."
        _log(msg, player)
        return msg

    lines = ["Your Spotify playlists:"]
    for i, pl in enumerate(items, 1):
        name = pl.get("name", "Untitled")
        total = pl.get("tracks", {}).get("total", 0)
        owner = pl.get("owner", {}).get("display_name", "Unknown")
        lines.append(f"  {i}. \"{name}\" by {owner} ({total} tracks)")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


# ── Main entry point ─────────────────────────────────────────────────────────

_ACTION_MAP = {
    "now_playing":    spotify_now_playing,
    "play_pause":     spotify_play_pause,
    "next":           spotify_next_track,
    "next_track":     spotify_next_track,
    "previous":       spotify_previous_track,
    "previous_track": spotify_previous_track,
    "search":         spotify_search,
    "play_track":     spotify_play_track,
    "play":           spotify_play_track,
    "volume":         spotify_set_volume,
    "set_volume":     spotify_set_volume,
    "playlists":      spotify_get_playlists,
    "get_playlists":  spotify_get_playlists,
}


def spotify_control(parameters: dict, player=None) -> str:
    """
    Main entry point for Spotify control.
    Routes to the appropriate handler based on parameters["action"].
    """
    # Gate on config
    cfg = _load_config()
    if not cfg.get("spotify_client_id") or not cfg.get("spotify_client_secret"):
        msg = (
            "Spotify is not configured, sir. "
            "Please add spotify_client_id and spotify_client_secret "
            "to config/api_keys.json."
        )
        _log(msg, player)
        return msg

    action = parameters.get("action", "").strip().lower()

    if not action:
        msg = "No Spotify action specified, sir."
        _log(msg, player)
        return msg

    handler = _ACTION_MAP.get(action)
    if not handler:
        supported = ", ".join(sorted(_ACTION_MAP.keys()))
        msg = f"Unknown Spotify action: \"{action}\". Supported actions: {supported}"
        _log(msg, player)
        return msg

    try:
        return handler(parameters, player=player)
    except Exception as e:
        error_str = str(e).lower()
        if "no active device" in error_str or "player command failed" in error_str:
            msg = (
                "No active Spotify device found, sir. "
                "Please open Spotify on one of your devices first."
            )
        elif "premium" in error_str:
            msg = "This feature requires a Spotify Premium subscription, sir."
        else:
            msg = f"Spotify error: {e}"
        _log(msg, player)
        return msg
