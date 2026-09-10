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


# ── Messages database access ────────────────────────────────────────────────
#
# Everything that reads message history goes through chat.db, and macOS puts
# that file behind Full Disk Access. The file is mode 644 and plainly readable
# by the user, but the kernel refuses the open anyway, and sqlite reports it as
# the very unhelpful "unable to open database file". No amount of code gets
# around this: it is a consent decision, and only the user can make it.

DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

FULL_DISK_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_AllFilesAccess"
)


def _host_app_name() -> str:
    """Name the application the user actually has to tick in System Settings.

    Full Disk Access is granted to the app that owns the process, which is the
    terminal or editor Jarvis was launched from — not "Python". Naming the
    wrong thing sends people to tick a box that changes nothing.
    """
    try:
        import psutil

        proc = psutil.Process()
        for candidate in [proc] + proc.parents():
            exe = candidate.exe() or ""
            # The interpreter itself lives inside Python.framework's own
            # Python.app. That bundle is not something the user can grant
            # anything to, so keep walking up to the app that launched us.
            if ".framework/" in exe or ".app/Contents/MacOS/" not in exe:
                continue
            # The first .app in the path is the outermost bundle: a helper
            # process reports ".../Visual Studio Code.app/Contents/Frameworks/
            # Code Helper.app/...", and the name worth printing is the outer one.
            for part in exe.split("/"):
                if part.endswith(".app"):
                    name = part[:-4]
                    if name.lower() != "python":
                        return name
                    break
    except Exception:
        pass
    return "your terminal application"


def _full_disk_access_help() -> str:
    return (
        f"I can't read the Messages database — macOS is blocking it. "
        f"To fix it: open System Settings > Privacy & Security > Full Disk "
        f"Access, turn it on for {_host_app_name()}, then restart me. "
        f"That permission is what lets me read message history; sending "
        f"messages works without it."
    )


def _chat_db_available() -> tuple[bool, str]:
    """Can this process actually read chat.db right now?

    Tested with a plain file open rather than by trying sqlite: a TCC denial
    surfaces there as a clean PermissionError, where sqlite flattens every
    cause into one ambiguous message.
    """
    if not DB_PATH.exists():
        return False, (
            "The Messages database doesn't exist on this Mac — Messages may "
            "never have been set up."
        )
    try:
        with open(DB_PATH, "rb") as fh:
            fh.read(16)
    except PermissionError:
        return False, _full_disk_access_help()
    except OSError as exc:
        return False, f"Could not read the Messages database: {exc}"
    return True, ""


def _open_chat_db():
    """Return (connection, error). Read-only, so a live Messages app is never
    disturbed and the watch cannot lock the database against it."""
    ok, reason = _chat_db_available()
    if not ok:
        return None, reason
    try:
        import sqlite3

        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0
        )
        conn.row_factory = sqlite3.Row
        return conn, ""
    except Exception as exc:
        return None, f"Could not open the Messages database: {exc}"


def _handle_patterns(resolved: str) -> list[str]:
    """LIKE patterns that match how Messages might have stored this handle.

    A number saved as 5551234567 is stored by Messages as +15551234567, so
    matching the saved form alone finds nothing. Matching on the last ten
    digits catches every formatting of the same number without matching
    unrelated ones.
    """
    ident = (resolved or "").strip()
    if not ident:
        return []
    patterns = {f"%{ident}%"}
    if "@" in ident:
        return list(patterns)
    digits = "".join(c for c in ident if c.isdigit())
    if digits:
        patterns.add(f"%{digits}%")
        if len(digits) >= 10:
            patterns.add(f"%{digits[-10:]}%")
    return list(patterns)


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


def _normalise_identifier(identifier: str) -> str:
    """Clean up an iMessage handle before it is stored or sent to.

    An email address is a perfectly good iMessage handle, but the model tends
    to copy the "+1234567890" shape from the tool description and hand back
    "+someone@example.com". Messages will not match that against any buddy, and
    the failure is silent, so strip the phone punctuation off anything that is
    plainly an address.
    """
    ident = (identifier or "").strip()
    if not ident:
        return ident
    if "@" in ident:
        return ident.lstrip("+ ").strip().lower()
    # A phone number: keep a leading +, drop the formatting humans add.
    plus = ident.lstrip().startswith("+")
    digits = "".join(c for c in ident if c.isdigit())
    return ("+" + digits) if plus and digits else (digits or ident)


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
        return _normalise_identifier(raw)

    contacts = _load_contacts()
    mapped = contacts.get(raw.lower())
    if mapped:
        # Stored entries are normalised on the way back out too, so contacts
        # saved before this existed still resolve correctly.
        return _normalise_identifier(mapped)

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
        cleaned = _normalise_identifier(identifier)
        if not cleaned:
            return "That does not look like a phone number or email address."
        contacts[name.strip()] = cleaned
        _save_contacts(contacts)
        return f"Contact saved: {name.strip()} → {cleaned}"
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

    conn, db_err = _open_chat_db()
    if conn is None:
        return db_err

    try:
        if contact:
            resolved = _resolve_contact(contact)
            patterns = _handle_patterns(resolved)
            if not patterns:
                return f"I don't have a phone number or email for {contact}."
            where = " OR ".join("h.id LIKE ?" for _ in patterns)
            query = f"""
                SELECT
                    m.text,
                    m.is_from_me,
                    datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as msg_date,
                    h.id as handle_id
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE ({where})
                ORDER BY m.date DESC
                LIMIT ?
            """
            rows = conn.execute(query, (*patterns, count)).fetchall()
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

    conn, db_err = _open_chat_db()
    if conn is None:
        return db_err

    try:
        resolved = _resolve_contact(contact)
        patterns = _handle_patterns(resolved)
        if not patterns:
            return f"I don't have a phone number or email for {contact}."
        where = " OR ".join("h.id LIKE ?" for _ in patterns)
        query = f"""
            SELECT
                m.text,
                m.is_from_me,
                datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as msg_date,
                m.date as raw_date,
                h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE ({where})
                AND m.is_from_me = 0
                AND m.text IS NOT NULL AND m.text != ''
            ORDER BY m.date DESC
            LIMIT 1
        """
        row = conn.execute(query, tuple(patterns)).fetchone()
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
_notifier = None          # set by main.py so a watch can actually speak
POLL_SECONDS = 10


def set_notifier(fn) -> None:
    """Register how a watch announces a new message.

    Without this a watch can only write to the activity log, which is not a
    notification — the whole point of "tell me when Mimi texts" is to be told
    while looking at something else. main.py passes the assistant's own speak().
    """
    global _notifier
    _notifier = fn


def _notify(text: str, player=None) -> None:
    print(f"[iMessage] {text}")
    if player is not None:
        try:
            player.write_log(f"JARVIS: {text}")
        except Exception:
            pass
    if _notifier is not None:
        try:
            _notifier(text)
        except Exception as exc:
            print(f"[iMessage] Could not announce: {exc}")


def _latest_from_db(conn, patterns: list[str], after: int = 0):
    """Rows from `patterns` newer than `after`, oldest first."""
    if not patterns:
        return []
    where = " OR ".join("h.id LIKE ?" for _ in patterns)
    return conn.execute(
        f"""
        SELECT
            m.text,
            m.date AS raw_date,
            datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') AS msg_date,
            h.id AS handle_id
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE ({where})
            AND m.is_from_me = 0
            AND m.date > ?
            AND m.text IS NOT NULL AND m.text != ''
        ORDER BY m.date ASC
        """,
        (*patterns, after),
    ).fetchall()


def _start_watch(contact: str, player=None) -> str:
    """Watch for incoming messages from one contact.

    If Full Disk Access has not been granted the watch is still created, in a
    waiting state: the poll loop keeps checking, and the moment the permission
    appears it takes a baseline and starts reporting. That way granting access
    is all the user has to do — they do not also have to remember to come back
    and re-issue the command.
    """
    err = _check_macos()
    if err:
        return err

    if not contact:
        return "Who should I watch for?"

    resolved = _resolve_contact(contact)
    contact_lower = contact.lower()
    patterns = _handle_patterns(resolved)

    if not patterns or not _is_phone_or_email(resolved):
        # An unresolved name would arm a watch against a handle that cannot
        # exist, and it would sit there looking healthy forever.
        return (
            f"I don't have a phone number or email for {contact}, so there's "
            f"nothing to watch. Add one first — say something like: "
            f"add {contact} to my contacts as their number or email."
        )

    with _watch_lock:
        if contact_lower in _active_watches:
            return f"Already watching for messages from {contact}."

    conn, db_err = _open_chat_db()
    baseline, pending = 0, False
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT MAX(m.date) FROM message m "
                "LEFT JOIN handle h ON m.handle_id = h.ROWID "
                f"WHERE ({' OR '.join('h.id LIKE ?' for _ in patterns)}) "
                "AND m.is_from_me = 0",
                tuple(patterns),
            ).fetchone()
            baseline = (row[0] if row and row[0] else 0)
        except Exception as exc:
            conn.close()
            return f"Could not set up the watch: {exc}"
        conn.close()
    else:
        pending = True

    with _watch_lock:
        _active_watches[contact_lower] = {
            "contact": contact,
            "resolved": resolved,
            "patterns": patterns,
            "baseline": baseline,
            "started": datetime.now().isoformat(),
            "active": True,
            "pending": pending,
        }

    def _poll():
        while True:
            time.sleep(POLL_SECONDS)
            with _watch_lock:
                info = _active_watches.get(contact_lower)
                if not info or not info["active"]:
                    return
                waiting = info["pending"]

            conn, why = _open_chat_db()
            if conn is None:
                continue          # still no access — keep waiting quietly

            try:
                if waiting:
                    # Access has just appeared. Baseline on the newest existing
                    # message so the backlog is not announced as if it were new.
                    row = conn.execute(
                        "SELECT MAX(m.date) FROM message m "
                        "LEFT JOIN handle h ON m.handle_id = h.ROWID "
                        f"WHERE ({' OR '.join('h.id LIKE ?' for _ in patterns)}) "
                        "AND m.is_from_me = 0",
                        tuple(patterns),
                    ).fetchone()
                    with _watch_lock:
                        if contact_lower not in _active_watches:
                            conn.close()
                            return
                        _active_watches[contact_lower]["baseline"] = (
                            row[0] if row and row[0] else 0
                        )
                        _active_watches[contact_lower]["pending"] = False
                    conn.close()
                    _notify(
                        f"I can read Messages now — watching for texts from {contact}.",
                        player,
                    )
                    continue

                rows = _latest_from_db(conn, patterns, info["baseline"])
                conn.close()

                if rows:
                    with _watch_lock:
                        if contact_lower in _active_watches:
                            _active_watches[contact_lower]["baseline"] = rows[-1]["raw_date"]
                    for row in rows:
                        _notify(
                            f"New message from {contact}: {row['text']}", player
                        )
            except Exception as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                print(f"[iMessage] Watch error: {exc}")

    thread = threading.Thread(
        target=_poll, daemon=True, name=f"imessage-watch-{contact_lower}"
    )
    thread.start()

    note = f" (resolved → {resolved})" if resolved != contact else ""
    if pending:
        return (
            f"Watch armed for {contact}{note}, but I can't read Messages yet. "
            f"{db_err} I'll start reporting the moment that's granted — "
            "you won't need to ask again."
        )
    return (
        f"Now watching for new messages from {contact}{note}. "
        "I'll tell you when they text."
    )


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


def _check_access(open_settings: bool = False) -> str:
    """Report whether message history is readable, and offer the fix."""
    err = _check_macos()
    if err:
        return err

    ok, reason = _chat_db_available()
    if ok:
        return (
            "I can read the Messages database — reading history and watching "
            "for texts both work."
        )
    if open_settings:
        try:
            subprocess.run(
                ["open", FULL_DISK_SETTINGS_URL], capture_output=True, timeout=10
            )
            return reason + " I've opened that settings pane for you."
        except Exception:
            pass
    return reason


def _list_watches() -> str:
    with _watch_lock:
        if not _active_watches:
            return "No active message watches."
        lines = ["Active message watches:"]
        for _key, info in _active_watches.items():
            state = " — waiting for Full Disk Access" if info.get("pending") else ""
            lines.append(f"  - {info['contact']} (since {info['started']}){state}")
        return "\n".join(lines)


# ── List conversations ───────────────────────────────────────────────────────

def _list_conversations(count: int = 15) -> str:
    err = _check_macos()
    if err:
        return err

    conn, db_err = _open_chat_db()
    if conn is None:
        return db_err

    try:
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
    action = action.replace("-", "_").replace(" ", "_")
    action = {
        "notify": "watch", "notify_me": "watch", "watch_for": "watch",
        "unwatch": "stop_watch", "stop": "stop_watch",
        "permissions": "check_access", "check_permissions": "check_access",
        "diagnose": "check_access",
    }.get(action, action)

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
        "check_access":    lambda: _check_access(
            bool(parameters.get("open_settings", False))
        ),
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
            "list_watches | check_access | add_contact | remove_contact | list_contacts"
        )

    try:
        return handler()
    except Exception as e:
        return f"iMessage error: {e}"
