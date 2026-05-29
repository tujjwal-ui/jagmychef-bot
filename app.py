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

# ── Redis (Upstash) ──────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("UPSTASH_REDIS_URL", "")
redis_client = None
if REDIS_URL:
    try:
        import redis
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        print("Redis connected OK")
    except Exception as e:
        print(f"Redis connection failed, using file fallback: {e}")
        redis_client = None

STATE_KEY = "jagmychef:state"

# ── Menu ─────────────────────────────────────────────────────────────────────
BASE_DIR  = os.path.dirname(__file__)
MENU_FILE = os.path.join(BASE_DIR, "data", "menu.json")
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
    "dish_scores": {},
    "rejected_this_week": []
}

# ── State helpers (Redis-backed, file fallback) ───────────────────────────────
def load_state():
    # Try Redis first
    if redis_client:
        try:
            raw = redis_client.get(STATE_KEY)
            if raw:
                s = json.loads(raw)
                for k, v in DEFAULT_STATE.items():
                    if k not in s:
                        s[k] = v
                return s
        except Exception as e:
            print(f"Redis read error: {e}")

    # File fallback
    fpath = os.path.join(BASE_DIR, "data", "state.json")
    if os.path.exists(fpath):
        with open(fpath) as f:
            s = json.load(f)
            for k, v in DEFAULT_STATE.items():
                if k not in s:
                    s[k] = v
            return s

    return DEFAULT_STATE.copy()

def save_state(state):
    # Save to Redis
    if redis_client:
        try:
            redis_client.set(STATE_KEY, json.dumps(state))
        except Exception as e:
            print(f"Redis write error: {e}")

    # Always write file backup too
    fpath = os.path.join(BASE_DIR, "data", "state.json")
    try:
        with open(fpath, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"File write error: {e}")

# ── Scoring helpers ───────────────────────────────────────────────────────────
def get_recent_items(state):
    no_repeat = state["rules"].get("no_repeat_weeks", 2)
    recent = []
    for entry in state["selection_history"][-no_repeat:]:
        recent.extend(entry.get("picks", []))
    return recent

def build_frequency_summary(state):
    freq = {}
    for entry in state.get("selection_history", []):
        for pick in entry.get("picks", []):
            freq[pick] = freq.get(pick, 0) + 1

    scores   = state.get("dish_scores", {})
    rejected = state.get("rejected_this_week", [])

    lines = []
    all_dishes = set(list(freq.keys()) + list(scores.keys()))
    for dish in all_dishes:
        count    = freq.get(dish, 0)
        likes    = scores.get(dish, {}).get("likes", 0)
        dislikes = scores.get(dish, {}).get("dislikes", 0)
        net      = likes - dislikes
        rej_flag = " ⚠️ swapped out recently" if dish in rejected else ""
        tag = ""
        if net >= 2:  tag = " ⭐ highly rated"
        elif net == 1: tag = " 👍 liked"
        elif net <= -1: tag = " 👎 disliked — avoid unless requested"
        lines.append(f"  {dish} — ordered {count}x, net score {net:+d}{tag}{rej_flag}")

    lines.sort(key=lambda x: ("avoid" not in x, "highly" in x, "liked" in x), reverse=True)
    return "\n".join(lines[:30]) if lines else "No history yet."

# ── System prompt ─────────────────────────────────────────────────────────────
def build_system_prompt(state):
    recent       = get_recent_items(state)
    rules        = state["rules"]
    slots_desc   = "\n".join(
        f"  Slot {i+1}: {s['label']} — category '{s['category']}'"
        + (f", filter '{s['filter']}'" if s["filter"] != "any" else "")
        for i, s in enumerate(rules["slots"])
    )
    picks_desc   = "\n".join(
        f"  Slot {i+1} ({rules['slots'][i]['label']}): {p or 'EMPTY'}"
        for i, p in enumerate(state["current_picks"])
    )
    recent_desc  = "\n".join(f"  - {r}" for r in recent) if recent else "  None"
    freq_summary = build_frequency_summary(state)
    rejected_desc = "\n".join(f"  - {r}" for r in state.get("rejected_this_week", [])) or "  None"

    return f"""You are the Chef Jag meal selection assistant for a WhatsApp group chat.
Every Thursday you help Tushar and Sumegha pick 4 dishes for Chef Jag to cook on Monday.
This is a casual WhatsApp conversation — be warm, concise, friendly.

=== FULL MENU ===
{json.dumps(MENU["repository"], indent=2)}

=== CURRENT SELECTION RULES ===
{slots_desc}
No-repeat window: {rules['no_repeat_weeks']} weeks

=== CURRENT PICKS THIS WEEK ===
{picks_desc}

=== DO NOT REPEAT (last {rules['no_repeat_weeks']} weeks) ===
{recent_desc}

=== DISH SCORES & HISTORY ===
{freq_summary}

=== DISHES SWAPPED OUT THIS THURSDAY ===
{rejected_desc}

=== RECOMMENDATION ALGORITHM ===
Rank candidates using these signals in order:
1. HARD BLOCK: never suggest dishes in the no-repeat list
2. HARD BLOCK: never suggest dishes with net score <= -2 unless explicitly requested
3. PREFER: net score >= 2 (highly rated) — suggest these first
4. PREFER: ordered 2-3x (proven favourites) over untried dishes
5. NOVELTY: flag untried dishes with "haven't tried this yet!"
6. AVOID: dishes in rejected_this_week — treat as soft dislike for 2 weeks
7. BALANCE: vary flavour profiles within slots

=== CAPABILITIES ===

1. INITIAL 8 SUGGESTIONS (Thursday trigger):
   Present 8 options — 2 per slot — labeled A-H:

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

2. LETTER SELECTION: Map letters to dishes, confirm clearly.
   Ask: "Happy with these? Reply *confirm* to lock them in!"

3. SMART SWAP: Match natural requests to menu using culinary reasoning.
   When a dish is swapped out, emit at end of reply:
   <<<REJECTED>>>
   {{"dish": "name of dish being replaced"}}
   <<<END>>>

4. ECHO SENDER: Start every reply (except Thursday opener) with *[Tushar]:* or *[Sumegha]:*

5. RULE CHANGES: Detect and confirm rule changes. Emit:
   <<<RULE_CHANGE>>>
   {{"type": "this_week" or "permanent", "description": "...", "new_slots": [...] or null}}
   <<<END>>>

6. CONFIRMATION: On "confirm" / "looks good" / "lock it in", emit:
   <<<CONFIRMED>>>
   {{"picks": ["dish1", "dish2", "dish3", "dish4"]}}
   <<<END>>>

7. FEEDBACK MODE (Tuesday): Ask them to rate each dish 👍 or 👎, numbered.
   When ratings arrive, parse and emit:
   <<<FEEDBACK>>>
   {{"ratings": {{"dish_name": "like" or "dislike", ...}}}}
   <<<END>>>
   Then thank them and mention what you learned for next week.

=== STYLE ===
- WhatsApp style, *bold* for emphasis
- Initial: A-H pair format
- Swaps: new dish + what it replaces
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
    clean = reply

    if "<<<CONFIRMED>>>" in reply:
        try:
            block = reply.split("<<<CONFIRMED>>>")[1].split("<<<END>>>")[0].strip()
            picks = json.loads(block).get("picks", [])
            if len(picks) == 4:
                state["current_picks"]  = picks
                state["confirmed"]      = True
                state["selection_history"].append({
                    "week":     date.today().isoformat(),
                    "picks":    picks,
                    "rejected": list(state.get("rejected_this_week", [])),
                    "feedback": {}
                })
                state["conversation_history"] = []
                state["week"]               = date.today().isoformat()
                state["rejected_this_week"] = []
                state["feedback_mode"]      = False
        except Exception as e:
            print(f"Confirmed parse error: {e}")
        clean = reply.split("<<<CONFIRMED>>>")[0].strip()

    if "<<<REJECTED>>>" in reply:
        try:
            block = reply.split("<<<REJECTED>>>")[1].split("<<<END>>>")[0].strip()
            dish  = json.loads(block).get("dish", "")
            if dish and dish not in state.get("rejected_this_week", []):
                state.setdefault("rejected_this_week", []).append(dish)
        except Exception as e:
            print(f"Rejected parse error: {e}")
        # strip the block from clean
        if "<<<REJECTED>>>" in clean:
            start = clean.find("<<<REJECTED>>>")
            end   = clean.find("<<<END>>>", start) + 9
            clean = (clean[:start] + clean[end:]).strip()

    if "<<<FEEDBACK>>>" in reply:
        try:
            block   = reply.split("<<<FEEDBACK>>>")[1].split("<<<END>>>")[0].strip()
            ratings = json.loads(block).get("ratings", {})
            scores  = state.setdefault("dish_scores", {})
            for dish, rating in ratings.items():
                entry = scores.setdefault(dish, {"likes": 0, "dislikes": 0})
                if rating == "like":    entry["likes"]    += 1
                elif rating == "dislike": entry["dislikes"] += 1
            if state["selection_history"]:
                state["selection_history"][-1]["feedback"] = ratings
            state["feedback_mode"] = False
        except Exception as e:
            print(f"Feedback parse error: {e}")
        clean = reply.split("<<<FEEDBACK>>>")[0].strip()

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
        clean = reply.split("<<<RULE_CHANGE>>>")[0].strip()

    return clean, state

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
    clean, state = parse_reply(reply, state)
    save_state(state)
    broadcast(clean)
    return "OK", 200

# ── Tuesday feedback trigger ─────────────────────────────────────────────────
@app.route("/feedback", methods=["GET", "POST"])
def feedback_trigger():
    state = load_state()

    if not state.get("confirmed") or not any(state.get("current_picks", [])):
        return "No confirmed picks to rate", 200

    picks = [p for p in state["current_picks"] if p]
    state["feedback_mode"]        = True
    state["conversation_history"] = []

    prompt = (
        f"It's Tuesday evening. Ask Tushar and Sumegha to rate the 4 dishes "
        f"Chef Jag cooked on Monday. The dishes were: {picks}. "
        f"Ask them to reply with 👍 or 👎 for each, numbered clearly."
    )
    reply = ask_claude(state, prompt, "System")
    clean, state = parse_reply(reply, state)
    save_state(state)
    broadcast(clean)
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
    clean, state = parse_reply(reply, state)
    save_state(state)
    broadcast(clean)
    return str(MessagingResponse()), 200

# ── Seed history ─────────────────────────────────────────────────────────────
@app.route("/seed_history", methods=["POST"])
def seed_history():
    data  = request.get_json()
    state = load_state()
    existing = {e["week"] for e in state["selection_history"]}
    added = 0
    for entry in data.get("history", []):
        if entry["week"] not in existing:
            state["selection_history"].append({
                "week":     entry["week"],
                "picks":    entry["picks"],
                "rejected": [],
                "feedback": {}
            })
            added += 1
    state["selection_history"].sort(key=lambda x: x["week"])
    save_state(state)
    return {"seeded": added, "total": len(state["selection_history"])}, 200

# ── Health check ─────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    state   = load_state()
    storage = "redis" if redis_client else "file (ephemeral — set UPSTASH_REDIS_URL)"
    return {
        "status":        "ok",
        "storage":       storage,
        "date":          date.today().isoformat(),
        "history_weeks": len(state.get("selection_history", [])),
        "confirmed":     state.get("confirmed", False),
        "feedback_mode": state.get("feedback_mode", False),
        "dish_scores":   len(state.get("dish_scores", {}))
    }, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
