# apple_notes.py
# Apple Notes integration via AppleScript (macOS only).
# Each public helper takes a `parameters` dict and optional `player` kwarg,
# returning a human-readable string result.

import subprocess
import platform


_OS = platform.system()


def _escape_applescript(text: str) -> str:
    """Escape a string for safe embedding inside AppleScript double quotes."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str) -> tuple[bool, str]:
    """Run an AppleScript snippet and return (success, output_or_error)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or "Unknown AppleScript error"
            return False, err
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out after 30 seconds."
    except Exception as e:
        return False, f"Failed to run AppleScript: {e}"


def _check_macos() -> str | None:
    """Return an error message if not on macOS, otherwise None."""
    if _OS != "Darwin":
        return "Apple Notes is only available on macOS."
    return None


# ---------------------------------------------------------------------------
# Individual actions
# ---------------------------------------------------------------------------

def list_notes(folder: str | None = None, limit: int = 10) -> str:
    err = _check_macos()
    if err:
        return err

    limit = max(1, min(100, int(limit)))

    if folder:
        safe_folder = _escape_applescript(folder)
        script = f'''
tell application "Notes"
    try
        set theFolder to folder "{safe_folder}"
        set noteList to notes of theFolder
    on error
        return "Error: Folder \\"{safe_folder}\\" not found."
    end try
    set output to ""
    set maxCount to {limit}
    if (count of noteList) < maxCount then set maxCount to (count of noteList)
    repeat with i from 1 to maxCount
        set n to item i of noteList
        set output to output & name of n & " | " & modification date of n & linefeed
    end repeat
    return output
end tell
'''
    else:
        script = f'''
tell application "Notes"
    set noteList to notes
    set output to ""
    set maxCount to {limit}
    if (count of noteList) < maxCount then set maxCount to (count of noteList)
    repeat with i from 1 to maxCount
        set n to item i of noteList
        set output to output & name of n & " | " & modification date of n & linefeed
    end repeat
    return output
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to list notes: {out}"
    if not out:
        return "No notes found."
    return out


def read_note(title: str) -> str:
    err = _check_macos()
    if err:
        return err

    if not title:
        return "Please provide a note title."

    safe_title = _escape_applescript(title)
    # Try exact match first, then partial match.
    script = f'''
tell application "Notes"
    set matchedNote to missing value
    -- exact match
    repeat with n in notes
        if name of n is "{safe_title}" then
            set matchedNote to n
            exit repeat
        end if
    end repeat
    -- partial match fallback
    if matchedNote is missing value then
        repeat with n in notes
            if name of n contains "{safe_title}" then
                set matchedNote to n
                exit repeat
            end if
        end repeat
    end if
    if matchedNote is missing value then
        return "Note not found: {safe_title}"
    end if
    set noteTitle to name of matchedNote
    set noteBody to plaintext of matchedNote
    return noteTitle & linefeed & "---" & linefeed & noteBody
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to read note: {out}"
    return out


def search_notes(query: str) -> str:
    err = _check_macos()
    if err:
        return err

    if not query:
        return "Please provide a search query."

    safe_query = _escape_applescript(query)
    script = f'''
tell application "Notes"
    set output to ""
    set matchCount to 0
    repeat with n in notes
        try
            if (name of n contains "{safe_query}") or (plaintext of n contains "{safe_query}") then
                set matchCount to matchCount + 1
                set output to output & name of n & " | " & modification date of n & linefeed
                if matchCount >= 20 then exit repeat
            end if
        end try
    end repeat
    if matchCount = 0 then
        return "No notes matching \\"{safe_query}\\" found."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Search failed: {out}"
    return out


def create_note(title: str, body: str, folder: str | None = None) -> str:
    err = _check_macos()
    if err:
        return err

    if not title:
        return "Please provide a note title."

    safe_title = _escape_applescript(title)
    safe_body = _escape_applescript(body or "")

    if folder:
        safe_folder = _escape_applescript(folder)
        script = f'''
tell application "Notes"
    try
        set theFolder to folder "{safe_folder}"
    on error
        return "Error: Folder \\"{safe_folder}\\" not found. Use list_folders to see available folders."
    end try
    make new note at theFolder with properties {{name:"{safe_title}", body:"{safe_body}"}}
    return "Note \\"{safe_title}\\" created in folder \\"{safe_folder}\\"."
end tell
'''
    else:
        script = f'''
tell application "Notes"
    tell default account
        make new note at folder "Notes" with properties {{name:"{safe_title}", body:"{safe_body}"}}
    end tell
    return "Note \\"{safe_title}\\" created."
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to create note: {out}"
    return out


def list_folders() -> str:
    err = _check_macos()
    if err:
        return err

    script = '''
tell application "Notes"
    set output to ""
    repeat with f in folders
        set output to output & name of f & linefeed
    end repeat
    if output is "" then
        return "No folders found."
    end if
    return output
end tell
'''
    ok, out = _run_applescript(script)
    if not ok:
        return f"Failed to list folders: {out}"
    return out


# ---------------------------------------------------------------------------
# Main entry point — routes on parameters["action"]
# ---------------------------------------------------------------------------

def apple_notes(parameters: dict, player=None) -> str:
    params = parameters or {}
    action = (params.get("action", "") or "").strip().lower().replace("-", "_").replace(" ", "_")

    if player:
        player.write_log(f"[AppleNotes] {action}")

    if action == "list_notes":
        return list_notes(
            folder=params.get("folder"),
            limit=int(params.get("limit", 10)),
        )
    elif action == "read_note":
        return read_note(title=params.get("title", ""))
    elif action == "search_notes":
        return search_notes(query=params.get("query", ""))
    elif action == "create_note":
        return create_note(
            title=params.get("title", ""),
            body=params.get("body", ""),
            folder=params.get("folder"),
        )
    elif action == "list_folders":
        return list_folders()
    else:
        return (
            f"Unknown apple_notes action: '{action}'. "
            "Valid actions: list_notes, read_note, search_notes, create_note, list_folders."
        )
