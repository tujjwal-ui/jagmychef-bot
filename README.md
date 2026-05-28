# Chef Jag WhatsApp Bot — Deployment Guide

## What you need before starting
- Twilio account with a WhatsApp-capable number ✅ (you have this)
- New Anthropic API key (from console.anthropic.com — rotate the one you shared)
- GitHub account (free)
- Render account (free, render.com)
- cron-job.org account (free)

---

## Step 1 — Push code to GitHub

```bash
# On your computer, open Terminal and run:
cd ~/Desktop
git clone https://github.com/YOUR_USERNAME/jagmychef-bot  # or create new repo
# Copy all files from this folder into it, then:
cd jagmychef-bot
git add .
git commit -m "initial deploy"
git push
```

Or easier: go to github.com → New Repository → name it `jagmychef-bot` → 
upload all files via the web UI (drag and drop).

---

## Step 2 — Deploy to Render

1. Go to **render.com** → Sign up free with GitHub
2. Click **New** → **Web Service**
3. Connect your `jagmychef-bot` GitHub repo
4. Render auto-detects Python. Confirm:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
5. Click **Advanced** → **Add Environment Variable** — add ALL of these:

| Key | Value |
|-----|-------|
| `ANTHROPIC_API_KEY` | your new Anthropic key |
| `TWILIO_ACCOUNT_SID` | from twilio.com/console |
| `TWILIO_AUTH_TOKEN` | from twilio.com/console |
| `TWILIO_WHATSAPP_NUMBER` | `whatsapp:+1XXXXXXXXXX` (your Twilio number) |
| `USER_WHATSAPP_NUMBER` | `whatsapp:+1XXXXXXXXXX` (your number) |
| `SUMEGHA_WHATSAPP_NUMBER` | `whatsapp:+1XXXXXXXXXX` (Sumegha's number) |

6. Click **Create Web Service**
7. Wait ~3 minutes for deploy. You'll get a URL like:
   `https://jagmychef-bot.onrender.com`

---

## Step 3 — Connect Twilio webhook

1. Go to **twilio.com/console**
2. Navigate to **Messaging** → **Senders** → **WhatsApp Senders**
3. Click your number → **Sandbox Settings** (or number config)
4. Set **"When a message comes in"** to:
   ```
   https://jagmychef-bot.onrender.com/whatsapp
   ```
   Method: `HTTP POST`
5. Save

---

## Step 4 — Test it manually

Visit this URL in your browser to trigger a test suggestion right now:
```
https://jagmychef-bot.onrender.com/trigger
```
You and Sumegha should both receive a WhatsApp message within ~15 seconds.
(First time on Render free tier may take 30–60s to cold-start.)

Also test the health check:
```
https://jagmychef-bot.onrender.com/health
```

---

## Step 5 — Set up Thursday morning trigger

1. Go to **cron-job.org** → Create free account
2. Click **Create cronjob**
3. URL: `https://jagmychef-bot.onrender.com/trigger`
4. Schedule: **Every Thursday at 9:00 AM** (your timezone)
   - Cron expression: `0 9 * * 4`
5. Save → Enable

That's it. Every Thursday at 9am, both of you get the suggestions automatically.

---

## How the WhatsApp conversation works

**Thursday morning — bot sends:**
```
Good morning! Here are this week's 4 picks for Chef Jag 👨‍🍳

1. Chicken Kofta Curry — rich and hearty
2. Kale Squash Fig & Chickpea Salad — seasonal and light
3. Mumbai Style Pav Bhaji — crowd favourite
4. Bihari Chana Ghugni — great protein-packed option

Reply to swap, request something specific, or say "confirmed" when happy!
```

**You or Sumegha can reply:**
- `"swap the salad"` → random new salad
- `"I want something with paneer"` → smart match from menu
- `"suggest a kebab dish"` → Claude reasons across full menu
- `"change to 2 chicken dishes this week"` → rule override for this week
- `"from now on include a soup"` → permanent rule change
- `"confirmed"` or `"looks good"` → locks in picks, saves to history

**Both of you see every reply** — it feels like a 3-person group chat.

---

## File structure
```
jagmychef-bot/
├── app.py              ← main webhook (Flask)
├── requirements.txt    ← Python dependencies
├── Procfile            ← tells Render how to start
├── render.yaml         ← Render config
├── .gitignore
└── data/
    ├── menu.json       ← full Chef Jag menu (never changes)
    └── state.json      ← auto-created: picks, history, rules (persists)
```

---

## Changing rules via WhatsApp (examples)

| You say | What happens |
|---------|-------------|
| `"change to 2 chicken 1 salad 1 veg"` | Updates slots for this week |
| `"from now on always include a soup"` | Permanent rule change saved |
| `"no repeat for 3 weeks instead of 2"` | Updates no-repeat window |
| `"Sumegha is vegetarian this week"` | Claude notes it and adjusts |
| `"pick something light, we had heavy food all week"` | Claude uses judgment |

---

## Troubleshooting

**Bot not responding?**
- Check Render logs: render.com → your service → Logs
- Make sure Twilio webhook URL is exactly right (no trailing slash)
- Render free tier sleeps after 15min — first message each Thursday may take 30s

**Messages not sending to both?**
- Confirm both WhatsApp numbers are in env vars with `whatsapp:` prefix
- Both numbers must have messaged your Twilio sandbox first (Twilio requirement for sandbox)

**To wake up Render before Thursday:**
- Visit `https://jagmychef-bot.onrender.com/health` on Wednesday night
- Or upgrade to Render Starter ($7/mo) for always-on
