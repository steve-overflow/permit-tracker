"""
AI-powered natural language understanding for the Permit Tracker bot.
Uses Claude Opus 4.6 via OpenRouter to parse user intent and extract parameters.
"""

import json
import logging
import os
from urllib.request import Request, urlopen

from tracker import _SSL_CTX

log = logging.getLogger("permit-bot.ai")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("AI_MODEL", "anthropic/claude-opus-4-6")

SYSTEM_PROMPT = """You are the AI brain of a recreation.gov permit tracker Telegram bot. Your job is to understand what the user wants and return structured JSON.

You help users:
1. Search for permits (campgrounds, hiking permits, river permits, etc.)
2. Track permits for availability notifications
3. Check their active trackers
4. Stop/cancel trackers
5. Answer questions about recreation.gov permits and how this bot works

ALWAYS respond with valid JSON matching one of these intents:

## Intent: search
User wants to find/search for a permit, or asks about availability.
```json
{"intent": "search", "query": "<permit name or search terms>", "months": ["july", "august"], "season": null, "year": 2026}
```
- Extract the permit NAME/LOCATION, stripping filler words (find, search, any, permits, available, etc.)
- Extract months or season if mentioned. Season can be "summer", "fall", "spring", "winter"
- If no date mentioned, set months to null (we'll default to next 3 months)
- Year defaults to current year, or next year if month already passed

## Intent: track
User explicitly wants to set up tracking/alerts for something (after seeing results).
```json
{"intent": "track", "selection": "all" | [1, 3] | "cancel"}
```
- "all" if they want to track all results
- Array of 1-based numbers for specific selections
- "cancel" if they want to cancel/skip

## Intent: show_details
User wants to see more details/availability for a specific result number.
```json
{"intent": "show_details", "number": 2}
```

## Intent: division_select
User is picking which divisions/sites to track.
```json
{"intent": "division_select", "selection": "all" | [1, 3, 5] | "skip"}
```

## Intent: confirm
User is confirming (yes/sure/ok) or denying (no/nah/cancel) a pending action.
```json
{"intent": "confirm", "value": true}
```
or
```json
{"intent": "confirm", "value": false}
```

## Intent: list_trackers
User wants to see their active trackers.
```json
{"intent": "list_trackers"}
```

## Intent: check_now
User wants to run an immediate availability check.
```json
{"intent": "check_now", "tracker_id": null}
```
- tracker_id is null for "check all", or a specific ID if mentioned

## Intent: stop_tracker
User wants to stop/cancel/delete a tracker.
```json
{"intent": "stop_tracker", "tracker_id": null}
```

## Intent: help
User needs help or is confused about how to use the bot.
```json
{"intent": "help"}
```

## Intent: report_bug
User is reporting an issue/bug/problem.
```json
{"intent": "report_bug", "message": "<their report text>"}
```

## Intent: question
User asks a general question about permits, recreation.gov, or the bot.
```json
{"intent": "question", "answer": "<helpful concise answer>"}
```
- Answer questions about recreation.gov, permit types, how the bot works, tips for getting permits
- Be helpful and knowledgeable about outdoor recreation permits
- Keep answers concise (2-3 sentences max for Telegram)

## Intent: greeting
User says hi/hello/hey.
```json
{"intent": "greeting"}
```

## Important rules:
- ONLY return valid JSON, no markdown, no explanation
- If the user types just a permit name with no explicit intent, assume "search"
- "Maroon Bells in August" → search intent
- "yes" or "track all" when in context of pending results → track intent
- "1 and 3" or "just 2" when in context → track intent with selection
- Be smart about extracting the actual permit/location name from natural language
- For questions about specific permits, recreation.gov policies, cancellation tips, etc. → use "question" intent and provide a helpful answer
"""


def parse_with_ai(user_text: str, context: str = "") -> dict | None:
    """Send user text to Claude Opus via OpenRouter and get structured intent JSON.
    
    Args:
        user_text: The raw message from the user
        context: Optional context about conversation state (e.g. "User has pending search results: [...]")
    
    Returns:
        Parsed intent dict, or None if AI call fails
    """
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY not set, falling back to keyword parsing")
        return None

    messages = []
    if context:
        messages.append({"role": "system", "content": SYSTEM_PROMPT + "\n\n## Current conversation context:\n" + context})
    else:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0,
    }

    try:
        req = Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://permit-tracker-production.up.railway.app",
                "X-Title": "Permit Tracker Bot",
            },
        )
        with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            result = json.loads(resp.read())
        
        content = result["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
        
        parsed = json.loads(content)
        log.info("AI parsed '%s' → %s", user_text[:50], parsed.get("intent", "unknown"))
        return parsed
    except json.JSONDecodeError as e:
        log.error("AI returned invalid JSON: %s (raw: %s)", e, content[:200] if 'content' in dir() else 'N/A')
        return None
    except Exception as e:
        log.error("AI parse failed: %s", e)
        return None
