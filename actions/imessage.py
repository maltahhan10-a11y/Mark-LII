"""
iMessage integration for Mark-LII via AppleScript.

Sends and reads iMessages through Messages.app on macOS.
Can monitor for replies from specific contacts.
No API keys needed — uses native macOS AppleScript.

Contact resolution: looks up names in config/contacts.json first,
then falls back to macOS Contacts.app to find phone/email by name.
"""

import platform
import subprocess
import json
import time
import threading
from pathlib import Path
from datetime import datetime

_CONTACTS_PATH = Path(__file__).resolve().parent.parent / "config" / "contacts.json"


def _check_macos() -> str | None:
    if platform.system() != "Darwin":
        return "iMessage is only available on macOS."
    return None


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return True, r.stdout.strip()
        return False, r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out."
    except Exception as e:
        return False, str(e)


# ── Contact resolution ──────────────────────────────────────────────────────

def _load_contacts() -> dict[str, str]:
    try:
        if _CONTACTS_PATH.exists():
            data = json.loads(_CONTACTS_PATH.read_text())
            if isinstance(data, dict):
                return {k.lower(): v for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_contacts(contacts: dict[str, str]) -> None:
    _CONTACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONTACTS_PATH.write_text(json.dumps(contacts, indent=4))


def _lookup_macos_contacts(name: str) -> str | None:
    """Search macOS Contacts.app for a phone number by name."""
    safe_name = _escape(name)
    script = f'''
        tell application "Contacts"
            set matchedPeople to people whose name contains "{safe_name}"
            if (count of matchedPeople) > 0 then
                set p to item 1 of matchedPeople
                if (count of phones of p) > 0 then
                    return value of phone 1 of p
                else if (count of emails of p) > 0 then
                    return value of email 1 of p
                end if
            end if
        end tell
    '''
    ok, result = _run_applescript(script, timeout=10)
    if ok and result:
        return result.strip()
    return None


def _is_phone_or_email(s: str) -> bool:
    s = s.strip()
    if "@" in s:
        return True
    digits = sum(1 for c in s if c.isdigit())
    if digits >= 7:
        return True
    return False


def _resolve_contact(name_or_id: str) -> str:
    """
    Resolve a contact name to a phone number or email address.
    Priority: 1) already a phone/email → use as-is
              2) config/contacts.json lookup
              3) macOS Contacts.app lookup
              4) return original (will likely fail)
    """
    if not name_or_id:
        return name_or_id

    raw = name_or_id.strip()

    if _is_phone_or_email(raw):
        return raw

    contacts = _load_contacts()
    mapped = contacts.get(raw.lower())
    if mapped:
        return mapped

    macos_result = _lookup_macos_contacts(raw)
    if macos_result:
        contacts[raw.lower()] = macos_result
        try:
            original = {}
            if _CONTACTS_PATH.exists():
                original = json.loads(_CONTACTS_PATH.read_text())
            original[raw] = macos_result
            _save_contacts(original)
        except Exception:
            pass
        return macos_result

    return raw


# ── Contact management ──────────────────────────────────────────────────────

def _add_contact(name: str, identifier: str) -> str:
    if not name or not identifier:
        return "Both name and phone/email are required."

    try:
        contacts = {}
        if _CONTACTS_PATH.exists():
            contacts = json.loads(_CONTACTS_PATH.read_text())
        contacts[name.strip()] = identifier.strip()
        _save_contacts(contacts)
        return f"Contact saved: {name.strip()} → {identifier.strip()}"
    except Exception as e:
        return f"Failed to save contact: {e}"


def _remove_contact(name: str) -> str:
    if not name:
        return "Please specify the contact name to remove."

    try:
        if not _CONTACTS_PATH.exists():
            return f"Contact '{name}' not found."
        contacts = json.loads(_CONTACTS_PATH.read_text())
        key_to_remove = None
        for k in contacts:
            if k.lower() == name.strip().lower():
                key_to_remove = k
                break
        if not key_to_remove:
            return f"Contact '{name}' not found."
        del contacts[key_to_remove]
        _save_contacts(contacts)
        return f"Contact '{key_to_remove}' removed."
    except Exception as e:
        return f"Failed to remove contact: {e}"


def _list_contacts_action() -> str:
    try:
        if not _CONTACTS_PATH.exists():
            return "No saved contacts. Use add_contact to add name→phone mappings."
        contacts = json.loads(_CONTACTS_PATH.read_text())
        if not contacts:
            return "No saved contacts. Use add_contact to add name→phone mappings."
        lines = ["Saved contacts:"]
        for name, ident in contacts.items():
            lines.append(f"  • {name} → {ident}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not load contacts: {e}"


# ── Send ─────────────────────────────────────────────────────────────────────

def _send_imessage(to: str, message: str) -> str:
    err = _check_macos()
    if err:
        return err

    if not to or not message:
        return "Both recipient and message are required."

    resolved = _resolve_contact(to)
    safe_to = _escape(resolved)
    safe_msg = _escape(message)

    script = f'''
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to buddy "{safe_to}" of targetService
            send "{safe_msg}" to targetBuddy
        end tell
    '''

    ok, result = _run_applescript(script)
    if ok:
        note = f" (resolved {to} → {resolved})" if resolved != to else ""
        return f"iMessage sent to {to}{note}."

    script2 = f'''
        tell application "Messages"
            send "{safe_msg}" to buddy "{safe_to}" of (service 1 whose service type is iMessage)
        end tell
    '''
    ok2, result2 = _run_applescript(script2)
    if ok2:
        note = f" (resolved {to} → {resolved})" if resolved != to else ""
        return f"iMessage sent to {to}{note}."

    if resolved == to:
        return (
            f"Could not send iMessage to {to}. "
            "I couldn't find a phone number for this name. "
            "Use add_contact to save their number: "
            "action='add_contact', name='Name', identifier='+1234567890'"
        )

    return f"Could not send iMessage to {to} ({resolved}). Error: {result}"


# ── Read recent messages ─────────────────────────────────────────────────────

def _get_recent_messages(contact: str | None = None, count: int = 10) -> str:
    err = _check_macos()
    if err:
        return err

    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return "Messages database not found. Make sure Messages.app has been used on this Mac."

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        if contact:
            resolved = _resolve_contact(contact)
            safe_contact = resolved.replace("'", "''")
            query = """
                SELECT
                    m.text,
                    m.is_from_me,
                    datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as msg_date,
                    h.id as handle_id
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE h.id LIKE ? OR h.id LIKE ?
                ORDER BY m.date DESC
                LIMIT ?
            """
            rows = conn.execute(
                query,
                (f"%{safe_contact}%", f"%{safe_contact}%", count)
            ).fetchall()
        else:
            query = """
                SELECT
                    m.text,
                    m.is_from_me,
                    datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as msg_date,
                    h.id as handle_id
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.text IS NOT NULL AND m.text != ''
                ORDER BY m.date DESC
                LIMIT ?
            """
            rows = conn.execute(query, (count,)).fetchall()

        conn.close()

        if not rows:
            if contact:
                return f"No messages found with {contact}."
            return "No recent messages found."

        contacts_map = _load_contacts()
        reverse_map = {v.lower(): k for k, v in contacts_map.items()}

        lines = []
        for row in reversed(rows):
            handle = row["handle_id"] or "Unknown"
            if row["is_from_me"]:
                sender = "You"
            else:
                sender = reverse_map.get(handle.lower(), handle)
            text = row["text"] or "(attachment)"
            date = row["msg_date"] or ""
            lines.append(f"[{date}] {sender}: {text}")

        header = f"Messages with {contact}" if contact else "Recent messages"
        return f"{header}:\n" + "\n".join(lines)

    except Exception as e:
        return f"Could not read messages: {e}"


# ── Get unread / latest from a contact ───────────────────────────────────────

def _get_latest_from(contact: str) -> str:
    err = _check_macos()
    if err:
        return err

    if not contact:
        return "Please specify a contact name, phone number, or email."

    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return "Messages database not found."

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        resolved = _resolve_contact(contact)
        safe_contact = resolved.replace("'", "''")
        query = """
            SELECT
                m.text,
                m.is_from_me,
                datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as msg_date,
                m.date as raw_date,
                h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE (h.id LIKE ? OR h.id LIKE ?)
                AND m.is_from_me = 0
                AND m.text IS NOT NULL AND m.text != ''
            ORDER BY m.date DESC
            LIMIT 1
        """
        row = conn.execute(
            query, (f"%{safe_contact}%", f"%{safe_contact}%")
        ).fetchone()
        conn.close()

        if not row:
            return f"No messages found from {contact}."

        return (
            f"Latest message from {contact}:\n"
            f"[{row['msg_date']}] {row['text']}"
        )
    except Exception as e:
        return f"Could not check messages: {e}"


# ── Watch for a reply ────────────────────────────────────────────────────────

_active_watches: dict[str, dict] = {}
_watch_lock = threading.Lock()


def _start_watch(contact: str, player=None) -> str:
    err = _check_macos()
    if err:
        return err

    if not contact:
        return "Please specify a contact to watch for."

    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return "Messages database not found."

    resolved = _resolve_contact(contact)
    contact_lower = contact.lower()

    with _watch_lock:
        if contact_lower in _active_watches:
            return f"Already watching for messages from {contact}."

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        safe_contact = resolved.replace("'", "''")
        row = conn.execute(
            """
            SELECT MAX(m.date) as latest
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE (h.id LIKE ? OR h.id LIKE ?)
                AND m.is_from_me = 0
            """,
            (f"%{safe_contact}%", f"%{safe_contact}%")
        ).fetchone()
        conn.close()
        baseline = row[0] if row and row[0] else 0
    except Exception as e:
        return f"Could not set up watch: {e}"

    with _watch_lock:
        _active_watches[contact_lower] = {
            "contact": contact,
            "resolved": resolved,
            "baseline": baseline,
            "started": datetime.now().isoformat(),
            "active": True,
        }

    def _poll():
        import sqlite3 as _sql
        while True:
            time.sleep(10)
            with _watch_lock:
                info = _active_watches.get(contact_lower)
                if not info or not info["active"]:
                    return

            try:
                conn = _sql.connect(str(db_path))
                conn.row_factory = _sql.Row
                sc = resolved.replace("'", "''")
                rows = conn.execute(
                    """
                    SELECT
                        m.text,
                        m.date as raw_date,
                        datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as msg_date,
                        h.id as handle_id
                    FROM message m
                    LEFT JOIN handle h ON m.handle_id = h.ROWID
                    WHERE (h.id LIKE ? OR h.id LIKE ?)
                        AND m.is_from_me = 0
                        AND m.date > ?
                        AND m.text IS NOT NULL AND m.text != ''
                    ORDER BY m.date ASC
                    """,
                    (f"%{sc}%", f"%{sc}%", info["baseline"])
                ).fetchall()
                conn.close()

                if rows:
                    with _watch_lock:
                        _active_watches[contact_lower]["baseline"] = rows[-1]["raw_date"]

                    for row in rows:
                        msg = f"New message from {contact}: {row['text']}"
                        print(f"[iMessage] {msg}")
                        if player:
                            try:
                                player.write_log(f"JARVIS: {msg}")
                            except Exception:
                                pass
            except Exception as e:
                print(f"[iMessage] Watch error: {e}")

    thread = threading.Thread(target=_poll, daemon=True, name=f"imessage-watch-{contact_lower}")
    thread.start()

    note = f" (resolved → {resolved})" if resolved != contact else ""
    return f"Now watching for new messages from {contact}{note}. I'll notify you when they respond."


def _stop_watch(contact: str) -> str:
    if not contact:
        return "Please specify which contact to stop watching."

    contact_lower = contact.lower()
    with _watch_lock:
        info = _active_watches.get(contact_lower)
        if not info:
            return f"Not currently watching for messages from {contact}."
        info["active"] = False
        del _active_watches[contact_lower]

    return f"Stopped watching for messages from {contact}."


def _list_watches() -> str:
    with _watch_lock:
        if not _active_watches:
            return "No active message watches."
        lines = ["Active message watches:"]
        for key, info in _active_watches.items():
            lines.append(f"  - {info['contact']} (since {info['started']})")
        return "\n".join(lines)


# ── List conversations ───────────────────────────────────────────────────────

def _list_conversations(count: int = 15) -> str:
    err = _check_macos()
    if err:
        return err

    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return "Messages database not found."

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT
                h.id as contact,
                MAX(datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime')) as last_date,
                COUNT(*) as msg_count
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE h.id IS NOT NULL
            GROUP BY h.id
            ORDER BY MAX(m.date) DESC
            LIMIT ?
            """,
            (count,)
        ).fetchall()
        conn.close()

        if not rows:
            return "No conversations found."

        contacts_map = _load_contacts()
        reverse_map = {v.lower(): k for k, v in contacts_map.items()}

        lines = ["Recent conversations:"]
        for i, row in enumerate(rows, 1):
            handle = row["contact"]
            display = reverse_map.get(handle.lower(), handle)
            lines.append(f"  {i}. {display} ({handle}) — {row['msg_count']} messages, last: {row['last_date']}")
        return "\n".join(lines)

    except Exception as e:
        return f"Could not list conversations: {e}"


# ── Main entry point ─────────────────────────────────────────────────────────

def imessage(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "").strip().lower()

    _ACTIONS = {
        "send":            lambda: _send_imessage(
            parameters.get("to", ""), parameters.get("message", "")
        ),
        "read":            lambda: _get_recent_messages(
            parameters.get("contact"), int(parameters.get("count", 10))
        ),
        "latest":          lambda: _get_latest_from(
            parameters.get("contact", "")
        ),
        "conversations":   lambda: _list_conversations(
            int(parameters.get("count", 15))
        ),
        "watch":           lambda: _start_watch(
            parameters.get("contact", ""), player
        ),
        "stop_watch":      lambda: _stop_watch(
            parameters.get("contact", "")
        ),
        "list_watches":    lambda: _list_watches(),
        "add_contact":     lambda: _add_contact(
            parameters.get("name", ""), parameters.get("identifier", "")
        ),
        "remove_contact":  lambda: _remove_contact(
            parameters.get("name", "")
        ),
        "list_contacts":   lambda: _list_contacts_action(),
    }

    handler = _ACTIONS.get(action)
    if not handler:
        return (
            f"Unknown iMessage action: '{action}'. "
            "Available: send | read | latest | conversations | watch | stop_watch | "
            "list_watches | add_contact | remove_contact | list_contacts"
        )

    try:
        return handler()
    except Exception as e:
        return f"iMessage error: {e}"
