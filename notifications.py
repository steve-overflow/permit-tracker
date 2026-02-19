"""
Notification senders for permit availability alerts.

Supports:
  - Telegram messages via Bot API (primary — best for non-technical users)
  - Push notifications via ntfy.sh (secondary — free, instant)
  - Email via Resend API (optional)
  - SMS via email-to-SMS carrier gateways (requires verified Resend domain)
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

# Configurable sender address.
# IMPORTANT: The default onboarding@resend.dev sandbox sender can ONLY deliver
# to the Resend account owner's verified email address. It CANNOT send to
# arbitrary addresses or carrier SMS gateways (e.g. 5551234567@vtext.com).
# To send real emails/SMS, set RESEND_FROM_EMAIL to an address on a verified
# domain in your Resend account (e.g. "alerts@yourdomain.com").
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
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
# Carrier gateways for email-to-SMS
# ---------------------------------------------------------------------------

CARRIER_GATEWAYS = {
    "verizon": "@vtext.com",
    "att": "@txt.att.net",
    "tmobile": "@tmomail.net",
    "sprint": "@messaging.sprintpcs.com",
}


def _build_message(permit_name: str, slots: list) -> str:
    """Build a human-readable message from available slots."""
    lines = [f"Permit: {permit_name}", ""]
    for s in slots:
        lines.append(
            f"  {s['date']} - {s['division_name']}: "
            f"{s['remaining']}/{s['total']} spots"
        )
    if slots:
        lines.append("")
        lines.append(
            f"Book now: https://www.recreation.gov/permits/{slots[0]['permit_id']}"
        )
    return "\n".join(lines)


def _build_sms_message(permit_name: str, slots: list) -> str:
    """Build a short SMS-friendly message."""
    count = len(slots)
    dates = ", ".join(s["date"] for s in slots[:3])
    if count > 3:
        dates += f" +{count - 3} more"
    pid = slots[0]["permit_id"] if slots else ""
    return (
        f"Permit available: {permit_name}\n"
        f"{dates}\n"
        f"https://www.recreation.gov/permits/{pid}"
    )


# ---------------------------------------------------------------------------
# Resend email
# ---------------------------------------------------------------------------

def send_email(to_email: str, permit_name: str, slots: list) -> bool:
    """Send an email notification via Resend API."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("RESEND_API_KEY not set — skipping email to %s", to_email)
        return False

    body = _build_message(permit_name, slots)
    payload = json.dumps(
        {
            "from": f"Permit Tracker <{RESEND_FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Permit Available: {permit_name}",
            "text": body,
        }
    ).encode()

    req = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            log.info("Email sent to %s (status %s)", to_email, resp.status)
            return True
    except Exception as e:
        log.error("Failed to send email to %s: %s", to_email, e)
        return False


# ---------------------------------------------------------------------------
# ntfy.sh push notification
# ---------------------------------------------------------------------------

def send_ntfy(topic: str, permit_name: str, slots: list) -> bool:
    """Send a push notification via ntfy.sh."""
    if not topic:
        return False

    body = _build_message(permit_name, slots)
    url = f"https://ntfy.sh/{topic}"
    req = Request(
        url,
        data=body.encode(),
        headers={
            "Title": f"Permit Available: {permit_name}",
            "Priority": "high",
            "Tags": "national-park,hiking",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            log.info("ntfy sent to topic '%s' (status %s)", topic, resp.status)
            return True
    except Exception as e:
        log.error("Failed to send ntfy to '%s': %s", topic, e)
        return False


# ---------------------------------------------------------------------------
# Email-to-SMS
# ---------------------------------------------------------------------------

def send_sms(phone: str, carrier: str, permit_name: str, slots: list) -> bool:
    """Send SMS via email-to-SMS gateway using Resend."""
    gateway = CARRIER_GATEWAYS.get(carrier)
    if not gateway:
        log.error("Unknown carrier: %s", carrier)
        return False

    # Strip non-digits from phone
    clean_phone = "".join(c for c in phone if c.isdigit())
    if len(clean_phone) != 10:
        log.error("Phone number must be 10 digits, got: %s", phone)
        return False

    sms_email = f"{clean_phone}{gateway}"
    body = _build_sms_message(permit_name, slots)

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("RESEND_API_KEY not set — skipping SMS to %s", sms_email)
        return False

    payload = json.dumps(
        {
            "from": f"Permit Tracker <{RESEND_FROM_EMAIL}>",
            "to": [sms_email],
            "subject": "Permit Alert",
            "text": body,
        }
    ).encode()

    req = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            log.info("SMS sent to %s (status %s)", sms_email, resp.status)
            return True
    except Exception as e:
        log.error("Failed to send SMS to %s: %s", sms_email, e)
        return False


# ---------------------------------------------------------------------------
# Dispatch — send all configured notifications for a tracker
# ---------------------------------------------------------------------------

def send_test_notification(notif_type: str, **kwargs) -> dict:
    """
    Send a single test notification.

    Args:
        notif_type: "email", "ntfy", or "sms"
        kwargs: email=..., ntfy_topic=..., sms_phone=..., sms_carrier=...

    Returns:
        {"type": ..., "target": ..., "success": bool, "error": str|None}
    """
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

        elif notif_type == "email":
            email = kwargs.get("email", "")
            if not email:
                return {"type": "email", "target": "", "success": False, "error": "No email provided"}
            ok = send_email(email, test_permit, test_slots)
            return {"type": "email", "target": email, "success": ok, "error": None if ok else "Send failed (check RESEND_API_KEY)"}

        elif notif_type == "ntfy":
            topic = kwargs.get("ntfy_topic", "")
            if not topic:
                return {"type": "ntfy", "target": "", "success": False, "error": "No ntfy topic provided"}
            ok = send_ntfy(topic, test_permit, test_slots)
            return {"type": "ntfy", "target": topic, "success": ok, "error": None if ok else "Send failed"}

        elif notif_type == "sms":
            phone = kwargs.get("sms_phone", "")
            carrier = kwargs.get("sms_carrier", "")
            if not phone or not carrier:
                return {"type": "sms", "target": phone, "success": False, "error": "Phone and carrier required"}
            ok = send_sms(phone, carrier, test_permit, test_slots)
            return {"type": "sms", "target": phone, "success": ok, "error": None if ok else "Send failed (check RESEND_API_KEY)"}

        else:
            return {"type": notif_type, "target": "", "success": False, "error": f"Unknown type: {notif_type}"}
    except Exception as e:
        return {"type": notif_type, "target": "", "success": False, "error": str(e)}


def send_all_notifications(tracker_config: dict, permit_name: str, slots: list) -> list:
    """
    Send notifications based on tracker configuration.

    tracker_config should have keys like:
        notify_email, notify_ntfy_topic, notify_sms_phone, notify_sms_carrier

    Returns list of result dicts.
    """
    results = []

    telegram_chat_id = tracker_config.get("notify_telegram_chat_id")
    if telegram_chat_id:
        ok = send_telegram(telegram_chat_id, permit_name, slots)
        results.append({"type": "telegram", "target": telegram_chat_id, "success": ok})

    email = tracker_config.get("notify_email")
    if email:
        ok = send_email(email, permit_name, slots)
        results.append({"type": "email", "target": email, "success": ok})

    ntfy_topic = tracker_config.get("notify_ntfy_topic")
    if ntfy_topic:
        ok = send_ntfy(ntfy_topic, permit_name, slots)
        results.append({"type": "ntfy", "target": ntfy_topic, "success": ok})

    phone = tracker_config.get("notify_sms_phone")
    carrier = tracker_config.get("notify_sms_carrier")
    if phone and carrier:
        ok = send_sms(phone, carrier, permit_name, slots)
        results.append({"type": "sms", "target": phone, "success": ok})

    return results
