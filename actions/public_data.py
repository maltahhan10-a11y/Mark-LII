"""
Public Data APIs — free, keyless endpoints for currency, crypto, holidays,
country info, IP geolocation, and spaceflight news.

All HTTP libraries are imported lazily inside functions so the module
loads instantly even when ``requests`` is not yet installed.
"""

import time
import sys
from pathlib import Path

# ── Simple TTL cache ─────────────────────────────────────────────────────────
_CACHE_TTL = 900  # 15 minutes
_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value):
    _cache[key] = (time.monotonic(), value)


# ── Country-name alias map ───────────────────────────────────────────────────
_COUNTRY_ALIASES: dict[str, str] = {
    "america":        "US",
    "usa":            "US",
    "united states":  "US",
    "uk":             "GB",
    "england":        "GB",
    "britain":        "GB",
    "great britain":  "GB",
    "united kingdom": "GB",
    "germany":        "DE",
    "deutschland":    "DE",
    "france":         "FR",
    "turkey":         "TR",
    "turkiye":        "TR",
    "japan":          "JP",
    "china":          "CN",
    "india":          "IN",
    "brazil":         "BR",
    "canada":         "CA",
    "australia":      "AU",
    "italy":          "IT",
    "spain":          "ES",
    "mexico":         "MX",
    "south korea":    "KR",
    "korea":          "KR",
    "russia":         "RU",
    "netherlands":    "NL",
    "holland":        "NL",
    "sweden":         "SE",
    "norway":         "NO",
    "switzerland":    "CH",
    "saudi arabia":   "SA",
    "uae":            "AE",
    "egypt":          "EG",
    "jordan":         "JO",
}


def _resolve_country(raw: str) -> str:
    """Return a two-letter country code, resolving common aliases."""
    key = raw.strip().lower()
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    # Already looks like a code
    if len(raw.strip()) == 2:
        return raw.strip().upper()
    return raw.strip()


# ── Helper ───────────────────────────────────────────────────────────────────
def _get(url: str, params: dict | None = None, timeout: int = 10) -> dict | list:
    import requests
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _log(msg: str, player=None):
    print(f"[PublicData] {msg}")
    if player:
        try:
            player.write_log(f"[PublicData] {msg}")
        except Exception:
            pass


# ── Individual features ──────────────────────────────────────────────────────

def _currency_convert(parameters: dict, player=None) -> str:
    amount   = float(parameters.get("amount", 1))
    src      = parameters.get("from_currency", "USD").upper().strip()
    dst      = parameters.get("to_currency",   "EUR").upper().strip()

    cache_key = f"currency:{src}:{dst}"
    cached = _cache_get(cache_key)
    if cached:
        rate = cached
    else:
        try:
            data = _get(
                f"https://api.frankfurter.app/latest",
                params={"from": src, "to": dst},
            )
            rate = data["rates"].get(dst)
            if rate is None:
                return f"Could not find conversion rate for {src} to {dst}."
            _cache_set(cache_key, rate)
        except Exception as e:
            _log(f"Currency API error: {e}", player)
            return f"Currency conversion failed: {e}"

    converted = round(amount * rate, 2)
    return f"{amount} {src} = {converted} {dst} (rate: {rate})"


def _crypto_price(parameters: dict, player=None) -> str:
    coin_id  = parameters.get("coin_id", "bitcoin").lower().strip()
    currency = parameters.get("vs_currency", "usd").lower().strip()

    cache_key = f"crypto:{coin_id}:{currency}"
    cached = _cache_get(cache_key)
    if cached:
        data = cached
    else:
        try:
            data = _get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids":           coin_id,
                    "vs_currencies": currency,
                    "include_24hr_change": "true",
                    "include_market_cap":  "true",
                },
            )
            _cache_set(cache_key, data)
        except Exception as e:
            _log(f"CoinGecko API error: {e}", player)
            return f"Crypto price lookup failed: {e}"

    coin_data = data.get(coin_id)
    if not coin_data:
        return f"Could not find data for '{coin_id}'. Try the full CoinGecko ID (e.g. 'bitcoin', 'ethereum')."

    price      = coin_data.get(currency, "N/A")
    change_24h = coin_data.get(f"{currency}_24h_change")
    market_cap = coin_data.get(f"{currency}_market_cap")

    parts = [f"{coin_id.title()}: {price} {currency.upper()}"]
    if change_24h is not None:
        parts.append(f"24h change: {change_24h:+.2f}%")
    if market_cap is not None:
        parts.append(f"Market cap: {market_cap:,.0f} {currency.upper()}")

    return " | ".join(parts)


def _public_holidays(parameters: dict, player=None) -> str:
    from datetime import datetime

    country = _resolve_country(parameters.get("country_code", "US"))
    year    = int(parameters.get("year", datetime.now().year))

    cache_key = f"holidays:{country}:{year}"
    cached = _cache_get(cache_key)
    if cached:
        holidays = cached
    else:
        try:
            holidays = _get(
                f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
            )
            _cache_set(cache_key, holidays)
        except Exception as e:
            _log(f"Holidays API error: {e}", player)
            return f"Holiday lookup failed: {e}"

    if not isinstance(holidays, list) or not holidays:
        return f"No public holidays found for {country} in {year}."

    lines = [f"Public holidays in {country} ({year}):"]
    for h in holidays:
        lines.append(f"  {h.get('date', '?')} - {h.get('localName', h.get('name', '?'))}")

    return "\n".join(lines)


def _country_info(parameters: dict, player=None) -> str:
    name = parameters.get("name", "").strip()
    if not name:
        return "Please provide a country name."

    resolved = _resolve_country(name)
    cache_key = f"country:{resolved}"
    cached = _cache_get(cache_key)
    if cached:
        countries = cached
    else:
        try:
            # Try by code first if it looks like one
            if len(resolved) == 2:
                countries = _get(
                    f"https://restcountries.com/v3.1/alpha/{resolved}"
                )
            else:
                countries = _get(
                    f"https://restcountries.com/v3.1/name/{resolved}"
                )
            _cache_set(cache_key, countries)
        except Exception as e:
            _log(f"Country API error: {e}", player)
            return f"Country lookup failed: {e}"

    if not isinstance(countries, list) or not countries:
        return f"No information found for '{name}'."

    c = countries[0]
    common_name = c.get("name", {}).get("common", "Unknown")
    official    = c.get("name", {}).get("official", "")
    capital     = ", ".join(c.get("capital", []))   or "N/A"
    region      = c.get("region", "N/A")
    subregion   = c.get("subregion", "")
    population  = c.get("population", 0)
    area        = c.get("area", 0)
    currencies  = c.get("currencies", {})
    languages   = c.get("languages", {})
    timezones   = c.get("timezones", [])

    cur_str  = ", ".join(
        f"{v.get('name', k)} ({v.get('symbol', '')})"
        for k, v in currencies.items()
    ) or "N/A"
    lang_str = ", ".join(languages.values()) or "N/A"
    tz_str   = ", ".join(timezones[:5])
    if len(timezones) > 5:
        tz_str += f" (+{len(timezones) - 5} more)"

    lines = [
        f"{common_name} ({official})" if official else common_name,
        f"  Capital:    {capital}",
        f"  Region:     {region}" + (f" / {subregion}" if subregion else ""),
        f"  Population: {population:,}",
        f"  Area:       {area:,.0f} km2",
        f"  Currencies: {cur_str}",
        f"  Languages:  {lang_str}",
        f"  Timezones:  {tz_str}",
    ]
    return "\n".join(lines)


def _ip_geolocation(parameters: dict, player=None) -> str:
    ip = parameters.get("ip", "").strip() or None

    url = f"http://ip-api.com/json/{ip}" if ip else "http://ip-api.com/json/"

    cache_key = f"ipgeo:{ip or 'self'}"
    cached = _cache_get(cache_key)
    if cached:
        data = cached
    else:
        try:
            data = _get(url)
            _cache_set(cache_key, data)
        except Exception as e:
            _log(f"IP-API error: {e}", player)
            return f"IP geolocation failed: {e}"

    if data.get("status") == "fail":
        return f"Geolocation failed: {data.get('message', 'unknown error')}"

    lines = [
        f"IP: {data.get('query', 'N/A')}",
        f"  Location: {data.get('city', '?')}, {data.get('regionName', '?')}, {data.get('country', '?')}",
        f"  ISP:      {data.get('isp', 'N/A')}",
        f"  Org:      {data.get('org', 'N/A')}",
        f"  Timezone: {data.get('timezone', 'N/A')}",
        f"  Coords:   {data.get('lat', '?')}, {data.get('lon', '?')}",
    ]
    return "\n".join(lines)


def _spaceflight_news(parameters: dict, player=None) -> str:
    limit = int(parameters.get("limit", 5))
    limit = max(1, min(limit, 20))

    cache_key = f"spacenews:{limit}"
    cached = _cache_get(cache_key)
    if cached:
        articles = cached
    else:
        try:
            data = _get(
                "https://api.spaceflightnewsapi.net/v4/articles/",
                params={"limit": limit},
            )
            articles = data.get("results", [])
            _cache_set(cache_key, articles)
        except Exception as e:
            _log(f"Spaceflight News API error: {e}", player)
            return f"Spaceflight news lookup failed: {e}"

    if not articles:
        return "No spaceflight news articles found."

    lines = [f"Latest spaceflight news ({len(articles)} articles):"]
    for i, a in enumerate(articles, 1):
        title   = a.get("title", "Untitled")
        source  = a.get("news_site", "Unknown")
        pub     = (a.get("published_at") or "")[:10]
        url     = a.get("url", "")
        summary = a.get("summary", "")
        if len(summary) > 120:
            summary = summary[:117] + "..."
        lines.append(f"  {i}. [{pub}] {title} ({source})")
        if summary:
            lines.append(f"     {summary}")
        if url:
            lines.append(f"     {url}")

    return "\n".join(lines)


# ── Action router ────────────────────────────────────────────────────────────
_ACTIONS = {
    "currency":   _currency_convert,
    "crypto":     _crypto_price,
    "holidays":   _public_holidays,
    "country":    _country_info,
    "ip_geo":     _ip_geolocation,
    "space_news": _spaceflight_news,
}


def public_data(parameters: dict, player=None) -> str:
    """
    Main entry point.  Routes on ``parameters["action"]``:

        currency | crypto | holidays | country | ip_geo | space_news
    """
    params = parameters or {}
    action = params.get("action", "").strip().lower()

    handler = _ACTIONS.get(action)
    if handler is None:
        available = ", ".join(sorted(_ACTIONS))
        return f"Unknown public_data action '{action}'. Available: {available}"

    try:
        return handler(params, player)
    except Exception as e:
        _log(f"Action '{action}' failed: {e}", player)
        return f"Public data request failed: {e}"
