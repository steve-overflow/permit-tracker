#!/usr/bin/env python3
"""End-to-end tests for the Telegram bot logic (simulated messages, no real Telegram API).
Run: python3 tests/test_e2e_telegram.py
"""
import os, sys, json, time

# Setup
test_db = "/tmp/test_bot_e2e.db"
if os.path.exists(test_db):
    os.remove(test_db)
os.environ["DB_PATH"] = test_db
os.environ["SECRET_KEY"] = "test"
os.environ["ACCESS_CODE"] = "permit2025!"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake:token")  # Prevent crashes on import

# Set allowed users file to temp
os.environ["ALLOWED_USERS_FILE"] = "/tmp/test_allowed_users.json"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Intercept Telegram sends
sent_messages = []
import bot as bot_module

original_send = bot_module.send_message
def mock_send(chat_id, text, **kwargs):
    sent_messages.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
    # Don't actually call Telegram
bot_module.send_message = mock_send

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

def last_msg():
    return sent_messages[-1]["text"] if sent_messages else ""

def clear():
    sent_messages.clear()

CHAT_ID = 12345
USER_ID = 67890

print("=" * 60)
print("TELEGRAM BOT END-TO-END TESTS")
print("=" * 60)

# Auth
print("\n🔐 Authentication")
clear()
bot_module.handle_start(CHAT_ID, USER_ID, "", "TestUser")  # No args = no code
test("Start without code shows auth prompt", "access code" in last_msg().lower() or "requires" in last_msg().lower(), last_msg()[:100])

clear()
bot_module.handle_start(CHAT_ID, USER_ID, "wrongcode", "TestUser")
test("Wrong code rejected", "invalid" in last_msg().lower() or "incorrect" in last_msg().lower(), last_msg()[:100])

clear()
bot_module.handle_start(CHAT_ID, USER_ID, "permit2025!", "TestUser")
test("Correct code accepted", "welcome" in last_msg().lower() or "granted" in last_msg().lower() or "✅" in last_msg(), last_msg()[:100])

# Help
print("\n📖 Help")
clear()
bot_module.handle_help(CHAT_ID)
test("Help shows commands", "track" in last_msg().lower() or "search" in last_msg().lower(), last_msg()[:100])

# Search — natural language
print("\n🔍 Natural Language Search")
clear()
# Already authorized from handle_start above
bot_module.handle_natural_language(CHAT_ID, USER_ID, "TestUser", "maroon bells permits in july")
# Give it a moment — this calls recreation.gov
time.sleep(3)
test("NL search sends response", len(sent_messages) > 0, f"sent {len(sent_messages)} messages")
all_text = " ".join(m["text"] for m in sent_messages)
test("NL search finds Maroon Bells", "maroon" in all_text.lower() or "searching" in all_text.lower(), all_text[:200])

# Search — sold out
print("\n🔍 Sold Out Permit")
clear()
bot_module.handle_natural_language(CHAT_ID, USER_ID, "TestUser", "half dome permits august")
time.sleep(3)
all_text = " ".join(m["text"] for m in sent_messages)
test("Half Dome search responds", len(sent_messages) > 0, f"sent {len(sent_messages)} msgs")

# Chat ID command
print("\n💬 Utility Commands")
clear()
bot_module.handle_chatid(CHAT_ID)
test("/chatid shows chat ID", str(CHAT_ID) in last_msg(), last_msg()[:100])

# Daily check-in
print("\n📊 Daily Check-in")
clear()
bot_module.handle_test(CHAT_ID)
test("/test runs check-in", len(sent_messages) > 0, f"sent {len(sent_messages)} msgs")
all_text = " ".join(m["text"] for m in sent_messages)
test("Check-in mentions daily", "check-in" in all_text.lower() or "check" in all_text.lower(), all_text[:200])

# Selection flow
print("\n🎯 Selection & Tracking")
clear()
# Manually set up a conversation state to test selection
from bot import _store_conv_state, handle_selection, CONV_STATE
_store_conv_state(CHAT_ID, [
    {"id": "233261", "name": "Desolation Wilderness", "location": "CA"},
    {"id": "234652", "name": "Half Dome", "location": "CA"},
], "2026-07-01", "2026-08-31")

# Test cancel
clear()
result = handle_selection(CHAT_ID, USER_ID, "cancel")
test("Cancel clears state", result and "cancel" in last_msg().lower(), last_msg()[:100])

# Test selection with number
_store_conv_state(CHAT_ID, [
    {"id": "233261", "name": "Desolation Wilderness", "location": "CA"},
], "2026-07-01", "2026-08-31")
clear()
result = handle_selection(CHAT_ID, USER_ID, "1")
time.sleep(2)
test("Number selection handled", result, f"returned {result}")
all_text = " ".join(m["text"] for m in sent_messages)
test("Selection creates tracker or shows divisions", "tracker" in all_text.lower() or "sites" in all_text.lower() or "division" in all_text.lower(), all_text[:200])

# Cleanup
if os.path.exists(test_db):
    os.remove(test_db)

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
