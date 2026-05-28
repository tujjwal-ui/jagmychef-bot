import os
import json
import anthropic
from datetime import datetime, date
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# ── Clients ─────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

TWILIO_NUMBER   = os.environ["TWILIO_WHATSAPP_NUMBER"]   # whatsapp:+14155238886
USER_NUMBER     = os.environ["USER_WHATSAPP_NUMBER"]      # whatsapp:+1XXXXXXXXXX
SUMEGHA_NUMBER  = os.environ["SUMEGHA_WHATSAPP_NUMBER"]   # whatsapp:+1XXXXXXXXXX

# ── Persistent state files ───────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(__file__)
MENU_FILE  = os.path.join(BASE_DIR, "data", "menu.json")
STATE_FILE = os.path.join(BASE_DIR, "data", "state.json")

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

with open(MENU_FILE) as f:
    MENU = json.load(f)

# ── State helpers ────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "rules": {
        "slots": [
            {"label": "Chicken dish",    "category": "Indian", "filter": "chicken"},
            {"label": "Salad",           "category": "Salads", "filter": "any"},
            {"label": "Veg dish",        "category": "Indian", "filter": "vegetarian"},
            {"label": "Veg dish",        "category": "Indian", "filter": "vegetarian"},
        ],
        "no_repeat_weeks": 2,
        "rule_overrides_this_week": []
    },
    "current_picks": [None, None, None, None],
    "conversation_history": [],
    "selection_history": [],
    "confirmed": False,
    "week": None
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return DEFAULT_STATE.copy()

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_recent_items(state):
    no_repeat = state["rules"].get("no_repeat_weeks", 2)
    recent = []
    for entry in state["selection_history"][-no_repeat:]:
        recent.extend(entry.get("picks", []))
    return recent

def all_items_flat():
    items = []
    for category, dishes in MENU["repository"].items():
        for dish in dishes:
            items.append({"name": dish, "category": category})
    return items

# ── Frequency map from history ───────────────────────────────────────────────
def build_frequency_summary(state):
    freq = {}
    for entry in state.get("selection_history", []):
        for pick in entry.get("picks", []):
            freq[pick] = freq.get(pick, 0) + 1
    if not freq:
        return "No history yet."
    sorted_items = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    lines = [f"  {dish} — selected {count}x" for dish, count in sorted_items[:20]]
    return "\n".join(lines)

# ── System prompt ────────────────────────────────────────────────────────────
def build_system_prompt(state):
    recent = get_recent_items(state)
    rules  = state["rules"]

    slots_desc = "\n".join(
        f"  Slot {i+1}: {s['label']} — category '{s['category']}'"
        + (f", filter '{s['filter']}'" if s["filter"] != "any" else "")
        for i, s in enumerate(rules["slots"])
    )

    current_picks_desc = "\n".join(
        f"  Slot {i+1} ({rules['slots'][i]['label']}): {p or 'EMPTY'}"
        for i, p in enumerate(state["current_picks"])
    )

    recent_desc   = "\n".join(f"  - {r}" for r in recent) if recent else "  None"
    freq_summary  = build_frequency_summary(state)

    return f"""You are the Chef Jag meal selection assistant for a WhatsApp group chat.
Every Thursday you help two people (the user and Sumegha) pick 4 dishes for Chef Jag to cook on Monday.
This is a casual WhatsApp conversation — be warm, concise, friendly.

=== FULL MENU ===
{json.dumps(MENU["repository"], indent=2)}

=== CURRENT SELECTION RULES ===
{slots_desc}
No-repeat window: {rules['no_repeat_weeks']} weeks

=== CURRENT PICKS THIS WEEK ===
{current_picks_desc}

=== DO NOT REPEAT (last {rules['no_repeat_weeks']} weeks) ===
{recent_desc}

=== SELECTION FREQUENCY (most picked = likely favourites) ===
{freq_summary}

=== YOUR CAPABILITIES ===

1. INITIAL 8 SUGGESTIONS (Thursday trigger):
   Present 8 options — double for each slot — so they can choose 4 with less back-and-forth.
   Format as pairs, labeled A through H:
   
   🍗 *Chicken* (pick 1):
   A. [dish] — [one-line reason]
   B. [dish] — [one-line reason]
   
   🥗 *Salad* (pick 1):
   C. [dish] — [one-line reason]
   D. [dish] — [one-line reason]
   
   🌿 *Veg dish* (pick 1):
   E. [dish] — [one-line reason]
   F. [dish] — [one-line reason]
   
   🌿 *Veg dish* (pick 1):
   G. [dish] — [one-line reason]
   H. [dish] — [one-line reason]
   
   End with: "Reply with 4 letters (e.g. A, C, E, G) or ask for swaps!"
   Use frequency data to favour dishes they haven't had recently or love.

2. LETTER SELECTION: When they reply with letters (e.g. "A C F H" or "A, C, F, H"):
   Map the letters to the dishes and confirm the 4 picks clearly.
   Ask "Happy with these? Reply *confirm* to lock them in!"

3. SMART SWAP: For specific requests ("I want a kebab", "something with paneer", 
   "lighter salad", "salad with cucumber") — use culinary reasoning to find the 
   best menu match. For ingredients not in dish names, say "likely contains".

4. ECHO SENDER: Start every response (except the initial Thursday message) with:
   [You]: or [Sumegha]: — whoever sent the last message — so both people 
   know who triggered what.

5. RULE CHANGES: Detect rule change requests:
   - "change to 2 chicken dishes" → update slots
   - "no Indian this week" → this-week override
   - "from now on include a soup" → permanent change
   - "Sumegha is vegetarian this week" → note and adjust
   Confirm what changed, then re-suggest affected slots.
   Emit at end of message:
   <<<RULE_CHANGE>>>
   {{"type": "this_week" or "permanent", "description": "...", "new_slots": [...] or null}}
   <<<END>>>

6. CONFIRMATION: When they say "confirm", "looks good", "perfect", "lock it in":
   Output a summary and emit at end:
   <<<CONFIRMED>>>
   {{"picks": ["dish1", "dish2", "dish3", "dish4"]}}
   <<<END>>>

7. USE HISTORY INTELLIGENTLY:
   - Favour dishes they pick frequently (from frequency data above) but respect no-repeat window
   - If a dish has never been picked, flag it as "haven't tried this yet!"
   - Avoid dishes marked disliked in preferences

=== RESPONSE STYLE ===
- WhatsApp style: short, warm, no markdown headers (use *bold* for WhatsApp bold)
- Initial suggestions: use the A-H pair format above
- Swaps: just show new dish + what it replaces
- Confirmations: clean numbered list of final 4
- Max ~300 words for initial, ~120 for swaps/replies
"""

# ── Claude call ──────────────────────────────────────────────────────────────
def ask_claude(state, user_message, sender_name):
    history = state.get("conversation_history", [])
    history.append({"role": "user", "content": f"[{sender_name}]: {user_message}"})

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=build_system_prompt(state),
        messages=history
    )

    reply_text = response.content[0].text
    history.append({"role": "assistant", "content": reply_text})
    state["conversation_history"] = history[-30:]
    return reply_text

# ── Parse Claude response ────────────────────────────────────────────────────
def parse_reply(reply, state):
    clean_reply = reply

    if "<<<CONFIRMED>>>" in reply:
        try:
            block = reply.split("<<<CONFIRMED>>>")[1].split("<<<END>>>")[0].strip()
            data  = json.loads(block)
            picks = data.get("picks", [])
            if len(picks) == 4:
                state["current_picks"] = picks
                state["confirmed"]     = True
                week_str               = date.today().isoformat()
                state["selection_history"].append({
                    "week":     week_str,
                    "picks":    picks,
                    "feedback": []
                })
                state["conversation_history"] = []
                state["week"] = week_str
        except Exception as e:
            print(f"Confirmation parse error: {e}")
        clean_reply = reply.split("<<<CONFIRMED>>>")[0].strip()

    if "<<<RULE_CHANGE>>>" in reply:
        try:
            block = reply.split("<<<RULE_CHANGE>>>")[1].split("<<<END>>>")[0].strip()
            data  = json.loads(block)
            if data.get("new_slots"):
                if data.get("type") == "permanent":
                    state["rules"]["slots"] = data["new_slots"]
                else:
                    state["rules"]["rule_overrides_this_week"] = data["new_slots"]
        except Exception as e:
            print(f"Rule change parse error: {e}")
        clean_reply = reply.split("<<<RULE_CHANGE>>>")[0].strip()

    return clean_reply, state

# ── Twilio send ──────────────────────────────────────────────────────────────
def send_whatsapp(message, to_number):
    twilio_client.messages.create(
        from_=TWILIO_NUMBER,
        body=message,
        to=to_number
    )

def broadcast(message):
    send_whatsapp(message, USER_NUMBER)
    send_whatsapp(message, SUMEGHA_NUMBER)

# ── Thursday trigger ─────────────────────────────────────────────────────────
@app.route("/trigger", methods=["GET", "POST"])
def thursday_trigger():
    state = load_state()
    state["current_picks"]   = [None, None, None, None]
    state["confirmed"]       = False
    state["conversation_history"] = []
    state["week"]            = date.today().isoformat()

    if state["rules"].get("rule_overrides_this_week"):
        state["rules"]["slots"] = state["rules"]["rule_overrides_this_week"]
        state["rules"]["rule_overrides_this_week"] = []

    today   = datetime.now().strftime("%A, %B %d")
    prompt  = (
        f"It's Thursday {today}. Generate the initial 8 suggestions "
        f"(2 per slot, A-H format) following current rules and history."
    )

    reply = ask_claude(state, prompt, "System")
    clean_reply, state = parse_reply(reply, state)

    save_state(state)
    broadcast(clean_reply)
    return "OK", 200

# ── Incoming WhatsApp ────────────────────────────────────────────────────────
@app.route("/whatsapp", methods=["POST"])
def whatsapp_incoming():
    from_number = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()

    if from_number == USER_NUMBER:
        sender_name = "You"
    elif from_number == SUMEGHA_NUMBER:
        sender_name = "Sumegha"
    else:
        return str(MessagingResponse()), 200

    state = load_state()
    reply = ask_claude(state, body, sender_name)
    clean_reply, state = parse_reply(reply, state)

    save_state(state)
    broadcast(clean_reply)
    return str(MessagingResponse()), 200

# ── Seed history from email data ─────────────────────────────────────────────
@app.route("/seed_history", methods=["POST"])
def seed_history():
    """
    POST JSON: {"history": [{"week": "2026-01-09", "picks": ["dish1","dish2","dish3","dish4"]}, ...]}
    Used to bootstrap selection history from past Grocery Genie emails.
    """
    data  = request.get_json()
    state = load_state()
    new_entries = data.get("history", [])
    existing_weeks = {e["week"] for e in state["selection_history"]}
    added = 0
    for entry in new_entries:
        if entry["week"] not in existing_weeks:
            state["selection_history"].append({
                "week":     entry["week"],
                "picks":    entry["picks"],
                "feedback": entry.get("feedback", [])
            })
            added += 1
    state["selection_history"].sort(key=lambda x: x["week"])
    save_state(state)
    return {"seeded": added, "total": len(state["selection_history"])}, 200

# ── Health check ─────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    state = load_state()
    return {
        "status":  "ok",
        "date":    date.today().isoformat(),
        "history": len(state.get("selection_history", [])),
        "confirmed_this_week": state.get("confirmed", False)
    }, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
