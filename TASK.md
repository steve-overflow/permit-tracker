# Updates Needed

## 1. Support "itinerary" permit availability (Maroon Bells, etc)

Recreation.gov has TWO different availability APIs:

### Standard permits (Half Dome, Desolation, etc)
- Info: `/api/permits/{id}` → payload.divisions
- Availability: `/api/permits/{id}/availability?start_date={iso}&end_date={iso}`
- Returns: payload.availability.{div_id}.date_availability.{date}.{remaining, total}

### Newer "itinerary" permits (Maroon Bells, Indian Peaks, Dolores River, etc)
- Info: `/api/permitcontent/{id}` → payload.divisions  
- Availability: `/api/permititinerary/{id}/division/{div_id}/availability/month?month={m}&year={y}`
- Returns: payload.quota_type_maps.ConstantQuotaUsageDaily.{date}.{remaining, total}
- Also has payload.bools.{date} = true/false for quick availability check
- MUST be queried per-division, per-month

### How to know which type:
- `/api/permitcontent/permitmapping` returns categories:
  - `itinerary_permit_ids` → use permititinerary API
  - `water_permit_ids` → use permititinerary API  
  - Others (day_use, lottery, etc) → use standard API
- Fallback: if `/api/permits/{id}` returns error, try permitcontent

### Changes needed in tracker.py:
- `get_permit_info()` already falls back to permitcontent - GOOD
- `check_availability()` needs to detect permit type and use the right API
- For itinerary permits: loop through months in the date range, query each division

## 2. Add "Send Test Notification" buttons to the web UI

In the tracking config section, add a "Test" button next to each notification type:
- Test Email → sends a test email to the entered address via Resend
- Test ntfy → sends a test push to the entered topic
- Test SMS → sends a test text via email-to-SMS gateway

Add API endpoint: POST /api/test-notification
Body: { "type": "email|ntfy|sms", "email": "...", "ntfy_topic": "...", "sms_phone": "...", "sms_carrier": "..." }

## 3. Browser cache issue
The frontend JS may be cached. Add cache-busting: 
- In index.html, append `?v={timestamp}` to the CSS and JS includes
- Or add `Cache-Control: no-cache` headers for static files
