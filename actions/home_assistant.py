"""
Home Assistant smart-home control for Mark-LII.

Uses the Home Assistant REST API via httpx to list devices, get state,
and control lights, switches, climate, and other entities.

Config keys required in config/api_keys.json:
    home_assistant_url, home_assistant_token
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


def _get_ha_client():
    """Return (httpx.Client, base_url) configured for Home Assistant."""
    import httpx

    cfg = _load_config()
    base_url = cfg.get("home_assistant_url", "").rstrip("/")
    token = cfg.get("home_assistant_token", "")

    if not base_url or not token:
        raise ValueError(
            "Home Assistant not configured. "
            "Set home_assistant_url and home_assistant_token in config/api_keys.json."
        )

    client = httpx.Client(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )
    return client, base_url


def _log(message: str, player=None) -> None:
    print(f"[HomeAssistant] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def _domain_from_entity(entity_id: str) -> str:
    """Extract the domain (e.g. 'light', 'switch') from an entity_id."""
    return entity_id.split(".")[0] if "." in entity_id else ""


def _friendly_name(entity: dict) -> str:
    """Get the friendly name from an entity state dict."""
    return entity.get("attributes", {}).get("friendly_name", entity.get("entity_id", "unknown"))


# ── Supported domain filter ─────────────────────────────────────────────────

_SUPPORTED_DOMAINS = {"light", "switch", "sensor", "climate", "binary_sensor", "fan", "cover"}


# ── Individual action functions ──────────────────────────────────────────────


def ha_list_devices(parameters: dict, player=None) -> str:
    """List Home Assistant entities filtered to common device types."""
    client, _ = _get_ha_client()

    domain_filter = parameters.get("domain", "").strip().lower()

    try:
        resp = client.get("/api/states")
        resp.raise_for_status()
        states = resp.json()
    finally:
        client.close()

    filtered = []
    for entity in states:
        eid = entity.get("entity_id", "")
        domain = _domain_from_entity(eid)
        if domain_filter and domain != domain_filter:
            continue
        if not domain_filter and domain not in _SUPPORTED_DOMAINS:
            continue
        filtered.append(entity)

    if not filtered:
        label = f" in domain '{domain_filter}'" if domain_filter else ""
        msg = f"No devices found{label}, sir."
        _log(msg, player)
        return msg

    lines = ["Home Assistant devices:"]
    for entity in filtered[:30]:
        eid = entity.get("entity_id", "")
        name = _friendly_name(entity)
        state = entity.get("state", "unknown")
        lines.append(f"  - {name} ({eid}): {state}")

    if len(filtered) > 30:
        lines.append(f"  ... and {len(filtered) - 30} more devices.")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


def ha_get_state(parameters: dict, player=None) -> str:
    """Get the state of a specific Home Assistant entity."""
    entity_id = parameters.get("entity_id", "").strip()

    if not entity_id:
        msg = "No entity_id provided, sir."
        _log(msg, player)
        return msg

    client, _ = _get_ha_client()

    try:
        resp = client.get(f"/api/states/{entity_id}")
        if resp.status_code == 404:
            msg = f"Entity '{entity_id}' not found, sir."
            _log(msg, player)
            return msg
        resp.raise_for_status()
        entity = resp.json()
    finally:
        client.close()

    name = _friendly_name(entity)
    state = entity.get("state", "unknown")
    attrs = entity.get("attributes", {})

    lines = [f"{name} ({entity_id}): {state}"]

    # Include relevant attributes
    if "brightness" in attrs:
        pct = round(attrs["brightness"] / 255 * 100)
        lines.append(f"  Brightness: {pct}%")
    if "color_temp" in attrs:
        lines.append(f"  Color temp: {attrs['color_temp']}")
    if "current_temperature" in attrs:
        lines.append(f"  Current temperature: {attrs['current_temperature']}")
    if "temperature" in attrs:
        lines.append(f"  Target temperature: {attrs['temperature']}")
    if "hvac_action" in attrs:
        lines.append(f"  HVAC action: {attrs['hvac_action']}")
    if "unit_of_measurement" in attrs:
        lines.append(f"  Unit: {attrs['unit_of_measurement']}")

    msg = "\n".join(lines)
    _log(msg, player)
    return msg


def ha_turn_on(parameters: dict, player=None) -> str:
    """Turn on a Home Assistant entity (domain-routed service call)."""
    entity_id = parameters.get("entity_id", "").strip()

    if not entity_id:
        msg = "No entity_id provided, sir."
        _log(msg, player)
        return msg

    domain = _domain_from_entity(entity_id)
    if not domain:
        msg = f"Invalid entity_id format: '{entity_id}'. Expected 'domain.name', sir."
        _log(msg, player)
        return msg

    client, _ = _get_ha_client()

    try:
        resp = client.post(
            f"/api/services/{domain}/turn_on",
            json={"entity_id": entity_id},
        )
        resp.raise_for_status()
    finally:
        client.close()

    name = entity_id.split(".", 1)[-1].replace("_", " ").title()
    msg = f"Turned on {name} ({entity_id}), sir."
    _log(msg, player)
    return msg


def ha_turn_off(parameters: dict, player=None) -> str:
    """Turn off a Home Assistant entity (domain-routed service call)."""
    entity_id = parameters.get("entity_id", "").strip()

    if not entity_id:
        msg = "No entity_id provided, sir."
        _log(msg, player)
        return msg

    domain = _domain_from_entity(entity_id)
    if not domain:
        msg = f"Invalid entity_id format: '{entity_id}'. Expected 'domain.name', sir."
        _log(msg, player)
        return msg

    client, _ = _get_ha_client()

    try:
        resp = client.post(
            f"/api/services/{domain}/turn_off",
            json={"entity_id": entity_id},
        )
        resp.raise_for_status()
    finally:
        client.close()

    name = entity_id.split(".", 1)[-1].replace("_", " ").title()
    msg = f"Turned off {name} ({entity_id}), sir."
    _log(msg, player)
    return msg


def ha_set_brightness(parameters: dict, player=None) -> str:
    """Set the brightness of a light entity (0-255)."""
    entity_id = parameters.get("entity_id", "").strip()
    brightness = parameters.get("brightness")

    if not entity_id:
        msg = "No entity_id provided, sir."
        _log(msg, player)
        return msg

    if brightness is None:
        msg = "No brightness value provided. Please specify a value between 0 and 255, sir."
        _log(msg, player)
        return msg

    try:
        brightness = int(brightness)
    except (ValueError, TypeError):
        msg = "Invalid brightness value. Please provide a number between 0 and 255, sir."
        _log(msg, player)
        return msg

    brightness = max(0, min(255, brightness))

    domain = _domain_from_entity(entity_id)
    if domain != "light":
        msg = f"Brightness control is only supported for light entities, not '{domain}', sir."
        _log(msg, player)
        return msg

    client, _ = _get_ha_client()

    try:
        if brightness == 0:
            resp = client.post(
                f"/api/services/light/turn_off",
                json={"entity_id": entity_id},
            )
        else:
            resp = client.post(
                f"/api/services/light/turn_on",
                json={"entity_id": entity_id, "brightness": brightness},
            )
        resp.raise_for_status()
    finally:
        client.close()

    pct = round(brightness / 255 * 100)
    name = entity_id.split(".", 1)[-1].replace("_", " ").title()
    msg = f"Set {name} brightness to {pct}% ({brightness}/255), sir."
    _log(msg, player)
    return msg


def ha_set_temperature(parameters: dict, player=None) -> str:
    """Set the target temperature of a climate/thermostat entity."""
    entity_id = parameters.get("entity_id", "").strip()
    temperature = parameters.get("temperature")

    if not entity_id:
        msg = "No entity_id provided, sir."
        _log(msg, player)
        return msg

    if temperature is None:
        msg = "No temperature value provided, sir."
        _log(msg, player)
        return msg

    try:
        temperature = float(temperature)
    except (ValueError, TypeError):
        msg = "Invalid temperature value. Please provide a number, sir."
        _log(msg, player)
        return msg

    domain = _domain_from_entity(entity_id)
    if domain != "climate":
        msg = f"Temperature control is only supported for climate entities, not '{domain}', sir."
        _log(msg, player)
        return msg

    client, _ = _get_ha_client()

    try:
        resp = client.post(
            f"/api/services/climate/set_temperature",
            json={"entity_id": entity_id, "temperature": temperature},
        )
        resp.raise_for_status()
    finally:
        client.close()

    name = entity_id.split(".", 1)[-1].replace("_", " ").title()
    msg = f"Set {name} temperature to {temperature} degrees, sir."
    _log(msg, player)
    return msg


# ── Main entry point ─────────────────────────────────────────────────────────

_ACTION_MAP = {
    "list_devices":    ha_list_devices,
    "list":            ha_list_devices,
    "get_state":       ha_get_state,
    "state":           ha_get_state,
    "turn_on":         ha_turn_on,
    "on":              ha_turn_on,
    "turn_off":        ha_turn_off,
    "off":             ha_turn_off,
    "set_brightness":  ha_set_brightness,
    "brightness":      ha_set_brightness,
    "set_temperature": ha_set_temperature,
    "temperature":     ha_set_temperature,
}


def home_assistant(parameters: dict, player=None) -> str:
    """
    Main entry point for Home Assistant control.
    Routes to the appropriate handler based on parameters["action"].
    """
    # Gate on config
    cfg = _load_config()
    if not cfg.get("home_assistant_url") or not cfg.get("home_assistant_token"):
        msg = (
            "Home Assistant is not configured, sir. "
            "Please add home_assistant_url and home_assistant_token "
            "to config/api_keys.json."
        )
        _log(msg, player)
        return msg

    action = parameters.get("action", "").strip().lower()

    if not action:
        msg = "No Home Assistant action specified, sir."
        _log(msg, player)
        return msg

    handler = _ACTION_MAP.get(action)
    if not handler:
        supported = ", ".join(sorted(_ACTION_MAP.keys()))
        msg = f"Unknown Home Assistant action: \"{action}\". Supported actions: {supported}"
        _log(msg, player)
        return msg

    try:
        return handler(parameters, player=player)
    except Exception as e:
        error_str = str(e)
        if "401" in error_str or "403" in error_str:
            msg = (
                "Home Assistant authentication failed, sir. "
                "Please check your home_assistant_token in config/api_keys.json."
            )
        elif "ConnectError" in error_str or "ConnectionError" in error_str:
            msg = (
                "Could not connect to Home Assistant, sir. "
                "Please check that home_assistant_url is correct and the server is running."
            )
        else:
            msg = f"Home Assistant error: {e}"
        _log(msg, player)
        return msg
