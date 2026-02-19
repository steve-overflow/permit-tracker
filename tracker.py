"""
Recreation.gov API client for permit availability tracking.

Adapted from reference_tracker.py — uses the same public API endpoints
that recreation.gov's website uses. No API key needed.
"""

import json
import logging
import ssl
from datetime import datetime, timedelta
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

    Returns dict with permit_type = "standard" or "itinerary".
    """
    divisions = {}
    name = "Unknown"
    permit_type = "standard"

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

    # Fall back to permitcontent endpoint for newer permits (itinerary type)
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
            permit_type = "itinerary"
        except Exception:
            pass

    return {
        "permit_id": permit_id,
        "name": name,
        "divisions": divisions,
        "permit_type": permit_type,
    }


def check_availability(permit_id: str, start_date: str, end_date: str) -> dict:
    """
    Check permit availability using the standard API.

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


def check_itinerary_availability(
    permit_id: str, division_ids: list, start_date: str, end_date: str
) -> dict:
    """
    Check availability using the itinerary API (per-division, per-month).

    Args:
        permit_id: Recreation.gov permit ID
        division_ids: List of division IDs to query
        start_date: ISO date string (YYYY-MM-DD)
        end_date: ISO date string (YYYY-MM-DD)

    Returns:
        Same format as check_availability:
        {division_id: {date_availability: {date: {remaining, total}}}}
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Build list of (year, month) pairs covering the range
    months = []
    cur = start_dt.replace(day=1)
    while cur <= end_dt:
        months.append((cur.year, cur.month))
        # Advance to next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)

    result = {}

    for div_id in division_ids:
        date_availability = {}
        for year, month in months:
            try:
                data = api_get(
                    f"/permititinerary/{permit_id}/division/{div_id}"
                    f"/availability/month?month={month}&year={year}"
                )
                payload = data.get("payload", {})
                quota_maps = payload.get("quota_type_maps", {})
                daily = quota_maps.get("ConstantQuotaUsageDaily", {})

                for date_str, info in daily.items():
                    # Filter to requested date range
                    try:
                        dt = datetime.fromisoformat(
                            date_str.replace("Z", "+00:00")
                        )
                        d = dt.strftime("%Y-%m-%d")
                    except Exception:
                        d = date_str[:10]

                    if start_date <= d <= end_date:
                        date_availability[date_str] = {
                            "remaining": info.get("remaining", 0),
                            "total": info.get("total", 0),
                        }
            except Exception as e:
                log.warning(
                    "Itinerary API failed for permit %s div %s %d/%d: %s",
                    permit_id, div_id, month, year, e,
                )

        if date_availability:
            result[div_id] = {"date_availability": date_availability}

    return result


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

    Automatically detects whether to use the standard or itinerary API.
    """
    try:
        info = get_permit_info(permit_id)
        div_names = {d["id"]: d["name"] for d in info["divisions"].values()}
        permit_type = info.get("permit_type", "standard")
    except Exception:
        div_names = {}
        permit_type = "standard"

    try:
        if permit_type == "itinerary":
            # Itinerary API requires explicit division list
            query_divs = division_ids or list(div_names.keys())
            if not query_divs:
                log.warning("No divisions to query for itinerary permit %s", permit_id)
                return []
            availability = check_itinerary_availability(
                permit_id, query_divs, start_date, end_date
            )
        else:
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

        for date_str, slot_info in sorted(dates.items()):
            remaining = slot_info.get("remaining", 0)
            total = slot_info.get("total", 0)
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
