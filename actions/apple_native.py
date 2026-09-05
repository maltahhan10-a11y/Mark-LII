# apple_native.py
# Apple Calendar.app + Mail.app integration via AppleScript (macOS only).
# Each public helper takes a `parameters` dict and optional `player` kwarg,
# returning a human-readable string result.
#
# SAFETY INVARIANT: mail_send_draft creates DRAFTS ONLY — it never sends email.

import subprocess
import platform
from datetime import datetime, timedelta


_OS = platform.system()


def _escape_applescript(text: str) -> str:
    """Escape a string for safe embedding inside AppleScript double quotes."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str, timeout: int = 30) -> tuple[bool, str]:
    """Run an AppleScript snippet and return (success, output_or_error)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or "Unknown AppleScript error"
            return False, err
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"AppleScript timed out after {timeout} seconds."
    except Exception as e:
        return False, f"Failed to run AppleScript: {e}"


def _check_macos() -> str | None:
    """Return an error message if not on macOS, otherwise None."""
    if _OS != "Darwin":
        return "Apple Calendar and Mail are only available on macOS."
    return None


# ===========================================================================
# Calendar actions
# ===========================================================================

def calendar_get_events(days_ahead: int = 7) -> str:
    err = _check_macos()
    if err:
        return err

    days_ahead = max(1, min(365, int(days_ahead)))
    script = f'''
set startDate to current date
set endDate to startDate + ({days_ahead} * days)

tell application "Calendar"
    set output to ""
    set eventCount to 0
    repeat with cal in calendars
        set calName to name of cal
        try
            set eventList to (every event of cal whose start date >= startDate and start date <= endDate)
            repeat with evt in eventList
                set evtTitle to summary of evt
                set evtStart to start date of evt
                set evtEnd to end date of evt
                set evtLoc to ""
                try
                    set evtLoc to location of evt
                end try
                set output to output & evtTitle & " | " & evtStart & " - " & evtEnd & " | " & calName
                if evtLoc is not "" and evtLoc is not missing value then
                    set output to output & " | " & evtLoc
                end if
                set output to output & linefeed
                set eventCount to eventCount + 1
                if eventCount >= 50 then exit repeat
            end repeat
        end try
        if eventCount >= 50 then exit repeat
    end repeat
    if eventCount = 0 then
        return "No events found in the next {days_ahead} day(s)."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script, timeout=60)
    if not ok:
        return f"Failed to get events: {out}"
    return out


def calendar_create_event(
    title: str,
    start_date: str,
    end_date: str,
    calendar_name: str | None = None,
    location: str | None = None,
    notes: str | None = None,
) -> str:
    err = _check_macos()
    if err:
        return err

    if not title:
        return "Please provide an event title."
    if not start_date or not end_date:
        return "Please provide both start_date and end_date (format: YYYY-MM-DD HH:MM)."

    safe_title = _escape_applescript(title)
    safe_start = _escape_applescript(start_date)
    safe_end = _escape_applescript(end_date)
    safe_location = _escape_applescript(location or "")
    safe_notes = _escape_applescript(notes or "")

    # Build the calendar target
    if calendar_name:
        safe_cal = _escape_applescript(calendar_name)
        cal_target = f'''
        set targetCal to missing value
        repeat with cal in calendars
            if name of cal is "{safe_cal}" then
                set targetCal to cal
                exit repeat
            end if
        end repeat
        if targetCal is missing value then
            return "Calendar \\"{safe_cal}\\" not found."
        end if
'''
    else:
        cal_target = '''
        set targetCal to default calendar
'''

    props = f'summary:"{safe_title}", start date:date "{safe_start}", end date:date "{safe_end}"'
    if location:
        props += f', location:"{safe_location}"'
    if notes:
        props += f', description:"{safe_notes}"'

    script = f'''
tell application "Calendar"
{cal_target}
    tell targetCal
        make new event with properties {{{props}}}
    end tell
    return "Event \\"{safe_title}\\" created."
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to create event: {out}"
    return out


def calendar_search(query: str) -> str:
    err = _check_macos()
    if err:
        return err

    if not query:
        return "Please provide a search query."

    safe_query = _escape_applescript(query)
    script = f'''
tell application "Calendar"
    set output to ""
    set matchCount to 0
    repeat with cal in calendars
        set calName to name of cal
        try
            repeat with evt in (every event of cal)
                try
                    if summary of evt contains "{safe_query}" then
                        set output to output & summary of evt & " | " & start date of evt & " | " & calName & linefeed
                        set matchCount to matchCount + 1
                        if matchCount >= 20 then exit repeat
                    end if
                end try
            end repeat
        end try
        if matchCount >= 20 then exit repeat
    end repeat
    if matchCount = 0 then
        return "No events matching \\"{safe_query}\\" found."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script, timeout=60)
    if not ok:
        return f"Calendar search failed: {out}"
    return out


# ===========================================================================
# Mail actions
# ===========================================================================

def mail_get_recent(count: int = 10) -> str:
    err = _check_macos()
    if err:
        return err

    count = max(1, min(50, int(count)))
    script = f'''
tell application "Mail"
    set output to ""
    set msgCount to 0
    set inboxMessages to messages of inbox
    set maxCount to {count}
    if (count of inboxMessages) < maxCount then set maxCount to (count of inboxMessages)
    repeat with i from 1 to maxCount
        set msg to item i of inboxMessages
        set msgSender to sender of msg
        set msgSubject to subject of msg
        set msgDate to date received of msg
        set output to output & msgSubject & " | " & msgSender & " | " & msgDate & linefeed
        set msgCount to msgCount + 1
    end repeat
    if msgCount = 0 then
        return "No messages found in inbox."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script, timeout=30)
    if not ok:
        return f"Failed to get recent mail: {out}"
    return out


def mail_unread_count() -> str:
    err = _check_macos()
    if err:
        return err

    script = '''
tell application "Mail"
    set unreadCount to unread count of inbox
    return "Unread messages: " & unreadCount
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to get unread count: {out}"
    return out


def mail_search(query: str) -> str:
    err = _check_macos()
    if err:
        return err

    if not query:
        return "Please provide a search query."

    safe_query = _escape_applescript(query)
    script = f'''
tell application "Mail"
    set output to ""
    set matchCount to 0
    set inboxMessages to messages of inbox
    repeat with msg in inboxMessages
        try
            if (subject of msg contains "{safe_query}") or (sender of msg contains "{safe_query}") then
                set output to output & subject of msg & " | " & sender of msg & " | " & date received of msg & linefeed
                set matchCount to matchCount + 1
                if matchCount >= 20 then exit repeat
            end if
        end try
    end repeat
    if matchCount = 0 then
        return "No emails matching \\"{safe_query}\\" found."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script, timeout=60)
    if not ok:
        return f"Mail search failed: {out}"
    return out


def mail_send_draft(to: str, subject: str, body: str) -> str:
    """Create a DRAFT email in Mail.app.

    *** SAFETY: This function ONLY creates a draft. It NEVER sends the email. ***
    The draft is saved to the Drafts mailbox for the user to review and send
    manually.
    """
    err = _check_macos()
    if err:
        return err

    if not to:
        return "Please provide a recipient email address."
    if not subject:
        return "Please provide a subject."

    safe_to = _escape_applescript(to)
    safe_subject = _escape_applescript(subject)
    safe_body = _escape_applescript(body or "")

    # CRITICAL SAFETY: visible is false, the message is NOT sent.
    # We explicitly do NOT call "send" on the outgoing message.
    script = f'''
tell application "Mail"
    set newMessage to make new outgoing message with properties {{subject:"{safe_subject}", content:"{safe_body}", visible:false}}
    tell newMessage
        make new to recipient at end of to recipients with properties {{address:"{safe_to}"}}
    end tell
    -- SAFETY: Save as draft, do NOT send.
    save newMessage
    return "Draft created (NOT sent) — To: {safe_to} | Subject: {safe_subject}"
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to create draft: {out}"
    return out


def mail_list_mailboxes() -> str:
    err = _check_macos()
    if err:
        return err

    script = '''
tell application "Mail"
    set output to ""
    repeat with acct in accounts
        set acctName to name of acct
        repeat with mb in mailboxes of acct
            set output to output & acctName & " / " & name of mb & linefeed
        end repeat
    end repeat
    if output is "" then
        return "No mailboxes found."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script, timeout=30)
    if not ok:
        return f"Failed to list mailboxes: {out}"
    return out


# ===========================================================================
# Main entry point — routes on parameters["action"]
# ===========================================================================

_ACTION_MAP = {
    "calendar_events":  "calendar_events",
    "calendar_create":  "calendar_create",
    "calendar_search":  "calendar_search",
    "mail_recent":      "mail_recent",
    "mail_unread":      "mail_unread",
    "mail_search":      "mail_search",
    "mail_draft":       "mail_draft",
    "mail_mailboxes":   "mail_mailboxes",
}


def apple_native(parameters: dict, player=None) -> str:
    params = parameters or {}
    action = (params.get("action", "") or "").strip().lower().replace("-", "_").replace(" ", "_")

    if player:
        player.write_log(f"[AppleNative] {action}")

    if action == "calendar_events":
        return calendar_get_events(
            days_ahead=int(params.get("days_ahead", 7)),
        )

    elif action == "calendar_create":
        return calendar_create_event(
            title=params.get("title", ""),
            start_date=params.get("start_date", ""),
            end_date=params.get("end_date", ""),
            calendar_name=params.get("calendar_name"),
            location=params.get("location"),
            notes=params.get("notes"),
        )

    elif action == "calendar_search":
        return calendar_search(query=params.get("query", ""))

    elif action == "mail_recent":
        return mail_get_recent(count=int(params.get("count", 10)))

    elif action == "mail_unread":
        return mail_unread_count()

    elif action == "mail_search":
        return mail_search(query=params.get("query", ""))

    elif action == "mail_draft":
        return mail_send_draft(
            to=params.get("to", ""),
            subject=params.get("subject", ""),
            body=params.get("body", ""),
        )

    elif action == "mail_mailboxes":
        return mail_list_mailboxes()

    else:
        valid = ", ".join(sorted(_ACTION_MAP.keys()))
        return (
            f"Unknown apple_native action: '{action}'. "
            f"Valid actions: {valid}."
        )
