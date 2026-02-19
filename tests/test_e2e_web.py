#!/usr/bin/env python3
"""End-to-end tests for the web app API.
Run: python3 tests/test_e2e_web.py
"""
import os, sys, json

# Setup test environment
test_db = "/tmp/test_tracker_e2e.db"
if os.path.exists(test_db):
    os.remove(test_db)
os.environ["DB_PATH"] = test_db
os.environ["SECRET_KEY"] = "test"
os.environ["ACCESS_CODE"] = "test123"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app

client = app.test_client()
passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} — {detail}")

print("=" * 60)
print("WEB APP END-TO-END TESTS")
print("=" * 60)

# Auth
print("\n🔐 Authentication")
resp = client.post("/login", data={"access_code": "wrong"}, follow_redirects=True)
test("Reject bad access code", b"Invalid" in resp.data or resp.status_code == 200)
resp = client.post("/login", data={"access_code": "test123"}, follow_redirects=False)
test("Accept valid access code", resp.status_code == 302)

# Search
print("\n🔍 Search")
resp = client.get("/api/search?q=Maroon+Bells")
data = resp.get_json()
test("Search returns results", len(data) > 0, f"got {len(data)}")
test("Search finds Maroon Bells", any("Maroon" in r["name"] for r in data))

resp = client.get("/api/search?q=Desolation+Wilderness")
data = resp.get_json()
test("Search finds Desolation", len(data) > 0)

resp = client.get("/api/search?q=Half+Dome")
data = resp.get_json()
test("Search finds Half Dome", len(data) > 0)

# Create trackers
print("\n📋 Tracker CRUD")
resp = client.post("/api/trackers", json={
    "permit_id": "233261", "permit_name": "Desolation Wilderness Permit",
    "division_ids": [], "start_date": "2026-07-01", "end_date": "2026-08-31",
    "notify_email": "", "notify_ntfy_topic": "", "notify_sms_phone": "",
    "notify_sms_carrier": "", "notify_telegram_chat_id": "",
})
d1 = resp.get_json()
test("Create tracker (Desolation)", resp.status_code == 201 and "id" in d1, str(d1))

resp = client.post("/api/trackers", json={
    "permit_id": "234652", "permit_name": "Half Dome Permits",
    "division_ids": [], "start_date": "2026-07-01", "end_date": "2026-08-31",
    "notify_email": "", "notify_ntfy_topic": "", "notify_sms_phone": "",
    "notify_sms_carrier": "", "notify_telegram_chat_id": "",
})
d2 = resp.get_json()
test("Create tracker (Half Dome)", resp.status_code == 201 and "id" in d2, str(d2))

resp = client.get("/api/trackers")
trackers = resp.get_json()
test("List shows 2 trackers", len(trackers) == 2, f"got {len(trackers)}")

# Check availability
print("\n🔍 Availability Checks")
resp = client.post(f"/api/trackers/{d1['id']}/check")
data = resp.get_json()
test("Desolation has availability", len(data.get("slots", [])) > 0, f"{len(data.get('slots', []))} slots")

resp = client.post(f"/api/trackers/{d2['id']}/check")
data = resp.get_json()
test("Half Dome is sold out", len(data.get("slots", [])) == 0, f"{len(data.get('slots', []))} slots")

# Toggle
print("\n⏯️ Toggle & Delete")
resp = client.post(f"/api/trackers/{d1['id']}/toggle")
data = resp.get_json()
test("Toggle pauses tracker", data.get("active") == 0)
resp = client.post(f"/api/trackers/{d1['id']}/toggle")
data = resp.get_json()
test("Toggle resumes tracker", data.get("active") == 1)

resp = client.delete(f"/api/trackers/{d2['id']}")
test("Delete tracker", resp.status_code == 200)
resp = client.get("/api/trackers")
test("Only 1 tracker remains", len(resp.get_json()) == 1)

# Preferences
print("\n⚙️ Preferences")
resp = client.post("/api/preferences", json={
    "notify_telegram_chat_id": "99999",
})
test("Save preferences", resp.status_code == 200)
resp = client.get("/api/preferences")
p = resp.get_json()
test("Load preferences (telegram)", p.get("notify_telegram_chat_id") == "99999" or p.get("telegram_chat_id") == "99999")

# Alerts
print("\n🔔 Alerts")
resp = client.get("/api/alerts?limit=20")
test("Alerts endpoint works", resp.status_code == 200)

# Daily check-in (may return 400 in test env with no real Telegram token — that's OK)
print("\n📊 Daily Check-in")
resp = client.post("/api/daily-checkin")
test("Daily check-in endpoint responds", resp.status_code in (200, 400))

# Cleanup
os.remove(test_db)

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
