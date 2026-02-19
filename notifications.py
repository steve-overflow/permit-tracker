"""
Notification senders for permit availability alerts.

Supports:
  - Telegram messages via Bot API
"""

import json
import logging
import os
import ssl
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# Reuse the SSL context from tracker
try:
    from tracker import _SSL_CTX
except Exception:
    _SSL_CTX = ssl._create_unverified_context()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ---------------------------------------------------------------------------
# Telegram Bot notification
# ---------------------------------------------------------------------------

def send_telegram(chat_id: str, permit_name: str, slots: list) -> bool:
    """Send a permit alert via Telegram bot message."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram not configured — skipping")
        return False

    # Build a nice Telegram message with emoji
    lines = [f"🏔️ *Permit Available: {permit_name}*", ""]
    for s in slots[:10]:  # Limit to 10 slots in message
        lines.append(f"📅 {s['date']} — {s['division_name']}: *{s['remaining']}/{s['total']}* spots")
    if len(slots) > 10:
        lines.append(f"...and {len(slots) - 10} more")
    if slots:
        lines.append("")
        lines.append(f"🔗 [Book now](https://www.recreation.gov/permits/{slots[0]['permit_id']})")

    text = "\n".join(lines)
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()

    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("Telegram sent to chat %s", chat_id)
                return True
            log.error("Telegram API error: %s", result)
            return False
    except Exception as e:
        log.error("Failed to send Telegram to %s: %s", chat_id, e)
        return False


# ---------------------------------------------------------------------------
# Test notification
# ---------------------------------------------------------------------------

def send_test_notification(notif_type: str, **kwargs) -> dict:
    """Send a single test notification (Telegram only)."""
    test_permit = "Test Permit"
    test_slots = [
        {
            "permit_id": "000000",
            "division_id": "0",
            "division_name": "Test Division",
            "date": "Mon Jan 01, 2099",
            "date_raw": "2099-01-01T00:00:00Z",
            "remaining": 5,
            "total": 10,
        }
    ]

    try:
        if notif_type == "telegram":
            chat_id = kwargs.get("telegram_chat_id", "")
            if not chat_id:
                return {"type": "telegram", "target": "", "success": False, "error": "No Telegram chat ID"}
            ok = send_telegram(chat_id, test_permit, test_slots)
            return {"type": "telegram", "target": chat_id, "success": ok, "error": None if ok else "Send failed (check TELEGRAM_BOT_TOKEN)"}
        else:
            return {"type": notif_type, "target": "", "success": False, "error": f"Unknown type: {notif_type}"}
    except Exception as e:
        return {"type": notif_type, "target": "", "success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Dispatch — send all configured notifications for a tracker
# ---------------------------------------------------------------------------

def send_all_notifications(tracker_config: dict, permit_name: str, slots: list) -> list:
    """Send notifications based on tracker configuration (Telegram only)."""
    results = []

    telegram_chat_id = tracker_config.get("notify_telegram_chat_id")
    if telegram_chat_id:
        ok = send_telegram(telegram_chat_id, permit_name, slots)
        results.append({"type": "telegram", "target": telegram_chat_id, "success": ok})

    return results
