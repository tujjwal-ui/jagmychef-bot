import os
import json
import anthropic
from datetime import datetime, date
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# ── Clients ────────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

TWILIO_NUMBER   = os.environ["TWILIO_WHATSAPP_NUMBER"]   # e.g. whatsapp:+14155238886
USER_NUMBER     = os.environ["USER_WHATSAPP_NUMBER"]      # e.g. whatsapp:+12065551234
SUMEGHA_NUMBER  = os.environ["SUMEGHA_WHATSAPP_NUMBER"]   # e.g. whatsapp:+12065555678

# ── Persistent state files ─────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(__file__)
MENU_FILE       = os.path.join(BASE_DIR, "data", "menu.json")
STATE_FILE      = os.path.join(BASE_DIR, "data", "state.json")

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

# ── Load menu ──────────────────────────────────────────────────────────────────
with open(MENU_FILE) as f:
    MENU = json.load(f)

# ── State helpers ──────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "rules": {
        "slots": [
            {"label": "Chicken dish",      "category": "Indian",  "filter": "chicken"},
            {"label": "Salad",             "category": "Salads",  "filter": "any"},
            {"label": "Vegetarian dish",   "category": "Indian",  "filter": "vegetarian"},
            {"label": "Vegetarian dish",   "category": "Indian",  "filter": "vegetarian"},
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

# ── Recent items (no-repeat logic) ─────────────────────────────────────────────
def get_recent_items(state):
    no_repeat = state["rules"].get("no_repeat_weeks", 2)
    recent = []
    for entry in state["selection_history"][-no_repeat:]:
        recent.extend(entry.get("picks", []))
    return recent

# ── All items flat list ────────────────────────────────────────────────────────
def all_items_flat():
    items = []
    for category, dishes in MENU["repository"].items():
        for dish in dishes:
            items.append({"name": dish, "category": category})
    return items

# ── System prompt for Claude ───────────────────────────────────────────────────
def build_system_prompt(state):
    recent = get_recent_items(state)
    all_items = all_items_flat()
    rules = state["rules"]

    slots_desc = "\n".join(
        f"  Slot {i+1}: {s['label']} — pick from category '{s['category']}'"
        + (f" matching filter '{s['filter']}'" if s["filter"] != "any" else "")
        for i, s in enumerate(rules["slots"])
    )

    current_picks_desc = "\n".join(
        f"  Slot {i+1} ({rules['slots'][i]['label']}): {p or 'EMPTY'}"
        for i, p in enumerate(state["current_picks"])
    )

    recent_desc = "\n".join(f"  - {r}" for r in recent) if recent else "  None"

    return f"""You are the Chef Jag meal selection assistant for a WhatsApp group.
Every Thursday you suggest 4 dishes from Chef Jag's menu for Monday cooking.
You are helpful, warm, concise — this is a casual WhatsApp conversation, keep replies short.

=== FULL MENU (JSON) ===
{json.dumps(MENU["repository"], indent=2)}

=== CURRENT SELECTION RULES ===
{slots_desc}
No-repeat window: {rules['no_repeat_weeks']} weeks

=== CURRENT PICKS THIS WEEK ===
{current_picks_desc}

=== DO NOT REPEAT THESE (used in last {rules['no_repeat_weeks']} weeks) ===
{recent_desc}

=== YOUR CAPABILITIES ===
1. SUGGEST: Generate initial 4 picks following the slot rules and no-repeat window.
2. SMART SWAP: When a user requests a specific dish or style (e.g. "I want a kebab", 
   "suggest a salad with cucumber", "something lighter"), use culinary reasoning to find 
   the best match from the menu. Always pick from the correct slot's category unless the 
   user explicitly asks to break a rule.
3. RULE CHANGES: Detect when a user wants to change rules. Examples:
   - "change to 2 chicken dishes" → update slot structure
   - "no Indian this week" → override category for this week only
   - "from now on add a soup" → permanent rule change
   - "Sumegha is vegetarian today" → note it, adjust if needed
   When rules change, confirm what changed and re-suggest affected slots.
4. CONFIRM: When both users seem happy (e.g. "looks good", "perfect", "confirmed", 
   "let's go"), output a JSON block at the END of your message in this exact format:
   <<<CONFIRMED>>>
   {{"picks": ["dish1", "dish2", "dish3", "dish4"]}}
   <<<END>>>
5. INGREDIENT REASONING: If asked for a dish with specific ingredients (cucumber, 
   pomegranate, etc.), reason from culinary knowledge about which menu dishes likely 
   contain them. Be honest if you're inferring — say "likely contains" not "definitely has".

=== RESPONSE STYLE ===
- WhatsApp style: short, friendly, no markdown headers
- Use numbered lists for the 4 dishes
- Add a one-line reason for each pick (e.g. "— light and seasonal")
- For swaps, just show the new dish and say what it replaces
- Emoji are fine but don't overdo it
- Max ~200 words per message

=== RULE CHANGE RESPONSE FORMAT ===
When you detect a rule change request, respond with a JSON block at END of message:
<<<RULE_CHANGE>>>
{{"type": "this_week" or "permanent", "change_description": "...", "new_slots": [...] or null}}
<<<END>>>
"""

# ── Claude call ────────────────────────────────────────────────────────────────
def ask_claude(state, user_message, sender_name):
    history = state.get("conversation_history", [])

    history.append({
        "role": "user",
        "content": f"[{sender_name}]: {user_message}"
    })

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=build_system_prompt(state),
        messages=history
    )

    reply_text = response.content[0].text

    history.append({
        "role": "assistant",
        "content": reply_text
    })

    state["conversation_history"] = history[-30:]  # keep last 30 turns
    return reply_text

# ── Parse Claude response for special blocks ───────────────────────────────────
def parse_reply(reply, state):
    clean_reply = reply

    # Check for confirmation
    if "<<<CONFIRMED>>>" in reply:
        try:
            block = reply.split("<<<CONFIRMED>>>")[1].split("<<<END>>>")[0].strip()
            data = json.loads(block)
            picks = data.get("picks", [])
            if len(picks) == 4:
                state["current_picks"] = picks
                state["confirmed"] = True
                week_str = date.today().isoformat()
                state["selection_history"].append({
                    "week": week_str,
                    "picks": picks,
                    "feedback": []
                })
                # Reset for next week
                state["conversation_history"] = []
                state["week"] = week_str
        except Exception as e:
            print(f"Confirmation parse error: {e}")
        clean_reply = reply.split("<<<CONFIRMED>>>")[0].strip()

    # Check for rule change
    if "<<<RULE_CHANGE>>>" in reply:
        try:
            block = reply.split("<<<RULE_CHANGE>>>")[1].split("<<<END>>>")[0].strip()
            data = json.loads(block)
            if data.get("new_slots"):
                if data.get("type") == "permanent":
                    state["rules"]["slots"] = data["new_slots"]
                else:
                    state["rules"]["rule_overrides_this_week"] = data["new_slots"]
        except Exception as e:
            print(f"Rule change parse error: {e}")
        clean_reply = reply.split("<<<RULE_CHANGE>>>")[0].strip()

    return clean_reply, state

# ── Send to both numbers ───────────────────────────────────────────────────────
def send_whatsapp(message, to_number):
    twilio_client.messages.create(
        from_=TWILIO_NUMBER,
        body=message,
        to=to_number
    )

def broadcast(message):
    send_whatsapp(message, USER_NUMBER)
    send_whatsapp(message, SUMEGHA_NUMBER)

# ── Thursday trigger ───────────────────────────────────────────────────────────
@app.route("/trigger", methods=["GET", "POST"])
def thursday_trigger():
    state = load_state()

    # Reset picks for new week
    state["current_picks"] = [None, None, None, None]
    state["confirmed"] = False
    state["conversation_history"] = []
    state["week"] = date.today().isoformat()

    # Apply any week-level overrides back to main slots if needed
    if state["rules"].get("rule_overrides_this_week"):
        state["rules"]["slots"] = state["rules"]["rule_overrides_this_week"]
        state["rules"]["rule_overrides_this_week"] = []

    # Ask Claude for initial suggestions
    today = datetime.now().strftime("%A, %B %d")
    initial_message = f"It's Thursday {today}. Please suggest this week's 4 dishes following the current rules."

    reply = ask_claude(state, initial_message, "System")
    clean_reply, state = parse_reply(reply, state)

    save_state(state)
    broadcast(clean_reply)

    return "OK", 200

# ── Incoming WhatsApp message ──────────────────────────────────────────────────
@app.route("/whatsapp", methods=["POST"])
def whatsapp_incoming():
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    # Identify sender
    if from_number == USER_NUMBER:
        sender_name = "You"
    elif from_number == SUMEGHA_NUMBER:
        sender_name = "Sumegha"
    else:
        return str(MessagingResponse()), 200  # ignore unknown numbers

    state = load_state()

    # Get Claude's response
    reply = ask_claude(state, body, sender_name)
    clean_reply, state = parse_reply(reply, state)

    save_state(state)

    # Send reply to BOTH people (so both see the response)
    broadcast(clean_reply)

    # Return empty TwiML (we already sent manually)
    return str(MessagingResponse()), 200

# ── Health check ───────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
