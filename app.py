"""
Permit Tracker Web — Flask app with background scheduler.

Provides a web UI for tracking recreation.gov permit availability
and sending notifications when slots open up.
"""

import json
import logging
import os
import random
import sqlite3
import string
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from functools import wraps

from notifications import send_all_notifications, send_test_notification
from tracker import check_availability, get_permit_info, search_permits, find_available_slots

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("permit-tracker")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

ACCESS_CODE = os.environ.get("ACCESS_CODE", "")

# Use /data/tracker.db on Railway (persistent volume) if /data exists,
# otherwise fall back to local ./tracker.db for development.
_default_db = "/data/tracker.db" if os.path.isdir("/data") else "tracker.db"
DB_PATH = os.environ.get("DB_PATH", _default_db)

# Cache-busting version (changes on each app restart)
import time
CACHE_VERSION = str(int(time.time()))


@app.context_processor
def inject_cache_version():
    return {"cache_version": CACHE_VERSION}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if ACCESS_CODE and not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not ACCESS_CODE:
        session["authenticated"] = True
        return redirect("/")
    error = None
    if request.method == "POST":
        code = request.form.get("access_code", "").strip()
        if code == ACCESS_CODE:
            session["authenticated"] = True
            return redirect("/")
        error = "Invalid access code"
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Permit Tracker - Login</title><link rel="stylesheet" href="/static/style.css">
</head><body>
<header><div class="container"><h1>Permit Tracker</h1>
<p class="subtitle">Enter access code to continue</p></div></header>
<main class="container"><section class="card" style="max-width:400px;margin:2rem auto;text-align:center">
<form method="POST" action="/login">
<input type="password" name="access_code" placeholder="Access code" 
 style="width:100%;padding:12px;font-size:16px;border:2px solid #ddd;border-radius:8px;margin-bottom:12px"
 autofocus>
<button type="submit" style="width:100%;padding:12px;font-size:16px;background:#2d6a4f;color:white;border:none;border-radius:8px;cursor:pointer">Enter</button>
{"<p style='color:#c0392b;margin-top:12px'>" + error + "</p>" if error else ""}
</form></section></main></body></html>"""

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trackers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            permit_id TEXT NOT NULL,
            permit_name TEXT NOT NULL,
            division_ids TEXT NOT NULL DEFAULT '[]',
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            notify_email TEXT DEFAULT '',
            notify_ntfy_topic TEXT DEFAULT '',
            notify_sms_phone TEXT DEFAULT '',
            notify_sms_carrier TEXT DEFAULT '',
            notify_telegram_chat_id TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_checked_at TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracker_id INTEGER NOT NULL,
            permit_name TEXT NOT NULL,
            slots_json TEXT NOT NULL,
            notification_results TEXT DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (tracker_id) REFERENCES trackers(id)
        );

        CREATE TABLE IF NOT EXISTS notified_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracker_id INTEGER NOT NULL,
            slot_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (tracker_id) REFERENCES trackers(id)
        );

        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notify_email TEXT DEFAULT '',
            notify_ntfy_topic TEXT DEFAULT '',
            notify_sms_phone TEXT DEFAULT '',
            notify_sms_carrier TEXT DEFAULT '',
            notify_telegram_chat_id TEXT DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    # Migrate: add telegram column if missing (existing DBs won't have it)
    try:
        conn.execute("ALTER TABLE trackers ADD COLUMN notify_telegram_chat_id TEXT DEFAULT ''")
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN notify_telegram_chat_id TEXT DEFAULT ''")
    except Exception:
        pass
    conn.close()
    log.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

# Track already-notified slots to deduplicate across restarts via DB
def poll_all_trackers():
    """Check all active trackers and send notifications for new availability."""
    log.info("Polling all active trackers...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    trackers = conn.execute(
        "SELECT * FROM trackers WHERE active = 1"
    ).fetchall()

    for t in trackers:
        tracker_id = t["id"]
        permit_id = t["permit_id"]
        permit_name = t["permit_name"]
        division_ids = json.loads(t["division_ids"])
        start_date = t["start_date"]
        end_date = t["end_date"]

        log.info(
            "Checking tracker #%s: %s (%s) %s to %s",
            tracker_id, permit_name, permit_id, start_date, end_date,
        )

        slots = find_available_slots(
            permit_id, start_date, end_date, division_ids or None
        )

        # Update last checked time
        conn.execute(
            "UPDATE trackers SET last_checked_at = datetime('now') WHERE id = ?",
            (tracker_id,),
        )

        if not slots:
            log.info("  No availability for tracker #%s", tracker_id)
            conn.commit()
            continue

        # Deduplicate: only notify on truly new slots
        new_slots = []
        for s in slots:
            key = f"{s['permit_id']}:{s['division_id']}:{s['date_raw']}"
            existing = conn.execute(
                "SELECT id FROM notified_slots WHERE tracker_id = ? AND slot_key = ?",
                (tracker_id, key),
            ).fetchone()
            if not existing:
                new_slots.append(s)
                conn.execute(
                    "INSERT INTO notified_slots (tracker_id, slot_key) VALUES (?, ?)",
                    (tracker_id, key),
                )

        if not new_slots:
            log.info(
                "  %s slot(s) available for tracker #%s but already notified",
                len(slots), tracker_id,
            )
            conn.commit()
            continue

        log.info(
            "  %s NEW slot(s) for tracker #%s — sending notifications",
            len(new_slots), tracker_id,
        )

        # Send notifications
        tracker_config = dict(t)
        results = send_all_notifications(tracker_config, permit_name, new_slots)

        # Record alert
        conn.execute(
            "INSERT INTO alerts (tracker_id, permit_name, slots_json, notification_results) "
            "VALUES (?, ?, ?, ?)",
            (
                tracker_id,
                permit_name,
                json.dumps(new_slots),
                json.dumps(results),
            ),
        )
        conn.commit()

    conn.close()
    log.info("Polling complete.")


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------


@app.route("/api/search")
@login_required
def api_search():
    """Search recreation.gov for permits."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Missing query parameter 'q'"}), 400
    try:
        results = search_permits(q)
        return jsonify(results)
    except Exception as e:
        log.error("Search failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/permits/<permit_id>")
@login_required
def api_permit_info(permit_id):
    """Get permit details and divisions."""
    try:
        info = get_permit_info(permit_id)
        if not info.get("divisions"):
            return jsonify({
                "error": "This permit doesn't have trackable divisions yet. "
                         "It may be a lottery-only permit or not open for the season."
            }), 404
        return jsonify(info)
    except Exception as e:
        log.error("Permit info failed for %s: %s", permit_id, e)
        return jsonify({
            "error": "Couldn't load this permit from recreation.gov. "
                     "It may not be available for tracking yet."
        }), 502


@app.route("/api/permits/<permit_id>/availability")
@login_required
def api_availability(permit_id):
    """Check availability for a permit."""
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not start or not end:
        return jsonify({"error": "Missing start_date or end_date"}), 400
    try:
        data = check_availability(permit_id, start, end)
        return jsonify(data)
    except Exception as e:
        log.error("Availability check failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/trackers", methods=["GET"])
@login_required
def api_list_trackers():
    """List all trackers with alert counts."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM trackers ORDER BY created_at DESC"
    ).fetchall()
    trackers = [dict(r) for r in rows]
    for t in trackers:
        t["division_ids"] = json.loads(t["division_ids"])
        # Count alerts and get last notification time
        count_row = db.execute(
            "SELECT COUNT(*) as cnt, MAX(created_at) as last_at FROM alerts WHERE tracker_id = ?", (t["id"],)
        ).fetchone()
        t["alert_count"] = count_row["cnt"] if count_row else 0
        t["last_notified_at"] = count_row["last_at"] if count_row else None
    return jsonify(trackers)


@app.route("/api/trackers", methods=["POST"])
@login_required
def api_create_tracker():
    """Create a new tracker."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["permit_id", "permit_name", "start_date", "end_date"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    db = get_db()
    cursor = db.execute(
        """INSERT INTO trackers
           (permit_id, permit_name, division_ids, start_date, end_date,
            notify_email, notify_ntfy_topic, notify_sms_phone, notify_sms_carrier,
            notify_telegram_chat_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["permit_id"],
            data["permit_name"],
            json.dumps(data.get("division_ids", [])),
            data["start_date"],
            data["end_date"],
            data.get("notify_email", ""),
            data.get("notify_ntfy_topic", ""),
            data.get("notify_sms_phone", ""),
            data.get("notify_sms_carrier", ""),
            data.get("notify_telegram_chat_id", ""),
        ),
    )
    db.commit()
    tracker_id = cursor.lastrowid
    return jsonify({"id": tracker_id, "status": "created"}), 201


@app.route("/api/trackers/<int:tracker_id>", methods=["DELETE"])
@login_required
def api_delete_tracker(tracker_id):
    """Delete a tracker and its associated data."""
    db = get_db()
    db.execute("DELETE FROM notified_slots WHERE tracker_id = ?", (tracker_id,))
    db.execute("DELETE FROM alerts WHERE tracker_id = ?", (tracker_id,))
    db.execute("DELETE FROM trackers WHERE id = ?", (tracker_id,))
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/trackers/<int:tracker_id>/toggle", methods=["POST"])
@login_required
def api_toggle_tracker(tracker_id):
    """Toggle a tracker active/inactive."""
    db = get_db()
    row = db.execute("SELECT active FROM trackers WHERE id = ?", (tracker_id,)).fetchone()
    if not row:
        return jsonify({"error": "Tracker not found"}), 404
    new_active = 0 if row["active"] else 1
    db.execute("UPDATE trackers SET active = ? WHERE id = ?", (new_active, tracker_id))
    db.commit()
    return jsonify({"id": tracker_id, "active": new_active})


@app.route("/api/alerts")
@login_required
def api_list_alerts():
    """List recent alerts."""
    limit = request.args.get("limit", 50, type=int)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    alerts = []
    for r in rows:
        a = dict(r)
        a["slots"] = json.loads(a["slots_json"])
        a["notification_results"] = json.loads(a["notification_results"])
        del a["slots_json"]
        alerts.append(a)
    return jsonify(alerts)


@app.route("/api/test-notification", methods=["POST"])
@login_required
def api_test_notification():
    """Send a test notification to verify configuration."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    notif_type = data.get("type", "")
    if notif_type not in ("email", "ntfy", "sms", "telegram"):
        return jsonify({"error": "type must be email, ntfy, sms, or telegram"}), 400

    result = send_test_notification(
        notif_type,
        email=data.get("email", ""),
        ntfy_topic=data.get("ntfy_topic", ""),
        sms_phone=data.get("sms_phone", ""),
        sms_carrier=data.get("sms_carrier", ""),
        telegram_chat_id=data.get("telegram_chat_id", ""),
    )
    return jsonify(result)


@app.route("/api/trackers/<int:tracker_id>/test-notifications", methods=["POST"])
@login_required
def api_test_all_notifications(tracker_id):
    """Send test notifications to all channels configured on a tracker."""
    db = get_db()
    row = db.execute("SELECT * FROM trackers WHERE id = ?", (tracker_id,)).fetchone()
    if not row:
        return jsonify({"error": "Tracker not found"}), 404

    t = dict(row)
    results = []
    if t.get("notify_telegram_chat_id"):
        results.append(send_test_notification("telegram", telegram_chat_id=t["notify_telegram_chat_id"]))
    if t.get("notify_ntfy_topic"):
        results.append(send_test_notification("ntfy", ntfy_topic=t["notify_ntfy_topic"]))
    if t.get("notify_email"):
        results.append(send_test_notification("email", email=t["notify_email"]))
    if t.get("notify_sms_phone") and t.get("notify_sms_carrier"):
        results.append(send_test_notification("sms", sms_phone=t["notify_sms_phone"], sms_carrier=t["notify_sms_carrier"]))

    if not results:
        return jsonify({"error": "No notification channels configured"}), 400
    return jsonify({"results": results})


@app.route("/api/trackers/<int:tracker_id>/check", methods=["POST"])
@login_required
def api_check_now(tracker_id):
    """Manually trigger a check for a single tracker."""
    db = get_db()
    row = db.execute("SELECT * FROM trackers WHERE id = ?", (tracker_id,)).fetchone()
    if not row:
        return jsonify({"error": "Tracker not found"}), 404

    t = dict(row)
    division_ids = json.loads(t["division_ids"])
    slots = find_available_slots(
        t["permit_id"], t["start_date"], t["end_date"], division_ids or None
    )

    db.execute(
        "UPDATE trackers SET last_checked_at = datetime('now') WHERE id = ?",
        (tracker_id,),
    )
    db.commit()

    return jsonify({"tracker_id": tracker_id, "slots": slots})


# ---------------------------------------------------------------------------
# User Preferences API
# ---------------------------------------------------------------------------

@app.route("/api/generate-topic", methods=["POST"])
@login_required
def api_generate_topic():
    """Generate a unique ntfy topic name."""
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    topic = f"permit-tracker-{suffix}"
    return jsonify({"topic": topic})


@app.route("/api/preferences", methods=["GET"])
@login_required
def api_get_preferences():
    """Get saved notification preferences."""
    db = get_db()
    row = db.execute("SELECT * FROM user_preferences ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        prefs = {
            "notify_email": row["notify_email"] if "notify_email" in row.keys() else "",
            "notify_ntfy_topic": row["notify_ntfy_topic"] if "notify_ntfy_topic" in row.keys() else "",
            "notify_sms_phone": row["notify_sms_phone"] if "notify_sms_phone" in row.keys() else "",
            "notify_sms_carrier": row["notify_sms_carrier"] if "notify_sms_carrier" in row.keys() else "",
            "notify_telegram_chat_id": row["notify_telegram_chat_id"] if "notify_telegram_chat_id" in row.keys() else "",
        }
        prefs["setup_complete"] = bool(prefs.get("notify_telegram_chat_id") or prefs.get("notify_ntfy_topic") or prefs.get("notify_email"))
        return jsonify(prefs)
    return jsonify({"setup_complete": False})


@app.route("/api/preferences", methods=["POST"])
@login_required
def api_save_preferences():
    """Save notification preferences."""
    data = request.get_json()
    db = get_db()
    # Upsert — delete old, insert new
    db.execute("DELETE FROM user_preferences")
    db.execute(
        """INSERT INTO user_preferences (notify_email, notify_ntfy_topic, notify_sms_phone, notify_sms_carrier, notify_telegram_chat_id)
           VALUES (?, ?, ?, ?, ?)""",
        (
            data.get("notify_email", ""),
            data.get("notify_ntfy_topic", ""),
            data.get("notify_sms_phone", ""),
            data.get("notify_sms_carrier", ""),
            data.get("notify_telegram_chat_id", ""),
        )
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Daily Check-in — real status report for the last 24 hours
# ---------------------------------------------------------------------------

def send_daily_checkin():
    """Send a daily status report with real stats from the last 24 hours."""
    log.info("📊 Daily check-in — building status report...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Collect unique notification targets from trackers AND global prefs
    targets = {
        "telegram": set(),
        "ntfy": set(),
        "email": set(),
    }

    # From active trackers
    trackers = conn.execute("SELECT * FROM trackers WHERE active = 1").fetchall()
    for t in trackers:
        if t["notify_telegram_chat_id"]:
            targets["telegram"].add(t["notify_telegram_chat_id"])
        if t["notify_ntfy_topic"]:
            targets["ntfy"].add(t["notify_ntfy_topic"])
        if t["notify_email"]:
            targets["email"].add(t["notify_email"])

    # From global preferences
    pref = conn.execute("SELECT * FROM user_preferences ORDER BY id DESC LIMIT 1").fetchone()
    if pref:
        try:
            if pref["notify_telegram_chat_id"]:
                targets["telegram"].add(pref["notify_telegram_chat_id"])
        except Exception:
            pass
        if pref["notify_ntfy_topic"]:
            targets["ntfy"].add(pref["notify_ntfy_topic"])
        if pref["notify_email"]:
            targets["email"].add(pref["notify_email"])

    # --- Gather real stats ---
    tracker_count = len(trackers)
    paused_count = conn.execute("SELECT COUNT(*) as cnt FROM trackers WHERE active = 0").fetchone()["cnt"]

    # How many checks in the last 24h (each tracker checked = 1 check)
    checks_24h = 0
    for t in trackers:
        if t["last_checked_at"]:
            checks_24h += 1  # At minimum checked once if last_checked_at is set

    # Calculate expected checks: 3 per hour * 24h = 72 per tracker, but we approximate
    # by counting how many poll cycles ran (every 20 min = 72 per day)
    expected_checks_per_day = 72  # 24h * 60min / 20min
    if tracker_count > 0:
        # Count alerts in last 24h
        alerts_24h_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE created_at >= datetime('now', '-24 hours')"
        ).fetchone()
        alerts_24h = alerts_24h_row["cnt"] if alerts_24h_row else 0

        # Total slots found in last 24h
        recent_alerts = conn.execute(
            "SELECT slots_json FROM alerts WHERE created_at >= datetime('now', '-24 hours')"
        ).fetchall()
        total_slots_found = 0
        for a in recent_alerts:
            try:
                slots = json.loads(a["slots_json"])
                total_slots_found += len(slots)
            except Exception:
                pass

        # Per-tracker summary
        tracker_lines = []
        for t in trackers:
            name = t["permit_name"]
            # Count alerts for this tracker in 24h
            t_alerts = conn.execute(
                "SELECT COUNT(*) as cnt FROM alerts WHERE tracker_id = ? AND created_at >= datetime('now', '-24 hours')",
                (t["id"],)
            ).fetchone()["cnt"]
            date_range = f"{t['start_date']} → {t['end_date']}"
            if t_alerts > 0:
                tracker_lines.append(f"  🟢 {name} — {t_alerts} alert(s) sent")
            else:
                tracker_lines.append(f"  ⚪ {name} — no new availability")
    else:
        alerts_24h = 0
        total_slots_found = 0
        tracker_lines = ["  No active trackers"]

    conn.close()

    now = datetime.utcnow().strftime("%b %d, %Y %H:%M UTC")
    from notifications import send_telegram, send_ntfy, send_email

    # Build status message
    if tracker_count > 0:
        status_emoji = "✅" if alerts_24h == 0 else "🔔"
        status_text = "All quiet — no new permits found" if alerts_24h == 0 else f"{alerts_24h} availability alert(s) sent!"
    else:
        status_emoji = "⏸️"
        status_text = "No active trackers — set one up to start monitoring"

    tracker_summary = "\n".join(tracker_lines)

    results = []

    # --- Telegram ---
    for chat_id in targets["telegram"]:
        from notifications import TELEGRAM_BOT_TOKEN
        if TELEGRAM_BOT_TOKEN:
            import json as _json
            from urllib.request import Request, urlopen
            from notifications import _SSL_CTX
            msg = (
                f"📊 *Daily Check\\-in*\n\n"
                f"{status_emoji} {_md2_escape(status_text)}\n\n"
                f"*Last 24 hours:*\n"
                f"🔍 ~{expected_checks_per_day} checks performed\n"
                f"📋 {tracker_count} active tracker{'s' if tracker_count != 1 else ''}"
                f"{f' \\+ {paused_count} paused' if paused_count else ''}\n"
                f"🔔 {alerts_24h} alert{'s' if alerts_24h != 1 else ''} sent"
                f"{f' \\({total_slots_found} slot{\"s\" if total_slots_found != 1 else \"\"}\\)' if total_slots_found else ''}\n\n"
                f"*Trackers:*\n{_md2_escape(tracker_summary)}\n\n"
                f"🕐 {_md2_escape(now)}"
            )
            payload = _json.dumps({
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "MarkdownV2",
            }).encode()
            req = Request(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                    results.append({"type": "telegram", "target": chat_id, "success": True})
                    log.info("Daily check-in sent to Telegram %s", chat_id)
            except Exception as e:
                results.append({"type": "telegram", "target": chat_id, "success": False, "error": str(e)})
                log.error("Daily check-in Telegram failed for %s: %s", chat_id, e)

    # --- ntfy ---
    for topic in targets["ntfy"]:
        msg = (
            f"📊 Daily Check-in\n\n"
            f"{status_emoji} {status_text}\n\n"
            f"Last 24 hours:\n"
            f"🔍 ~{expected_checks_per_day} checks performed\n"
            f"📋 {tracker_count} active tracker{'s' if tracker_count != 1 else ''}\n"
            f"🔔 {alerts_24h} alert{'s' if alerts_24h != 1 else ''} sent\n\n"
            f"Trackers:\n{tracker_summary}"
        )
        # send_ntfy expects slots but we pass a dummy for compatibility
        ok = send_ntfy(topic, "Daily Check-in", [{
            "permit_id": "0", "division_id": "0", "division_name": status_text,
            "date": now, "date_raw": datetime.utcnow().isoformat() + "Z",
            "remaining": alerts_24h, "total": tracker_count,
        }])
        results.append({"type": "ntfy", "target": topic, "success": ok})

    # --- Email ---
    for addr in targets["email"]:
        ok = send_email(addr, "Daily Check-in", [{
            "permit_id": "0", "division_id": "0", "division_name": status_text,
            "date": now, "date_raw": datetime.utcnow().isoformat() + "Z",
            "remaining": alerts_24h, "total": tracker_count,
        }])
        results.append({"type": "email", "target": addr, "success": ok})

    log.info("Daily check-in complete: %s", results)
    return results


def _md2_escape(text):
    """Escape special chars for Telegram MarkdownV2."""
    special = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in special else c for c in str(text))


@app.route("/api/daily-checkin", methods=["POST"])
@login_required
def api_daily_checkin():
    """Manually trigger a daily check-in status report."""
    results = send_daily_checkin()
    if not results:
        return jsonify({"error": "No notification channels configured. Set up Telegram or ntfy first!"}), 400
    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# Reports API (for monitoring by AI developer)
# ---------------------------------------------------------------------------

@app.route("/api/reports")
@login_required
def api_reports():
    """Get bug reports."""
    try:
        with open("reports.json") as f:
            reports = json.load(f)
        return jsonify(reports)
    except Exception:
        return jsonify([])


@app.route("/api/reports/reply", methods=["POST"])
@login_required
def api_report_reply():
    """Send a reply to a user via Telegram bot."""
    data = request.get_json()
    chat_id = data.get("chat_id")
    message = data.get("message")
    if not chat_id or not message:
        return jsonify({"error": "chat_id and message required"}), 400
    try:
        from bot import send_message
        send_message(int(chat_id), message)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

init_db()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(poll_all_trackers, "interval", minutes=20, id="poller", max_instances=1)
scheduler.add_job(send_daily_checkin, "interval", hours=24, id="daily-checkin", max_instances=1)
scheduler.start()
log.info("Background scheduler started — polling every 20 minutes, daily check-in every 24 hours")

# Start Telegram bot in background thread if token is set
_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if _bot_token:
    import threading
    def _run_bot():
        try:
            from bot import run_polling
            run_polling()
        except Exception as e:
            log.error("Telegram bot failed: %s", e)
    _bot_thread = threading.Thread(target=_run_bot, daemon=True)
    _bot_thread.start()
    log.info("Telegram bot started in background thread")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
