"""
Recreation.gov API client for permit availability tracking.

Adapted from reference_tracker.py — uses the same public API endpoints
that recreation.gov's website uses. No API key needed.
"""

import json
import logging
import ssl
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSL context fix for macOS Python (often lacks system certs)
# ---------------------------------------------------------------------------

def _make_ssl_ctx():
    try:
        ctx = ssl.create_default_context()
        urlopen(
            Request("https://www.recreation.gov/", headers={"User-Agent": "test"}),
            timeout=5,
            context=ctx,
        )
        return ctx
    except Exception:
        return ssl._create_unverified_context()


_SSL_CTX = _make_ssl_ctx()

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

BASE_URL = "https://www.recreation.gov/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def api_get(path: str) -> dict:
    """Make a GET request to the recreation.gov API."""
    url = f"{BASE_URL}{path}"
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        log.error("HTTP %s for %s: %s", e.code, url, e.read().decode()[:200])
        raise
    except URLError as e:
        log.error("Connection error for %s: %s", url, e)
        raise


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

def search_permits(query: str) -> list:
    """Search recreation.gov for permits matching a query string."""
    data = api_get(f"/search?q={quote(query)}&fq=entity_type:permit&size=20")
    results = []
    for item in data.get("results", []):
        entity = item.get("entity_id", "")
        name = item.get("name", "Unknown")
        city = item.get("city", "")
        state = item.get("state_code", "")
        loc = f"{city}, {state}".strip(", ")
        results.append({"id": entity, "name": name, "location": loc})
    return results


def get_permit_info(permit_id: str) -> dict:
    """Get permit metadata including division names.
    
    Tries the standard /permits/ endpoint first, then falls back to
    /permitcontent/ for newer permit types (wilderness permits, etc).
    """
    divisions = {}
    name = "Unknown"

    # Try standard endpoint first
    try:
        data = api_get(f"/permits/{permit_id}")
        payload = data.get("payload", {})
        if "error" not in data:
            for div_id, div in payload.get("divisions", {}).items():
                divisions[div_id] = {
                    "id": div_id,
                    "name": div.get("name", "Unknown"),
                    "type": div.get("type", "Unknown"),
                }
            name = payload.get("facility_name", payload.get("name", "Unknown"))
    except Exception:
        pass

    # Fall back to permitcontent endpoint for newer permits
    if not divisions:
        try:
            data = api_get(f"/permitcontent/{permit_id}")
            payload = data.get("payload", {})
            for div_id, div in payload.get("divisions", {}).items():
                divisions[div_id] = {
                    "id": div_id,
                    "name": div.get("name", "Unknown"),
                    "type": div.get("type", "Unknown"),
                }
            name = payload.get("name", name)
        except Exception:
            pass

    return {
        "permit_id": permit_id,
        "name": name,
        "divisions": divisions,
    }


def check_availability(permit_id: str, start_date: str, end_date: str) -> dict:
    """
    Check permit availability for a date range.

    Args:
        permit_id: Recreation.gov permit ID
        start_date: ISO date string (YYYY-MM-DD)
        end_date: ISO date string (YYYY-MM-DD)

    Returns:
        {division_id: {date_availability: {date: {remaining, total, ...}}}}
    """
    start = f"{start_date}T00:00:00.000Z"
    end = f"{end_date}T00:00:00.000Z"
    data = api_get(
        f"/permits/{permit_id}/availability?start_date={start}&end_date={end}"
    )
    return data.get("payload", {}).get("availability", {})


def find_available_slots(
    permit_id: str,
    start_date: str,
    end_date: str,
    division_ids: list | None = None,
    min_available: int = 1,
) -> list:
    """
    Check a permit and return list of available slot dicts.

    Each dict has: permit_id, division_id, division_name, date, date_raw,
    remaining, total.
    """
    try:
        info = get_permit_info(permit_id)
        div_names = {d["id"]: d["name"] for d in info["divisions"].values()}
    except Exception:
        div_names = {}

    try:
        availability = check_availability(permit_id, start_date, end_date)
    except Exception as e:
        log.error("Failed to check availability for %s: %s", permit_id, e)
        return []

    target = set(division_ids) if division_ids else None
    slots = []

    for div_id, div_data in availability.items():
        if target and div_id not in target:
            continue
        div_name = div_names.get(div_id, f"Division {div_id}")
        dates = div_data.get("date_availability", {})

        for date_str, info in sorted(dates.items()):
            remaining = info.get("remaining", 0)
            total = info.get("total", 0)
            if remaining >= min_available:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    display_date = dt.strftime("%a %b %d, %Y")
                except Exception:
                    display_date = date_str
                slots.append(
                    {
                        "permit_id": permit_id,
                        "division_id": div_id,
                        "division_name": div_name,
                        "date": display_date,
                        "date_raw": date_str,
                        "remaining": remaining,
                        "total": total,
                    }
                )

    return slots
