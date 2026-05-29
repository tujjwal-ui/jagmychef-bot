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

TWILIO_NUMBER  = os.environ["TWILIO_WHATSAPP_NUMBER"]
USER_NUMBER    = os.environ["USER_WHATSAPP_NUMBER"]
SUMEGHA_NUMBER = os.environ["SUMEGHA_WHATSAPP_NUMBER"]

# ── Menu ─────────────────────────────────────────────────────────────────────
BASE_DIR  = os.path.dirname(__file__)
MENU_FILE = os.path.join(BASE_DIR, "data", "menu.json")
STATE_FILE = os.path.join(BASE_DIR, "data", "state.json")
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

with open(MENU_FILE) as f:
    MENU = json.load(f)

# ── Default state ─────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "rules": {
        "slots": [
            {"label": "Chicken dish",  "category": "Indian", "filter": "chicken"},
            {"label": "Salad",         "category": "Salads", "filter": "any"},
            {"label": "Veg dish",      "category": "Indian", "filter": "vegetarian"},
            {"label": "Veg dish",      "category": "Indian", "filter": "vegetarian"},
        ],
        "no_repeat_weeks": 2,
        "rule_overrides_this_week": []
    },
    "current_picks": [None, None, None, None],
    "conversation_history": [],
    "selection_history": [],
    "confirmed": False,
    "week": None,
    "feedback_mode": False,
    "dish_scores": {},       # dish -> {"likes": N, "dislikes": N}
    "rejected_this_week": [] # dishes swapped out during Thursday chat
}

# ── State helpers ─────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
            # backfill new keys
            for k, v in DEFAULT_STATE.items():
                if k not in s:
                    s[k] = v
            return s
    return DEFAULT_STATE.copy()

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Scoring helpers ───────────────────────────────────────────────────────────
def get_recent_items(state):
    no_repeat = state["rules"].get("no_repeat_weeks", 2)
    recent = []
    for entry in state["selection_history"][-no_repeat:]:
        recent.extend(entry.get("picks", []))
    return recent

def build_frequency_summary(state):
    """Frequency + like/dislike scores combined into a ranked list."""
    freq = {}
    for entry in state.get("selection_history", []):
        for pick in entry.get("picks", []):
            freq[pick] = freq.get(pick, 0) + 1

    scores = state.get("dish_scores", {})
    rejected = state.get("rejected_this_week", [])

    lines = []
    all_dishes = set(list(freq.keys()) + list(scores.keys()))
    for dish in all_dishes:
        count = freq.get(dish, 0)
        likes = scores.get(dish, {}).get("likes", 0)
        dislikes = scores.get(dish, {}).get("dislikes", 0)
        rej = "⚠️ swapped out recently" if dish in rejected else ""
        net = likes - dislikes
        tag = ""
        if net >= 2: tag = "⭐ highly rated"
        elif net == 1: tag = "👍 liked"
        elif net <= -1: tag = "👎 disliked — avoid unless requested"
        lines.append(f"  {dish} — ordered {count}x, net score {net:+d} {tag} {rej}".strip())

    lines.sort(key=lambda x: ("avoid" not in x, "highly" in x, "liked" in x), reverse=True)
    return "\n".join(lines[:30]) if lines else "No history yet."

# ── System prompt ─────────────────────────────────────────────────────────────
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

    recent_desc     = "\n".join(f"  - {r}" for r in recent) if recent else "  None"
    freq_summary    = build_frequency_summary(state)
    rejected_desc   = "\n".join(f"  - {r}" for r in state.get("rejected_this_week", [])) or "  None"

    return f"""You are the Chef Jag meal selection assistant for a WhatsApp group chat.
Every Thursday you help Tushar and Sumegha pick 4 dishes for Chef Jag to cook on Monday.
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

=== DISH SCORES & HISTORY (use to rank suggestions) ===
{freq_summary}

=== DISHES SWAPPED OUT THIS THURSDAY (soft dislike signal) ===
{rejected_desc}

=== RECOMMENDATION ALGORITHM ===
When generating suggestions, rank candidates using these weighted signals:

1. HARD BLOCK: never suggest dishes in the no-repeat list
2. HARD BLOCK: never suggest dishes with net score <= -2 unless explicitly requested
3. PREFER: dishes with net score >= 2 (highly rated) — suggest these first unless recently repeated
4. PREFER: dishes ordered 2-3x (proven favourites) over untried dishes
5. NOVELTY: if all favourite slots are blocked by no-repeat, flag new dishes with "haven't tried this yet!"
6. AVOID: dishes swapped out this Thursday (rejected_this_week) — treat as soft dislike for 2 weeks
7. BALANCE: within slot constraints, aim for variety (avoid clustering similar flavour profiles)

=== YOUR CAPABILITIES ===

1. INITIAL 8 SUGGESTIONS (Thursday trigger):
   Present 8 options — 2 per slot — labeled A through H:

   🍗 *Chicken* (pick 1):
   A. [dish] — [one-line reason]
   B. [dish] — [one-line reason]

   🥗 *Salad* (pick 1):
   C. [dish] — [one-line reason]
   D. [dish] — [one-line reason]

   🌿 *Veg dish 1* (pick 1):
   E. [dish] — [one-line reason]
   F. [dish] — [one-line reason]

   🌿 *Veg dish 2* (pick 1):
   G. [dish] — [one-line reason]
   H. [dish] — [one-line reason]

   End with: "Reply with 4 letters (e.g. A, C, E, G) or ask for swaps!"

2. LETTER SELECTION: When they reply with 4 letters (e.g. "A C F H"):
   Map letters to dishes and confirm the 4 picks clearly.
   Ask: "Happy with these? Reply *confirm* to lock them in!"

3. SMART SWAP: For natural requests ("I want a kebab", "something lighter",
   "salad with cucumber") — use culinary reasoning to find the best menu match.
   When a dish is swapped out, note it internally as a rejection signal.
   Emit at end of swap response:
   <<<REJECTED>>>
   {{"dish": "name of dish being replaced"}}
   <<<END>>>

4. ECHO SENDER: Start EVERY response (except the initial Thursday message) with
   *[Tushar]:* or *[Sumegha]:* — whoever sent the last message.

5. RULE CHANGES: Detect rule change requests and confirm what changed.
   Emit at end of message:
   <<<RULE_CHANGE>>>
   {{"type": "this_week" or "permanent", "description": "...", "new_slots": [...] or null}}
   <<<END>>>

6. CONFIRMATION: When they say "confirm", "looks good", "perfect", "lock it in":
   Show a clean summary of the final 4, then emit:
   <<<CONFIRMED>>>
   {{"picks": ["dish1", "dish2", "dish3", "dish4"]}}
   <<<END>>>

7. FEEDBACK MODE (Tuesday evening):
   When you receive a feedback prompt, ask them to rate each dish with 👍 or 👎.
   Format your ask clearly showing each dish numbered.
   When they reply with ratings, parse them and emit:
   <<<FEEDBACK>>>
   {{"ratings": {{"dish_name": "like" or "dislike", ...}}}}
   <<<END>>>
   Then thank them warmly and mention what you learned.

=== STYLE ===
- WhatsApp: short, warm, use *bold* for WhatsApp formatting
- Initial suggestions: A-H pair format
- Swaps: just show new dish + what it replaces
- Max ~300 words initial, ~120 for replies
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

    # Confirmation
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
                    "week": week_str, "picks": picks,
                    "rejected": list(state.get("rejected_this_week", [])),
                    "feedback": {}
                })
                state["conversation_history"] = []
                state["week"] = week_str
                state["rejected_this_week"] = []
                state["feedback_mode"] = False
        except Exception as e:
            print(f"Confirmation parse error: {e}")
        clean_reply = reply.split("<<<CONFIRMED>>>")[0].strip()

    # Rejection tracking
    if "<<<REJECTED>>>" in reply:
        try:
            block = reply.split("<<<REJECTED>>>")[1].split("<<<END>>>")[0].strip()
            data  = json.loads(block)
            dish  = data.get("dish", "")
            if dish and dish not in state.get("rejected_this_week", []):
                state.setdefault("rejected_this_week", []).append(dish)
        except Exception as e:
            print(f"Rejection parse error: {e}")
        clean_reply = clean_reply.replace(
            reply[reply.find("<<<REJECTED>>>"):reply.find("<<<END>>>", reply.find("<<<REJECTED>>>"))+9], ""
        ).strip()

    # Feedback
    if "<<<FEEDBACK>>>" in reply:
        try:
            block   = reply.split("<<<FEEDBACK>>>")[1].split("<<<END>>>")[0].strip()
            data    = json.loads(block)
            ratings = data.get("ratings", {})
            scores  = state.setdefault("dish_scores", {})
            for dish, rating in ratings.items():
                entry = scores.setdefault(dish, {"likes": 0, "dislikes": 0})
                if rating == "like":
                    entry["likes"] += 1
                elif rating == "dislike":
                    entry["dislikes"] += 1
            # also store on most recent history entry
            if state["selection_history"]:
                state["selection_history"][-1]["feedback"] = ratings
            state["feedback_mode"] = False
        except Exception as e:
            print(f"Feedback parse error: {e}")
        clean_reply = reply.split("<<<FEEDBACK>>>")[0].strip()

    # Rule change
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

# ── Twilio helpers ───────────────────────────────────────────────────────────
def send_whatsapp(message, to_number):
    twilio_client.messages.create(from_=TWILIO_NUMBER, body=message, to=to_number)

def broadcast(message):
    send_whatsapp(message, USER_NUMBER)
    send_whatsapp(message, SUMEGHA_NUMBER)

# ── Thursday trigger ─────────────────────────────────────────────────────────
@app.route("/trigger", methods=["GET", "POST"])
def thursday_trigger():
    state = load_state()
    state["current_picks"]        = [None, None, None, None]
    state["confirmed"]            = False
    state["conversation_history"] = []
    state["week"]                 = date.today().isoformat()
    state["feedback_mode"]        = False
    state["rejected_this_week"]   = []

    if state["rules"].get("rule_overrides_this_week"):
        state["rules"]["slots"] = state["rules"]["rule_overrides_this_week"]
        state["rules"]["rule_overrides_this_week"] = []

    today  = datetime.now().strftime("%A, %B %d")
    prompt = (
        f"It's Thursday {today}. Generate the initial 8 suggestions "
        f"(2 per slot, A-H format) using the scoring algorithm."
    )

    reply = ask_claude(state, prompt, "System")
    clean_reply, state = parse_reply(reply, state)
    save_state(state)
    broadcast(clean_reply)
    return "OK", 200

# ── Tuesday feedback trigger ─────────────────────────────────────────────────
@app.route("/feedback", methods=["GET", "POST"])
def feedback_trigger():
    state = load_state()

    if not state.get("confirmed") or not state.get("current_picks"):
        return "No confirmed picks to rate", 200

    picks = state["current_picks"]
    state["feedback_mode"] = True
    state["conversation_history"] = []  # fresh context for feedback

    prompt = (
        f"It's Tuesday evening. Ask Tushar and Sumegha to rate the 4 dishes "
        f"Chef Jag cooked on Monday. The dishes were: {picks}. "
        f"Ask them to reply with thumbs up or down for each dish, numbered clearly."
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
        sender_name = "Tushar"
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

# ── Seed history ─────────────────────────────────────────────────────────────
@app.route("/seed_history", methods=["POST"])
def seed_history():
    data  = request.get_json()
    state = load_state()
    existing_weeks = {e["week"] for e in state["selection_history"]}
    added = 0
    for entry in data.get("history", []):
        if entry["week"] not in existing_weeks:
            state["selection_history"].append({
                "week": entry["week"], "picks": entry["picks"],
                "rejected": [], "feedback": {}
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
        "status":   "ok",
        "date":     date.today().isoformat(),
        "history":  len(state.get("selection_history", [])),
        "confirmed": state.get("confirmed", False),
        "feedback_mode": state.get("feedback_mode", False)
    }, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
