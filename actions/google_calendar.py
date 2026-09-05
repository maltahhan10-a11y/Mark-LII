"""
Google Calendar — OAuth 2.0 integration for listing, creating, searching,
and deleting calendar events.

Requires ``google-api-python-client`` and ``google-auth-oauthlib``.
All Google libraries are imported lazily inside functions.

Gated behind ``google_calendar_enabled`` in config/api_keys.json.
OAuth token is stored at config/google_token.json.
Credentials (client secret) are read from config/google_credentials.json.
"""

import json
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = _base_dir()
CONFIG_DIR       = BASE_DIR / "config"
CREDENTIALS_PATH = CONFIG_DIR / "google_credentials.json"
TOKEN_PATH       = CONFIG_DIR / "google_token.json"
API_CONFIG_PATH  = CONFIG_DIR / "api_keys.json"

SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str, player=None):
    print(f"[GoogleCalendar] {msg}")
    if player:
        try:
            player.write_log(f"[GoogleCalendar] {msg}")
        except Exception:
            pass


def _is_enabled() -> bool:
    try:
        cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        return bool(cfg.get("google_calendar_enabled", False))
    except Exception:
        return False


def _get_credentials():
    """Load or refresh OAuth credentials, opening browser on first use."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        except Exception:
            creds = None

    # Check that existing token covers our scopes
    if creds and not set(SCOPES).issubset(set(creds.scopes or [])):
        # Preserve any extra scopes (e.g. Gmail) while adding ours
        all_scopes = list(set((creds.scopes or []) + SCOPES))
        creds = None  # force re-auth with merged scopes

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if not CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"Google credentials file not found at {CREDENTIALS_PATH}. "
                "Download it from Google Cloud Console."
            )
        # Merge any existing scopes (e.g. Gmail) to avoid losing them
        merged_scopes = list(set(SCOPES))
        if TOKEN_PATH.exists():
            try:
                old = Credentials.from_authorized_user_file(str(TOKEN_PATH))
                if old.scopes:
                    merged_scopes = list(set(merged_scopes) | set(old.scopes))
            except Exception:
                pass

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), merged_scopes
        )
        creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _build_service():
    from googleapiclient.discovery import build
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


# ── Individual features ──────────────────────────────────────────────────────

def _list_events(parameters: dict, player=None) -> str:
    from datetime import datetime, timedelta, timezone

    days_ahead  = int(parameters.get("days_ahead", 7))
    calendar_id = parameters.get("calendar_id", "primary").strip()

    service = _build_service()

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()

    try:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=25,
        ).execute()
    except Exception as e:
        return f"Failed to list events: {e}"

    events = result.get("items", [])
    if not events:
        return f"No upcoming events in the next {days_ahead} days."

    lines = [f"Upcoming events (next {days_ahead} days):"]
    for ev in events:
        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "?")
        end   = ev.get("end",   {}).get("dateTime") or ev.get("end",   {}).get("date", "")
        summary  = ev.get("summary", "(No title)")
        location = ev.get("location", "")
        event_id = ev.get("id", "")

        # Trim ISO timestamps to readable form
        if "T" in start:
            start = start[:16].replace("T", " ")
        if end and "T" in end:
            end = end[:16].replace("T", " ")

        line = f"  - {start}"
        if end:
            line += f" to {end}"
        line += f": {summary}"
        if location:
            line += f" ({location})"
        line += f"  [ID: {event_id}]"
        lines.append(line)

    return "\n".join(lines)


def _create_event(parameters: dict, player=None) -> str:
    summary     = parameters.get("summary", "").strip()
    start       = parameters.get("start", "").strip()
    end         = parameters.get("end", "").strip()
    description = parameters.get("description", "").strip() or None
    location    = parameters.get("location", "").strip() or None
    calendar_id = parameters.get("calendar_id", "primary").strip()

    if not summary:
        return "Please provide an event summary/title."
    if not start or not end:
        return "Please provide both start and end times (ISO 8601 format, e.g. 2025-03-15T10:00:00)."

    body: dict = {
        "summary": summary,
        "start":   {},
        "end":     {},
    }

    # Support all-day events (date only) vs timed events
    if "T" in start:
        body["start"]["dateTime"] = start
        body["start"]["timeZone"] = parameters.get("timezone", "UTC")
    else:
        body["start"]["date"] = start

    if "T" in end:
        body["end"]["dateTime"] = end
        body["end"]["timeZone"] = parameters.get("timezone", "UTC")
    else:
        body["end"]["date"] = end

    if description:
        body["description"] = description
    if location:
        body["location"] = location

    service = _build_service()
    try:
        event = service.events().insert(
            calendarId=calendar_id,
            body=body,
        ).execute()
    except Exception as e:
        return f"Failed to create event: {e}"

    link = event.get("htmlLink", "")
    return f"Event created: {event.get('summary', summary)}. Link: {link}"


def _search_events(parameters: dict, player=None) -> str:
    from datetime import datetime, timedelta, timezone

    query      = parameters.get("query", "").strip()
    days_ahead = int(parameters.get("days_ahead", 30))

    if not query:
        return "Please provide a search query."

    service = _build_service()
    now = datetime.now(timezone.utc)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
    except Exception as e:
        return f"Event search failed: {e}"

    events = result.get("items", [])
    if not events:
        return f"No events matching '{query}' in the next {days_ahead} days."

    lines = [f"Events matching '{query}':"]
    for ev in events:
        start   = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "?")
        summary = ev.get("summary", "(No title)")
        event_id = ev.get("id", "")
        if "T" in start:
            start = start[:16].replace("T", " ")
        lines.append(f"  - {start}: {summary}  [ID: {event_id}]")

    return "\n".join(lines)


def _delete_event(parameters: dict, player=None) -> str:
    event_id    = parameters.get("event_id", "").strip()
    calendar_id = parameters.get("calendar_id", "primary").strip()

    if not event_id:
        return "Please provide the event ID to delete."

    service = _build_service()
    try:
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()
    except Exception as e:
        return f"Failed to delete event: {e}"

    return f"Event {event_id} deleted successfully."


def _list_calendars(parameters: dict, player=None) -> str:
    service = _build_service()
    try:
        result = service.calendarList().list().execute()
    except Exception as e:
        return f"Failed to list calendars: {e}"

    calendars = result.get("items", [])
    if not calendars:
        return "No calendars found."

    lines = ["Your calendars:"]
    for cal in calendars:
        cal_id   = cal.get("id", "?")
        summary  = cal.get("summary", "(No name)")
        primary  = " [PRIMARY]" if cal.get("primary") else ""
        lines.append(f"  - {summary}{primary}  (ID: {cal_id})")

    return "\n".join(lines)


# ── Action router ────────────────────────────────────────────────────────────
_ACTIONS = {
    "list_events":    _list_events,
    "create_event":   _create_event,
    "search_events":  _search_events,
    "delete_event":   _delete_event,
    "list_calendars": _list_calendars,
}


def google_calendar(parameters: dict, player=None) -> str:
    """
    Main entry point.  Routes on ``parameters["action"]``:

        list_events | create_event | search_events | delete_event | list_calendars
    """
    if not _is_enabled():
        return (
            "Google Calendar is not enabled. "
            "Set \"google_calendar_enabled\": true in config/api_keys.json."
        )

    params = parameters or {}
    action = params.get("action", "").strip().lower()

    handler = _ACTIONS.get(action)
    if handler is None:
        available = ", ".join(sorted(_ACTIONS))
        return f"Unknown google_calendar action '{action}'. Available: {available}"

    try:
        return handler(params, player)
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        _log(f"Action '{action}' failed: {e}", player)
        return f"Google Calendar request failed: {e}"
