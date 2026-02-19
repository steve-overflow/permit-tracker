"""
Permit Tracker Web — Flask app with background scheduler.

Provides a web UI for tracking recreation.gov permit availability
and sending notifications when slots open up.
"""

import json
import logging
import os
import sqlite3
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
DB_PATH = os.environ.get("DB_PATH", "tracker.db")

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
        """
    )
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
    """List all trackers."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM trackers ORDER BY created_at DESC"
    ).fetchall()
    trackers = [dict(r) for r in rows]
    # Parse division_ids back to list
    for t in trackers:
        t["division_ids"] = json.loads(t["division_ids"])
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
            notify_email, notify_ntfy_topic, notify_sms_phone, notify_sms_carrier)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    if notif_type not in ("email", "ntfy", "sms"):
        return jsonify({"error": "type must be email, ntfy, or sms"}), 400

    result = send_test_notification(
        notif_type,
        email=data.get("email", ""),
        ntfy_topic=data.get("ntfy_topic", ""),
        sms_phone=data.get("sms_phone", ""),
        sms_carrier=data.get("sms_carrier", ""),
    )
    return jsonify(result)


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
# Start
# ---------------------------------------------------------------------------

init_db()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(poll_all_trackers, "interval", minutes=10, id="poller", max_instances=1)
scheduler.start()
log.info("Background scheduler started — polling every 10 minutes")

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
