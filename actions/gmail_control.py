"""
Gmail Control — OAuth 2.0 integration (DRAFT-ONLY, never auto-sends).

Supports inbox listing, unread count, email reading, search, draft creation,
and label listing.

CRITICAL SAFETY:
  - Scope is ``gmail.modify`` — the API itself cannot send mail.
  - ``gmail_create_draft`` creates drafts ONLY.

Requires ``google-api-python-client`` and ``google-auth-oauthlib``.
All Google libraries are imported lazily inside functions.

Gated behind ``gmail_enabled`` in config/api_keys.json.
Shares the OAuth token file with Google Calendar (config/google_token.json).
If the existing token lacks Gmail scopes, scopes are merged (preserving
Calendar scopes) and the user is re-authenticated.
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

# gmail.modify — read, draft, label, trash; CANNOT send.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str, player=None):
    print(f"[Gmail] {msg}")
    if player:
        try:
            player.write_log(f"[Gmail] {msg}")
        except Exception:
            pass


def _is_enabled() -> bool:
    try:
        cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        return bool(cfg.get("gmail_enabled", False))
    except Exception:
        return False


def _get_credentials():
    """
    Load or refresh OAuth credentials.  If the existing token does not
    include Gmail scopes, merge them with whatever scopes are already
    present (e.g. Calendar) and re-authenticate via browser.
    """
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    existing_scopes: list[str] = []

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
            existing_scopes = list(creds.scopes or [])
        except Exception:
            creds = None

    # Check whether we already have Gmail scopes
    need_reauth = creds is None or not set(GMAIL_SCOPES).issubset(set(existing_scopes))

    if creds and not need_reauth and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            need_reauth = True

    if need_reauth or (creds and not creds.valid):
        if not CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"Google credentials file not found at {CREDENTIALS_PATH}. "
                "Download it from Google Cloud Console."
            )

        # Merge Gmail scopes with any existing scopes (Calendar, etc.)
        merged_scopes = list(set(existing_scopes) | set(GMAIL_SCOPES))

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), merged_scopes
        )
        creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _build_service():
    from googleapiclient.discovery import build
    creds = _get_credentials()
    return build("gmail", "v1", credentials=creds)


def _decode_body(payload: dict) -> str:
    """Recursively extract and decode the text body from a Gmail message payload."""
    import base64

    mime = payload.get("mimeType", "")

    # Simple single-part
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart — recurse into parts
    parts = payload.get("parts", [])
    # Prefer text/plain over text/html
    for preferred in ("text/plain", "text/html"):
        for part in parts:
            if part.get("mimeType") == preferred:
                data = part.get("body", {}).get("data", "")
                if data:
                    decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    if preferred == "text/html":
                        # Strip HTML tags for a readable result
                        import re
                        decoded = re.sub(r"<[^>]+>", "", decoded)
                        decoded = re.sub(r"\s+", " ", decoded).strip()
                    return decoded
            # Nested multipart
            nested = _decode_body(part)
            if nested:
                return nested

    return "(Could not extract message body)"


def _header(headers: list, name: str) -> str:
    """Extract a header value by name from Gmail message headers."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ── Individual features ──────────────────────────────────────────────────────

def _get_inbox(parameters: dict, player=None) -> str:
    count = int(parameters.get("count", 10))
    count = max(1, min(count, 50))

    service = _build_service()
    try:
        result = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=count,
        ).execute()
    except Exception as e:
        return f"Failed to fetch inbox: {e}"

    messages = result.get("messages", [])
    if not messages:
        return "Inbox is empty."

    lines = [f"Recent emails ({len(messages)}):"]
    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers  = msg.get("payload", {}).get("headers", [])
            sender   = _header(headers, "From")
            subject  = _header(headers, "Subject") or "(No subject)"
            date     = _header(headers, "Date")
            snippet  = msg.get("snippet", "")[:80]
            unread   = "UNREAD" in msg.get("labelIds", [])
            flag     = "*" if unread else " "

            lines.append(f"  {flag} [{msg_ref['id']}] {subject}")
            lines.append(f"    From: {sender}  |  {date}")
            if snippet:
                lines.append(f"    {snippet}")
        except Exception:
            lines.append(f"  - [ID: {msg_ref['id']}] (could not load details)")

    return "\n".join(lines)


def _unread_count(parameters: dict, player=None) -> str:
    service = _build_service()
    try:
        result = service.users().messages().list(
            userId="me",
            labelIds=["INBOX", "UNREAD"],
            maxResults=1,
        ).execute()
    except Exception as e:
        return f"Failed to check unread count: {e}"

    total = result.get("resultSizeEstimate", 0)
    if total == 0:
        return "No unread emails."
    return f"You have approximately {total} unread email(s)."


def _read_email(parameters: dict, player=None) -> str:
    message_id = parameters.get("message_id", "").strip()
    if not message_id:
        return "Please provide a message_id."

    service = _build_service()
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full",
        ).execute()
    except Exception as e:
        return f"Failed to read email: {e}"

    headers = msg.get("payload", {}).get("headers", [])
    sender  = _header(headers, "From")
    to      = _header(headers, "To")
    subject = _header(headers, "Subject") or "(No subject)"
    date    = _header(headers, "Date")
    body    = _decode_body(msg.get("payload", {}))

    # Truncate very long bodies
    if len(body) > 3000:
        body = body[:2997] + "..."

    lines = [
        f"Subject: {subject}",
        f"From:    {sender}",
        f"To:      {to}",
        f"Date:    {date}",
        "",
        body,
    ]
    return "\n".join(lines)


def _search(parameters: dict, player=None) -> str:
    query = parameters.get("query", "").strip()
    if not query:
        return "Please provide a Gmail search query."

    count = int(parameters.get("count", 10))
    count = max(1, min(count, 50))

    service = _build_service()
    try:
        result = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=count,
        ).execute()
    except Exception as e:
        return f"Gmail search failed: {e}"

    messages = result.get("messages", [])
    if not messages:
        return f"No emails matching '{query}'."

    lines = [f"Search results for '{query}' ({len(messages)} found):"]
    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = msg.get("payload", {}).get("headers", [])
            subject = _header(headers, "Subject") or "(No subject)"
            sender  = _header(headers, "From")
            date    = _header(headers, "Date")
            lines.append(f"  [{msg_ref['id']}] {subject}")
            lines.append(f"    From: {sender}  |  {date}")
        except Exception:
            lines.append(f"  [ID: {msg_ref['id']}] (could not load details)")

    return "\n".join(lines)


def _create_draft(parameters: dict, player=None) -> str:
    """Create a DRAFT email.  NEVER sends — gmail.modify scope cannot send."""
    to      = parameters.get("to", "").strip()
    subject = parameters.get("subject", "").strip()
    body    = parameters.get("body", "").strip()

    if not to:
        return "Please provide a recipient (to)."
    if not subject:
        return "Please provide a subject."
    if not body:
        return "Please provide a message body."

    import base64
    from email.mime.text import MIMEText

    message = MIMEText(body)
    message["to"]      = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    service = _build_service()
    try:
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
    except Exception as e:
        return f"Failed to create draft: {e}"

    draft_id = draft.get("id", "?")
    return (
        f"Draft created successfully (ID: {draft_id}). "
        f"To: {to}, Subject: {subject}. "
        "The draft is saved in Gmail — open Gmail to review and send it."
    )


def _list_labels(parameters: dict, player=None) -> str:
    service = _build_service()
    try:
        result = service.users().labels().list(userId="me").execute()
    except Exception as e:
        return f"Failed to list labels: {e}"

    labels = result.get("labels", [])
    if not labels:
        return "No labels found."

    lines = ["Gmail labels:"]
    for label in sorted(labels, key=lambda l: l.get("name", "")):
        name     = label.get("name", "?")
        label_id = label.get("id", "?")
        ltype    = label.get("type", "user")
        lines.append(f"  - {name}  (ID: {label_id}, type: {ltype})")

    return "\n".join(lines)


# ── Action router ────────────────────────────────────────────────────────────
_ACTIONS = {
    "get_inbox":     _get_inbox,
    "unread_count":  _unread_count,
    "read_email":    _read_email,
    "search":        _search,
    "create_draft":  _create_draft,
    "list_labels":   _list_labels,
}


def gmail_control(parameters: dict, player=None) -> str:
    """
    Main entry point.  Routes on ``parameters["action"]``:

        get_inbox | unread_count | read_email | search | create_draft | list_labels

    SAFETY: Uses ``gmail.modify`` scope — the API cannot send mail.
    Drafts are created only; the user must open Gmail to send.
    """
    if not _is_enabled():
        return (
            "Gmail is not enabled. "
            "Set \"gmail_enabled\": true in config/api_keys.json."
        )

    params = parameters or {}
    action = params.get("action", "").strip().lower()

    handler = _ACTIONS.get(action)
    if handler is None:
        available = ", ".join(sorted(_ACTIONS))
        return f"Unknown gmail_control action '{action}'. Available: {available}"

    try:
        return handler(params, player)
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        _log(f"Action '{action}' failed: {e}", player)
        return f"Gmail request failed: {e}"
