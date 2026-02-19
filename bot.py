#!/usr/bin/env python3
"""
Telegram Bot for Permit Tracker
Allows users to manage permit tracking via chat.

Commands:
  /start <access_code>  - Authenticate
  /search <query>       - Search for permits
  /track <permit_id>    - Set up tracking (interactive)
  /list                 - Show active trackers
  /check                - Run an immediate check
  /stop <tracker_id>    - Stop a tracker
  /help                 - Show commands
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import re

from tracker import get_permit_info, search_permits, check_availability, _SSL_CTX

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("permit-bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
DB_PATH = os.environ.get("DB_PATH", "tracker.db")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

# ---------------------------------------------------------------------------
# Conversation state for multi-step flows (search → select → create)
# ---------------------------------------------------------------------------
# Keyed by chat_id, stores pending search results awaiting user selection
CONV_STATE = {}  # {chat_id: {results: [...], date_range: {start, end}, timestamp: float}}

# Store auth file in persistent volume on Railway, local fallback for dev
_data_dir = "/data" if os.path.isdir("/data") else "."
ALLOWED_USERS_FILE = os.path.join(_data_dir, "allowed_telegram_users.json")


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def tg_api(method, data=None):
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if data:
        payload = json.dumps(data).encode()
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    else:
        req = Request(url)
    try:
        with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.error("Telegram API error: %s", e)
        return {"ok": False, "error": str(e)}


def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    """Send a message to a chat."""
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_api("sendMessage", data)


# ---------------------------------------------------------------------------
# User auth
# ---------------------------------------------------------------------------

def load_allowed_users():
    try:
        with open(ALLOWED_USERS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_allowed_users(users):
    with open(ALLOWED_USERS_FILE, "w") as f:
        json.dump(list(users), f)


def is_authorized(user_id):
    if not ACCESS_CODE:
        return True
    return user_id in load_allowed_users()


def authorize_user(user_id):
    users = load_allowed_users()
    users.add(user_id)
    save_allowed_users(users)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_start(chat_id, user_id, args, first_name):
    chat_id_msg = f"\n\n📋 *Your Chat ID:* `{chat_id}`\nCopy this into the web app to get Telegram notifications!"

    if not ACCESS_CODE:
        authorize_user(user_id)
        send_message(chat_id, f"👋 Welcome {first_name}! No access code required.{chat_id_msg}\n\nUse /help to see commands.", parse_mode="Markdown")
        return

    if is_authorized(user_id):
        send_message(chat_id, f"👋 Welcome back {first_name}! You're already verified.{chat_id_msg}\n\nUse /help to see commands.", parse_mode="Markdown")
        return

    if not args:
        send_message(chat_id, "🔒 This bot requires an access code.\n\nUsage: /start <code>\n\nAsk the admin for the access code.")
        return

    if args.strip() == ACCESS_CODE:
        authorize_user(user_id)
        send_message(chat_id, f"✅ Access granted! Welcome {first_name}.{chat_id_msg}\n\nUse /help to see what I can do.", parse_mode="Markdown")
    else:
        send_message(chat_id, "❌ Invalid access code. Try again with /start <code>")


def handle_chatid(chat_id):
    send_message(chat_id, f"📋 Your Chat ID is: <code>{chat_id}</code>\n\nCopy this into the Permit Tracker web app to receive Telegram notifications!")


def handle_test(chat_id):
    """Trigger a mike test — heartbeat notification to all configured channels."""
    send_message(chat_id, "🎤 Running mike test...")
    try:
        # Import and run the mike test from app
        import importlib
        import sys
        # Direct approach: call the function
        from app import send_mike_test
        results = send_mike_test()
        if not results:
            send_message(chat_id, "⚠️ No notification channels configured yet. Set up notifications in the web app first!")
            return
        lines = ["🎤 <b>Mike Test Results:</b>", ""]
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            lines.append(f"{icon} {r['type']} → {r.get('target', '?')}")
        send_message(chat_id, "\n".join(lines))
    except Exception as e:
        log.error("Mike test from Telegram failed: %s", e)
        send_message(chat_id, f"❌ Mike test failed: {e}")


def handle_help(chat_id):
    send_message(chat_id, """🎯 <b>Permit Tracker Bot</b>

<b>Just type naturally:</b>
• <i>"river permits in Colorado for August"</i>
• <i>"track Maroon Bells July through September"</i>
• <i>"any Half Dome availability this summer?"</i>
I'll search, show numbered results, and you pick which to track!

<b>Commands (also work):</b>
/search &lt;query&gt; — Search for permits
/track &lt;permit_id&gt; &lt;start&gt; &lt;end&gt; — Track a permit
/list — Show your active trackers
/check &lt;tracker_id&gt; — Run immediate check
/checkall — Check all trackers now
/stop &lt;tracker_id&gt; — Stop tracking
/test — 🎤 Mike test (send heartbeat to all notification channels)
/chatid — Show your Chat ID (for web app notifications)
/report &lt;issue&gt; — Report a bug or request a feature
/help — Show this message""")


def handle_search(chat_id, query):
    if not query:
        send_message(chat_id, "Usage: /search <query>\n\nExample: /search half dome")
        return

    send_message(chat_id, f"🔍 Searching for: {query}...")
    try:
        results = search_permits(query)
        if not results:
            send_message(chat_id, "No permits found. Try a different search term.")
            return

        lines = [f"<b>Found {len(results)} permit(s):</b>\n"]
        for r in results[:10]:
            loc = f" — 📍 {r['location']}" if r.get('location') else ""
            lines.append(f"🏕 <b>{r['name']}</b>{loc}\n   ID: <code>{r['id']}</code>")

        lines.append(f"\n💡 To track: /track &lt;ID&gt; &lt;start_date&gt; &lt;end_date&gt;")
        send_message(chat_id, "\n".join(lines))
    except Exception as e:
        send_message(chat_id, f"❌ Search failed: {e}")


def handle_track(chat_id, user_id, args):
    parts = args.strip().split()
    if len(parts) < 3:
        send_message(chat_id, "Usage: /track <permit_id> <start_date> <end_date>\n\nExample: /track 234652 2026-07-01 2026-08-31")
        return

    permit_id, start_date, end_date = parts[0], parts[1], parts[2]

    send_message(chat_id, f"📋 Looking up permit {permit_id}...")

    try:
        info = get_permit_info(permit_id)
        name = info.get("name", "Unknown")
        divisions = info.get("divisions", {})

        # Save to DB — also pull saved user_preferences for other notification channels
        prefs = _get_user_prefs(chat_id)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO trackers
               (permit_id, permit_name, division_ids, start_date, end_date,
                notify_email, notify_ntfy_topic, notify_sms_phone, notify_sms_carrier,
                notify_telegram_chat_id, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (permit_id, name, json.dumps([]), start_date, end_date,
             prefs.get("notify_email", ""),
             prefs.get("notify_ntfy_topic", ""),
             prefs.get("notify_sms_phone", ""),
             prefs.get("notify_sms_carrier", ""),
             str(chat_id))
        )
        tracker_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        div_text = ""
        if divisions:
            div_list = list(divisions.values())[:10]
            div_text = "\n\n<b>Divisions:</b>\n" + "\n".join(
                f"  • {d['name']} ({d.get('type', '?')})" for d in div_list
            )
            if len(divisions) > 10:
                div_text += f"\n  ... and {len(divisions) - 10} more"

        send_message(chat_id, 
            f"✅ <b>Now tracking:</b> {name}\n"
            f"📅 {start_date} → {end_date}\n"
            f"🔔 Notifications: Telegram (this chat)\n"
            f"🆔 Tracker ID: {tracker_id}"
            f"{div_text}\n\n"
            f"I'll check every 10 minutes and message you here when spots open up!"
        )
    except Exception as e:
        send_message(chat_id, f"❌ Failed to set up tracking: {e}")


def handle_list(chat_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trackers WHERE notify_telegram_chat_id = ? AND active = 1",
        (str(chat_id),)
    ).fetchall()
    conn.close()

    if not rows:
        # Also check trackers without notify_telegram_chat_id (web-created)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trackers WHERE active = 1").fetchall()
        conn.close()

    if not rows:
        send_message(chat_id, "No active trackers. Use /search and /track to set one up!")
        return

    lines = ["<b>📋 Active Trackers:</b>\n"]
    for r in rows:
        checked = r['last_checked_at'] or 'never'
        lines.append(
            f"🆔 <b>#{r['id']}</b> — {r['permit_name']}\n"
            f"   📅 {r['start_date']} → {r['end_date']}\n"
            f"   🕐 Last checked: {checked}\n"
        )
    lines.append("Use /check <id> to check now, /stop <id> to remove")
    send_message(chat_id, "\n".join(lines))


def handle_check(chat_id, args):
    send_message(chat_id, "🔍 Checking availability now...")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.strip():
        tracker_id = args.strip()
        rows = conn.execute("SELECT * FROM trackers WHERE id = ? AND active = 1", (tracker_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trackers WHERE (notify_telegram_chat_id = ? OR notify_telegram_chat_id = '' OR notify_telegram_chat_id IS NULL) AND active = 1",
            (str(chat_id),)
        ).fetchall()
    conn.close()

    if not rows:
        send_message(chat_id, "No trackers found. Use /list to see your trackers.")
        return

    for row in rows:
        try:
            avail = check_availability(row['permit_id'], row['start_date'], row['end_date'])
            
            slots = []
            for div_id, div_data in avail.items():
                dates = div_data.get("date_availability", {})
                for date_str, info in sorted(dates.items()):
                    remaining = info.get("remaining", 0)
                    total = info.get("total", 0)
                    if remaining > 0:
                        try:
                            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                            display = dt.strftime("%a %b %d")
                        except Exception:
                            display = date_str[:10]
                        slots.append(f"  📅 {display}: {remaining}/{total} spots")

            if slots:
                text = f"✅ <b>{row['permit_name']}</b> — AVAILABLE!\n\n" + "\n".join(slots[:20])
                if len(slots) > 20:
                    text += f"\n... and {len(slots) - 20} more dates"
                text += f"\n\n🔗 https://www.recreation.gov/permits/{row['permit_id']}"
                send_message(chat_id, text)
            else:
                send_message(chat_id, f"❌ <b>{row['permit_name']}</b> — No availability in {row['start_date']} to {row['end_date']}")

            # Update last checked
            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("UPDATE trackers SET last_checked_at = ? WHERE id = ?",
                         (datetime.utcnow().isoformat(), row['id']))
            conn2.commit()
            conn2.close()

        except Exception as e:
            send_message(chat_id, f"⚠️ Error checking {row['permit_name']}: {e}")


def handle_stop(chat_id, args):
    if not args.strip():
        send_message(chat_id, "Usage: /stop <tracker_id>\n\nUse /list to see your tracker IDs.")
        return

    tracker_id = args.strip()
    conn = sqlite3.connect(DB_PATH)
    result = conn.execute("UPDATE trackers SET active = 0 WHERE id = ?", (tracker_id,))
    conn.commit()
    conn.close()

    if result.rowcount:
        send_message(chat_id, f"✅ Tracker #{tracker_id} stopped.")
    else:
        send_message(chat_id, f"❌ Tracker #{tracker_id} not found.")


def handle_report(chat_id, user_id, first_name, args):
    """Save a bug report/feature request to a file for the dev to review."""
    if not args.strip():
        send_message(chat_id, "Usage: /report <describe the issue>\n\nExample: /report Maroon Bells search isn't showing any results")
        return

    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "user_name": first_name,
        "chat_id": chat_id,
        "message": args.strip()
    }

    # Append to reports file
    reports_file = "reports.json"
    try:
        with open(reports_file) as f:
            reports = json.load(f)
    except Exception:
        reports = []

    reports.append(report)
    with open(reports_file, "w") as f:
        json.dump(reports, f, indent=2)

    log.info("Bug report from %s: %s", first_name, args.strip())

    # Try to wake the AI developer immediately
    try:
        webhook_url = os.environ.get("REPORT_WEBHOOK_URL", "")
        if webhook_url:
            payload = json.dumps({
                "text": f"🐛 Permit Tracker bug report from {first_name}: {args.strip()}",
                "report": report
            }).encode()
            req = Request(webhook_url, data=payload,
                         headers={"Content-Type": "application/json"}, method="POST")
            urlopen(req, timeout=10, context=_SSL_CTX)
    except Exception as e:
        log.error("Failed to send webhook: %s", e)

    send_message(chat_id,
        f"✅ <b>Report received!</b>\n\n"
        f"The AI developer has been notified and will investigate immediately. "
        f"Fixes are usually deployed within minutes — I'll message you here when it's done.\n\n"
        f"<i>You reported: {args.strip()}</i>"
    )


# ---------------------------------------------------------------------------
# Natural Language Understanding
# ---------------------------------------------------------------------------

MONTH_MAP = {
    "jan": (1, "January"), "january": (1, "January"),
    "feb": (2, "February"), "february": (2, "February"),
    "mar": (3, "March"), "march": (3, "March"),
    "apr": (4, "April"), "april": (4, "April"),
    "may": (5, "May"),
    "jun": (6, "June"), "june": (6, "June"),
    "jul": (7, "July"), "july": (7, "July"),
    "aug": (8, "August"), "august": (8, "August"),
    "sep": (9, "September"), "september": (9, "September"),
    "oct": (10, "October"), "october": (10, "October"),
    "nov": (11, "November"), "november": (11, "November"),
    "dec": (12, "December"), "december": (12, "December"),
}

TRACK_WORDS = {"alert", "notify", "track", "watch", "monitor", "tell", "let", "want", "need", "find", "looking", "any", "available", "availability", "avail", "open", "openings", "spots", "check"}
SEARCH_WORDS = {"search", "find", "look", "what", "which", "show", "list"}
CHECK_WORDS = {"check", "status", "how", "update"}
STOP_WORDS = {"stop", "cancel", "remove", "delete", "disable", "turn off"}
REPORT_WORDS = {"broken", "bug", "error", "not working", "doesn't work", "doesnt work", "issue", "problem", "wrong", "fix"}

import calendar


def parse_month_range(text, year=None):
    """Extract month(s) from text and return (start_date, end_date)."""
    if year is None:
        year = datetime.now().year
        # If month already passed, use next year
    
    words = text.lower().split()
    months_found = []
    for word in words:
        word_clean = word.strip(".,!?")
        if word_clean in MONTH_MAP:
            months_found.append(MONTH_MAP[word_clean])
    
    if not months_found:
        # Check for "summer", "fall", etc.
        t = text.lower()
        if "summer" in t:
            months_found = [(6, "June"), (7, "July"), (8, "August")]
        elif "fall" in t or "autumn" in t:
            months_found = [(9, "September"), (10, "October"), (11, "November")]
        elif "spring" in t:
            months_found = [(4, "April"), (5, "May"), (6, "June")]
        elif "winter" in t:
            months_found = [(12, "December"), (1, "January"), (2, "February")]
    
    if not months_found:
        # Default: next 3 months
        now = datetime.now()
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=90)).strftime("%Y-%m-%d")
        return start, end, "the next 3 months"
    
    min_month = min(m[0] for m in months_found)
    max_month = max(m[0] for m in months_found)
    
    # If month already passed this year, use next year
    if max_month < datetime.now().month:
        year = datetime.now().year + 1
    elif min_month < datetime.now().month and max_month >= datetime.now().month:
        year = datetime.now().year
    else:
        year = datetime.now().year

    start_date = f"{year}-{min_month:02d}-01"
    last_day = calendar.monthrange(year, max_month)[1]
    end_date = f"{year}-{max_month:02d}-{last_day:02d}"
    
    month_names = sorted(set(m[1] for m in months_found))
    desc = " - ".join(month_names) if len(month_names) > 1 else month_names[0]
    
    return start_date, end_date, desc


def extract_permit_query(text):
    """Extract the permit name/query from natural language, removing common filler words."""
    # Remove common filler words
    filler = {"alert", "notify", "track", "watch", "monitor", "tell", "let", "me", "to",
              "any", "all", "permits", "permit", "available", "availability", "avail",
              "open", "openings", "spots", "for", "in", "during", "the", "a", "an",
              "find", "search", "looking", "want", "need", "know", "when", "if",
              "about", "i", "my", "please", "can", "you", "check", "on", "of",
              "show", "what", "which", "are", "is", "there", "get", "with"}
    
    words = text.split()
    # Also remove month names
    month_words = set(MONTH_MAP.keys()) | {"summer", "fall", "autumn", "spring", "winter"}
    
    cleaned = [w for w in words if w.lower().strip(".,!?") not in filler 
               and w.lower().strip(".,!?") not in month_words
               and not w.startswith("/")]
    
    return " ".join(cleaned).strip(".,!? ")


def handle_natural_language(chat_id, user_id, first_name, text):
    """Parse natural language and route to appropriate handler."""
    lower = text.lower()
    words = set(lower.split())

    # Check if user is responding to a pending selection (numbered results)
    if _get_conv_state(chat_id):
        if handle_selection(chat_id, user_id, text):
            return
        # If parse_selection returned None, it's not a selection — fall through
        # to treat as a new search (which will clear the old state)

    # Check for "yes" confirmation of pending action (legacy single-result flow)
    if lower.strip() in ("yes", "yeah", "yep", "y", "sure", "ok", "do it", "go ahead"):
        pending = _get_pending(chat_id)
        if pending and pending.get("action") == "track":
            handle_track(chat_id, user_id,
                f"{pending['permit_id']} {pending['start_date']} {pending['end_date']}")
            return

    # Check if it's a bug report
    for phrase in REPORT_WORDS:
        if phrase in lower:
            handle_report(chat_id, user_id, first_name, text)
            return

    # Check if it's about stopping/canceling
    if words & STOP_WORDS:
        send_message(chat_id, "To stop a tracker, use /list to find its ID, then /stop <ID>")
        return

    # Check if it's a status check
    if words & CHECK_WORDS and not (words & TRACK_WORDS):
        # Could be "check my trackers" or "check availability for X"
        query = extract_permit_query(text)
        if query and len(query) > 2:
            # They want to check a specific permit
            handle_search_and_maybe_track(chat_id, user_id, query, text)
        else:
            handle_check(chat_id, "")
        return

    # Check if they want to track/alert/find something
    if words & (TRACK_WORDS | SEARCH_WORDS):
        query = extract_permit_query(text)
        if query and len(query) > 2:
            handle_search_and_maybe_track(chat_id, user_id, query, text)
            return

    # Fallback: if it looks like a permit name (2+ words, no common phrases)
    query = extract_permit_query(text)
    if query and len(query) > 3:
        handle_search_and_maybe_track(chat_id, user_id, query, text)
        return

    # True fallback
    send_message(chat_id,
        "🤔 I'm not sure what you mean. Try something like:\n\n"
        "• <i>\"river permits in Colorado for August\"</i>\n"
        "• <i>\"any half dome availability this summer?\"</i>\n"
        "• <i>\"check my trackers\"</i>\n\n"
        "Or use /help to see all commands."
    )


def handle_search_and_maybe_track(chat_id, user_id, query, original_text):
    """Search for a permit and show numbered results for conversational selection."""
    # Clear any old conversation state
    _clear_conv_state(chat_id)

    send_message(chat_id, f"🔍 Searching for: <b>{query}</b>...")

    try:
        results = search_permits(query)
    except Exception as e:
        send_message(chat_id, f"❌ Search failed: {e}")
        return

    if not results:
        send_message(chat_id, f"No permits found for \"{query}\". Try different keywords.")
        return

    # Parse dates from original text
    start_date, end_date, date_desc = parse_month_range(original_text)

    # Limit to top 10 results
    results = results[:10]

    if len(results) == 1:
        # Single result — store as conversation state for "yes"/"all"/"1" selection
        r = results[0]
        _store_conv_state(chat_id, results, start_date, end_date)
        send_message(chat_id,
            f"Found 1 permit:\n\n"
            f"1. <b>{r['name']}</b>\n"
            f"   📍 {r.get('location', 'Unknown location')}\n"
            f"   ID: <code>{r['id']}</code>\n\n"
            f"📅 Dates: {date_desc} ({start_date} → {end_date})\n\n"
            f"Reply: <b>yes</b> to track it, or <b>cancel</b> to skip"
        )
    else:
        # Multiple results — show numbered list and store state
        _store_conv_state(chat_id, results, start_date, end_date)

        lines = [f"Found {len(results)} permit(s):\n"]
        for i, r in enumerate(results, 1):
            loc = f" — 📍 {r['location']}" if r.get('location') else ""
            lines.append(f"{i}. <b>{r['name']}</b>{loc}\n   ID: <code>{r['id']}</code>")

        lines.append(f"\n📅 Dates: {date_desc} ({start_date} → {end_date})")
        lines.append(f"\nReply: \"<b>all</b>\" to track everything, \"<b>1 and 3</b>\", \"<b>just 2</b>\", or \"<b>cancel</b>\"")
        send_message(chat_id, "\n".join(lines))


# Pending actions (simple in-memory store for "yes" confirmations)
_pending_actions = {}

def _store_pending(chat_id, action):
    _pending_actions[str(chat_id)] = {"action": action, "expires": time.time() + 120}

def _get_pending(chat_id):
    key = str(chat_id)
    if key in _pending_actions:
        p = _pending_actions[key]
        if time.time() < p["expires"]:
            del _pending_actions[key]
            return p["action"]
        del _pending_actions[key]
    return None


# ---------------------------------------------------------------------------
# Conversation state management (multi-step search → select → create)
# ---------------------------------------------------------------------------

ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def _store_conv_state(chat_id, results, start_date, end_date):
    """Store search results for a pending selection."""
    CONV_STATE[str(chat_id)] = {
        "results": results,
        "date_range": {"start": start_date, "end": end_date},
        "timestamp": time.time(),
    }


def _get_conv_state(chat_id):
    """Get pending search results, or None if expired/missing."""
    key = str(chat_id)
    state = CONV_STATE.get(key)
    if not state:
        return None
    # Expire after 10 minutes
    if time.time() - state["timestamp"] > 600:
        del CONV_STATE[key]
        return None
    return state


def _clear_conv_state(chat_id):
    CONV_STATE.pop(str(chat_id), None)


def parse_selection(text, max_index):
    """Parse user's selection from natural language.

    Returns:
        list of 0-based indices, or
        'all' if user wants everything, or
        'cancel' if user wants to cancel, or
        None if unparseable.
    """
    lower = text.lower().strip()

    # Cancel patterns
    if lower in ("none", "cancel", "nevermind", "never mind", "nah", "no", "nope", "skip", "n"):
        return "cancel"

    # All patterns
    if lower in ("all", "yes", "track all", "everything", "all of them", "track everything", "yep", "y", "sure"):
        return "all"

    indices = set()

    # Replace ordinals with numbers
    for word, num in ORDINALS.items():
        lower = re.sub(r'\b' + word + r'\b', str(num), lower)

    # Parse ranges like "1-3" or "1 through 3" or "1 to 3"
    for m in re.finditer(r'(\d+)\s*(?:-|through|thru|to)\s*(\d+)', lower):
        start, end = int(m.group(1)), int(m.group(2))
        for i in range(start, end + 1):
            if 1 <= i <= max_index:
                indices.add(i - 1)

    # Parse individual numbers (after ranges so we don't double-count)
    # Remove already-matched ranges first
    remaining = re.sub(r'(\d+)\s*(?:-|through|thru|to)\s*(\d+)', '', lower)
    for m in re.finditer(r'(\d+)', remaining):
        num = int(m.group(1))
        if 1 <= num <= max_index:
            indices.add(num - 1)

    if indices:
        return sorted(indices)

    return None


def _get_user_prefs(chat_id):
    """Read user_preferences from DB, return dict with notification settings."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM user_preferences ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            return {
                "notify_email": row["notify_email"] if "notify_email" in row.keys() else "",
                "notify_ntfy_topic": row["notify_ntfy_topic"] if "notify_ntfy_topic" in row.keys() else "",
                "notify_sms_phone": row["notify_sms_phone"] if "notify_sms_phone" in row.keys() else "",
                "notify_sms_carrier": row["notify_sms_carrier"] if "notify_sms_carrier" in row.keys() else "",
                "notify_telegram_chat_id": row["notify_telegram_chat_id"] if "notify_telegram_chat_id" in row.keys() else "",
            }
    except Exception as e:
        log.error("Failed to load user_preferences: %s", e)
    return {}


def _create_tracker_from_selection(chat_id, permit, start_date, end_date):
    """Create a tracker in the DB for a given permit, auto-filling notifications."""
    prefs = _get_user_prefs(chat_id)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO trackers
           (permit_id, permit_name, division_ids, start_date, end_date,
            notify_email, notify_ntfy_topic, notify_sms_phone, notify_sms_carrier,
            notify_telegram_chat_id, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            permit["id"],
            permit["name"],
            json.dumps([]),
            start_date,
            end_date,
            prefs.get("notify_email", ""),
            prefs.get("notify_ntfy_topic", ""),
            prefs.get("notify_sms_phone", ""),
            prefs.get("notify_sms_carrier", ""),
            str(chat_id),  # Always use the user's Telegram chat_id
        ),
    )
    tracker_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return tracker_id


def handle_selection(chat_id, user_id, text):
    """Handle user's selection from pending search results. Returns True if handled."""
    state = _get_conv_state(chat_id)
    if not state:
        return False

    results = state["results"]
    start_date = state["date_range"]["start"]
    end_date = state["date_range"]["end"]

    selection = parse_selection(text, len(results))

    if selection is None:
        return False  # Not a selection — treat as new query

    _clear_conv_state(chat_id)

    if selection == "cancel":
        send_message(chat_id, "👍 Cancelled. Send me a new search anytime!")
        return True

    if selection == "all":
        selected = list(range(len(results)))
    else:
        selected = selection

    # Create trackers for selected permits
    created_ids = []
    created_names = []
    for idx in selected:
        permit = results[idx]
        try:
            tracker_id = _create_tracker_from_selection(chat_id, permit, start_date, end_date)
            created_ids.append(tracker_id)
            created_names.append(permit["name"])
        except Exception as e:
            log.error("Failed to create tracker for %s: %s", permit["name"], e)
            send_message(chat_id, f"⚠️ Failed to create tracker for {permit['name']}: {e}")

    if created_ids:
        names_text = "\n".join(f"  • {name} (#{tid})" for name, tid in zip(created_names, created_ids))
        send_message(chat_id,
            f"✅ Created {len(created_ids)} tracker(s)!\n\n"
            f"{names_text}\n\n"
            f"📅 {start_date} → {end_date}\n"
            f"🔔 You'll get a Telegram notification here when spots open up.\n"
            f"Checking every 10 minutes!"
        )
    return True


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def process_update(update):
    """Process a single Telegram update."""
    msg = update.get("message", {})
    if not msg:
        return

    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id")
    first_name = msg.get("from", {}).get("first_name", "there")
    text = msg.get("text", "").strip()

    if not text or not chat_id:
        return

    # Parse command
    if text.startswith("/"):
        parts = text.split(None, 1)
        command = parts[0].lower().split("@")[0]  # Remove @botname suffix
        args = parts[1] if len(parts) > 1 else ""

        if command == "/start":
            handle_start(chat_id, user_id, args, first_name)
            return

        # All other commands require auth
        if not is_authorized(user_id):
            send_message(chat_id, "🔒 Please authenticate first: /start <access_code>")
            return

        if command == "/help":
            handle_help(chat_id)
        elif command == "/chatid":
            handle_chatid(chat_id)
        elif command == "/test":
            handle_test(chat_id)
        elif command == "/search":
            handle_search(chat_id, args)
        elif command == "/track":
            handle_track(chat_id, user_id, args)
        elif command == "/list":
            handle_list(chat_id, user_id)
        elif command == "/check":
            handle_check(chat_id, args)
        elif command == "/checkall":
            handle_check(chat_id, "")
        elif command == "/stop":
            handle_stop(chat_id, args)
        elif command == "/report":
            handle_report(chat_id, user_id, first_name, args)
        else:
            send_message(chat_id, f"Unknown command: {command}\n\nUse /help to see available commands.")
    else:
        # Natural language processing
        if is_authorized(user_id):
            handle_natural_language(chat_id, user_id, first_name, text)
        else:
            send_message(chat_id, "🔒 Please authenticate first: /start <access_code>")


def run_polling():
    """Long-polling loop for Telegram updates."""
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set!")
        return

    log.info("🤖 Permit Tracker Bot starting... (@Permit_tracker_bot)")
    
    # Ensure DB has notify_telegram_chat_id column (init_db handles this, but just in case)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("ALTER TABLE trackers ADD COLUMN notify_telegram_chat_id TEXT DEFAULT ''")
        log.info("Added notify_telegram_chat_id column to trackers table")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.close()

    offset = 0
    while True:
        try:
            result = tg_api("getUpdates", {"offset": offset, "timeout": 30})
            if result.get("ok") and result.get("result"):
                for update in result["result"]:
                    offset = update["update_id"] + 1
                    try:
                        process_update(update)
                    except Exception as e:
                        log.error("Error processing update: %s", e)
        except Exception as e:
            log.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_polling()
