# Permit Tracker

Web app that monitors recreation.gov permit availability and sends notifications when slots open up.

## Features

- Search recreation.gov permits by name
- Track multiple permits with custom date ranges and divisions
- Background polling every 10 minutes with deduplication
- Notifications via email (Resend), push (ntfy.sh), and SMS (email-to-SMS gateways)
- Mobile-friendly UI

## Local Development

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `RESEND_API_KEY` | For email/SMS | API key from [resend.com](https://resend.com) |
| `SECRET_KEY` | Recommended | Flask session secret (defaults to dev key) |
| `PORT` | No | Server port (default: 5000) |

## Deploy to Railway

1. Push this repo to GitHub
2. Create a new project on [railway.app](https://railway.app)
3. Connect your GitHub repo
4. Add environment variables (`RESEND_API_KEY`, `SECRET_KEY`)
5. Deploy — Railway auto-detects the Procfile

## Notification Setup

### Email
Set `RESEND_API_KEY` and enter your email in the tracker form.

### ntfy.sh Push Notifications
1. Install the [ntfy app](https://ntfy.sh) on your phone
2. Subscribe to a topic name (e.g., `my-permit-alerts`)
3. Enter that same topic name when creating a tracker

### SMS
Enter your 10-digit phone number and select your carrier. Uses email-to-SMS gateways via Resend. Supported carriers: Verizon, AT&T, T-Mobile, Sprint.
