"""
Todoist task management for Mark-LII.

Uses the Todoist REST API v2 to manage tasks, projects, and filters.
HTTP calls are made with the requests library (already a project dependency).

Config key required in config/api_keys.json:
    todoist_api_key
"""

import json
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


API_CONFIG_PATH = _base_dir() / "config" / "api_keys.json"

_TODOIST_API_BASE = "https://api.todoist.com/rest/v2"


def _load_config() -> dict:
    """Load config/api_keys.json and return the full dict."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_api_key() -> str:
    """Return the Todoist API key from config."""
    cfg = _load_config()
    key = cfg.get("todoist_api_key", "")
    if not key:
        raise ValueError(
            "Todoist API key not configured. "
            "Set todoist_api_key in config/api_keys.json."
        )
    return key


def _headers() -> dict:
    """Return auth headers for the Todoist REST API."""
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }


def _log(message: str, player=None) -> None:
    print(f"[Todoist] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def _resolve_project_id(project_name: str) -> str | None:
    """Resolve a project name to its ID. Returns None if not found."""
    import requests

    resp = requests.get(
        f"{_TODOIST_API_BASE}/projects",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    projects = resp.json()

    name_lower = project_name.lower().strip()
    for project in projects:
        if project.get("name", "").lower() == name_lower:
            return project["id"]

    # Partial match fallback
    for project in projects:
        if name_lower in project.get("name", "").lower():
            return project["id"]

    return None


def _priority_label(priority: int) -> str:
    """Convert Todoist priority (1=normal, 4=urgent) to a human label."""
    return {1: "Normal", 2: "Medium", 3: "High", 4: "Urgent"}.get(priority, "Normal")


# ── Individual action functions ──────────────────────────────────────────────


def todoist_get_tasks(parameters: dict, player=None) -> str:
    """List active tasks, optionally filtered to a specific project."""
    import requests

    project_name = parameters.get("project_name", "").strip()
    params = {}

    if project_name:
        project_id = _resolve_project_id(project_name)
        if not project_id:
            msg = f"Project \"{project_name}\" not found in Todoist, sir."
            _log(msg, player)
            return msg
        params["project_id"] = project_id

    resp = requests.get(
        f"{_TODOIST_API_BASE}/tasks",
        headers=_headers(),
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    tasks = resp.json()

    if not tasks:
        scope = f" in project \"{project_name}\"" if project_name else ""
        msg = f"No active tasks found{scope}, sir."
        _log(msg, player)
        return msg

    scope_label = f" in \"{project_name}\"" if project_name else ""
    lines = [f"Active tasks{scope_label}:"]
    for i, task in enumerate(tasks[:25], 1):
        content = task.get("content", "Untitled")
        priority = _priority_label(task.get("priority", 1))
        due = task.get("due")
        due_str = f" (due: {due['string']})" if due and due.get("string") else ""
        task_id = task.get("id", "")
        lines.append(f"  {i}. [{priority}] {content}{due_str} [ID: {task_id}]")

    if len(tasks) > 25:
        lines.append(f"  ... and {len(tasks) - 25} more tasks.")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


def todoist_add_task(parameters: dict, player=None) -> str:
    """Add a new task to Todoist."""
    import requests

    content = parameters.get("content", "").strip()
    if not content:
        msg = "No task content provided, sir."
        _log(msg, player)
        return msg

    body = {"content": content}

    due_string = parameters.get("due_string", "").strip()
    if due_string:
        body["due_string"] = due_string

    priority = parameters.get("priority")
    if priority is not None:
        try:
            priority = int(priority)
            priority = max(1, min(4, priority))
            body["priority"] = priority
        except (ValueError, TypeError):
            pass

    project_name = parameters.get("project_name", "").strip()
    if project_name:
        project_id = _resolve_project_id(project_name)
        if not project_id:
            msg = f"Project \"{project_name}\" not found in Todoist, sir."
            _log(msg, player)
            return msg
        body["project_id"] = project_id

    description = parameters.get("description", "").strip()
    if description:
        body["description"] = description

    resp = requests.post(
        f"{_TODOIST_API_BASE}/tasks",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    task = resp.json()

    task_id = task.get("id", "")
    parts = [f"Task added: \"{content}\""]
    if due_string:
        parts.append(f"due {due_string}")
    if project_name:
        parts.append(f"in project \"{project_name}\"")
    parts.append(f"[ID: {task_id}]")

    msg = ", ".join(parts) + ", sir."
    _log(msg, player)
    return msg


def todoist_complete_task(parameters: dict, player=None) -> str:
    """Mark a task as complete by its task ID."""
    import requests

    task_id = parameters.get("task_id", "").strip()
    if not task_id:
        msg = "No task_id provided, sir."
        _log(msg, player)
        return msg

    # Get task info first for the confirmation message
    try:
        info_resp = requests.get(
            f"{_TODOIST_API_BASE}/tasks/{task_id}",
            headers=_headers(),
            timeout=10,
        )
        info_resp.raise_for_status()
        task_info = info_resp.json()
        task_name = task_info.get("content", "Unknown")
    except Exception:
        task_name = f"Task {task_id}"

    resp = requests.post(
        f"{_TODOIST_API_BASE}/tasks/{task_id}/close",
        headers=_headers(),
        timeout=10,
    )

    if resp.status_code == 404:
        msg = f"Task with ID \"{task_id}\" not found, sir."
        _log(msg, player)
        return msg

    resp.raise_for_status()

    msg = f"Completed: \"{task_name}\", sir."
    _log(msg, player)
    return msg


def todoist_get_projects(parameters: dict, player=None) -> str:
    """List all Todoist projects."""
    import requests

    resp = requests.get(
        f"{_TODOIST_API_BASE}/projects",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    projects = resp.json()

    if not projects:
        msg = "No projects found in your Todoist account, sir."
        _log(msg, player)
        return msg

    lines = ["Your Todoist projects:"]
    for i, project in enumerate(projects, 1):
        name = project.get("name", "Untitled")
        color = project.get("color", "")
        is_fav = " [Favorite]" if project.get("is_favorite") else ""
        lines.append(f"  {i}. {name}{is_fav}")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


def todoist_search_tasks(parameters: dict, player=None) -> str:
    """Search tasks using Todoist filter syntax."""
    import requests

    query = parameters.get("query", "").strip()
    if not query:
        msg = "No search query provided, sir."
        _log(msg, player)
        return msg

    # Use the filter parameter to search tasks
    resp = requests.get(
        f"{_TODOIST_API_BASE}/tasks",
        headers=_headers(),
        params={"filter": query},
        timeout=10,
    )

    if resp.status_code == 400:
        # Invalid filter syntax -- fall back to fetching all and matching content
        resp_all = requests.get(
            f"{_TODOIST_API_BASE}/tasks",
            headers=_headers(),
            timeout=10,
        )
        resp_all.raise_for_status()
        all_tasks = resp_all.json()
        query_lower = query.lower()
        tasks = [
            t for t in all_tasks
            if query_lower in t.get("content", "").lower()
            or query_lower in t.get("description", "").lower()
        ]
    else:
        resp.raise_for_status()
        tasks = resp.json()

    if not tasks:
        msg = f"No tasks found matching \"{query}\", sir."
        _log(msg, player)
        return msg

    lines = [f"Tasks matching \"{query}\":"]
    for i, task in enumerate(tasks[:20], 1):
        content = task.get("content", "Untitled")
        priority = _priority_label(task.get("priority", 1))
        due = task.get("due")
        due_str = f" (due: {due['string']})" if due and due.get("string") else ""
        task_id = task.get("id", "")
        lines.append(f"  {i}. [{priority}] {content}{due_str} [ID: {task_id}]")

    if len(tasks) > 20:
        lines.append(f"  ... and {len(tasks) - 20} more tasks.")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


# ── Main entry point ─────────────────────────────────────────────────────────

_ACTION_MAP = {
    "get_tasks":      todoist_get_tasks,
    "list_tasks":     todoist_get_tasks,
    "tasks":          todoist_get_tasks,
    "add_task":       todoist_add_task,
    "add":            todoist_add_task,
    "complete_task":  todoist_complete_task,
    "complete":       todoist_complete_task,
    "done":           todoist_complete_task,
    "get_projects":   todoist_get_projects,
    "projects":       todoist_get_projects,
    "list_projects":  todoist_get_projects,
    "search":         todoist_search_tasks,
    "search_tasks":   todoist_search_tasks,
}


def todoist_control(parameters: dict, player=None) -> str:
    """
    Main entry point for Todoist task management.
    Routes to the appropriate handler based on parameters["action"].
    """
    # Gate on config
    cfg = _load_config()
    if not cfg.get("todoist_api_key"):
        msg = (
            "Todoist is not configured, sir. "
            "Please add todoist_api_key to config/api_keys.json."
        )
        _log(msg, player)
        return msg

    action = parameters.get("action", "").strip().lower()

    if not action:
        msg = "No Todoist action specified, sir."
        _log(msg, player)
        return msg

    handler = _ACTION_MAP.get(action)
    if not handler:
        supported = ", ".join(sorted(_ACTION_MAP.keys()))
        msg = f"Unknown Todoist action: \"{action}\". Supported actions: {supported}"
        _log(msg, player)
        return msg

    try:
        return handler(parameters, player=player)
    except Exception as e:
        error_str = str(e)
        if "401" in error_str or "403" in error_str:
            msg = (
                "Todoist authentication failed, sir. "
                "Please check your todoist_api_key in config/api_keys.json."
            )
        else:
            msg = f"Todoist error: {e}"
        _log(msg, player)
        return msg
