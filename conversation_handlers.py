"""Multi-turn handling: respond(state, merchant_message) -> {"action": send|wait|end, ...}.

Rule-first: a deterministic classifier decides the *move* (exit, back off, act, redirect,
answer); the LLM (if configured) only writes the wording, and is validated like any send.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from vera.composer import MERCHANT_ONLY
from vera.facts import build_facts, humanize
from vera.llm import get_llm, parse_json
from vera.playbooks import CONFIRM, NONE, OPEN, SLOTS, YES_STOP
from vera.templates import _peer_line, _weak_spot, build_artifact
from vera.validator import validate

# ---------------------------------------------------------------- patterns

AUTO_REPLY_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)", r"(our|the) team will (respond|get back|contact|reply)",
    r"we will (get back|respond|reply|contact)", r"will (respond|reply|get back) (shortly|soon|within)",
    r"automated (assistant|message|reply|response)", r"auto[- ]?reply", r"currently (unavailable|away|closed|busy)",
    r"out of (the )?office", r"business hours", r"this is an automated", r"i am an (ai|automated)",
    r"jaankari ke liye .*shukriya", r"team tak pahuncha", r"aapki madad ke liye shukriya",
    r"hum jald (hi )?(sampark|contact)", r"please leave (a|your) message",
]
OPT_OUT_PATTERNS = [
    r"\bstop\b", r"unsubscribe", r"not interested", r"no interest", r"don'?t (message|text|contact|send)",
    r"do not (message|text|contact|send)", r"stop (messaging|texting|sending)", r"leave me alone", r"remove me",
    r"mat (bhejo|bhejiye|karo message)", r"band karo", r"nahi chahiye", r"interest nahi", r"block (kar|you)",
]
HOSTILE_PATTERNS = [
    r"\bspam\b", r"useless", r"bothering", r"harass", r"\bidiot", r"\bstupid", r"\bfool\b", r"nonsense", r"bakwas",
    r"\bfuck", r"\bshit", r"\bbloody\b", r"\bscam\b", r"fraud", r"pagal", r"bekar", r"irritat", r"waste of (my )?time",
]
COMMIT_PATTERNS = [
    r"\blet'?s do (it|this)\b", r"\bgo ahead\b", r"\bdo it\b", r"\bproceed\b", r"\bconfirm(ed)?\b", r"\bsign me up\b",
    r"\bi want to join\b", r"\bjoin\b", r"\bjudna\b", r"\bjudrna\b", r"\bstart (it|now)\b", r"\bplease (do|send|draft|go)\b",
    r"^\s*(yes|yes please|yes pls|yep|yeah|sure|ok|okay|ok ?done|done|haan|haan ji|ha|han|ji|theek hai|thik hai|chalo|karo|kar do|kardo)\b",
    r"\bsend (it|me|the)\b", r"\bdraft (it|the|a)\b", r"\bbook (it|me)\b", r"\bsounds good\b", r"\bperfect\b",
    r"\bkar (do|dijiye|dena)\b", r"\bbhej (do|dijiye)\b",
]
LATER_PATTERNS = [r"\blater\b", r"\bnot now\b", r"\bbusy\b", r"baad (mein|me)", r"\bkal\b", r"\btomorrow\b",
                  r"call (you|me) (back|later)", r"in a meeting", r"\bthoda (time|ruk)"]
NO_PATTERNS = [r"^\s*(no|nope|nahi|nahin|na|no thanks|no thank you|not needed|not required)\s*[.!]*\s*$"]
THANKS_PATTERNS = [r"^\s*(ok(ay)?[\s,]*)?(thanks|thank you|thanku|thx|ty|shukriya|dhanyava?a?d)(\s+(so much|a lot|ji))?[\s.!🙏👍]*$"]
APPROVAL_PATTERNS = [r"\bconfirm(ed)?\b", r"\bapproved?\b", r"\blooks good\b", r"\bfinal\b", r"\bperfect\b"]
OFF_TOPIC_PATTERNS = [
    r"\bgst\b", r"income tax", r"\bitr\b", r"\btax (filing|return)", r"\bloan\b", r"insurance", r"\bvisa\b",
    r"electricity bill", r"\brecharge\b", r"\bpassport\b", r"\blawyer\b", r"legal notice", r"\bcricket score\b",
    r"\bstock market\b", r"\bshare price\b", r"\bcrypto\b", r"\baccounting\b", r"\bpayroll\b",
]
HINDI_MARKERS = r"\b(hai|hain|nahi|nahin|karo|kya|aap|aapka|mujhe|haan|kaise|chahiye|hoon|mein|kar|bhej|theek|accha|acha|ji|kab|kyun)\b"
QUALIFYING_PHRASES = ["would you", "do you", "can you tell", "what if", "how about", "are you", "could you tell"]


def _match(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", "", re.sub(r"\s+", " ", (text or "").lower())).strip()


def detect_language(text: str, default: str) -> str:
    """Per-turn language: follow the merchant if they clearly switch."""
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hindi"
    hits = len(re.findall(HINDI_MARKERS, (text or "").lower()))
    if hits >= 2 or (hits == 1 and default != "english"):
        return "hinglish"
    if hits == 0 and len((text or "").split()) >= 4:
        return "english"  # a full English sentence -> reply in English
    return default


# ---------------------------------------------------------------- state


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    kind: Optional[str] = None
    category: dict = field(default_factory=dict)
    merchant: dict = field(default_factory=dict)
    trigger: dict = field(default_factory=dict)
    customer: Optional[dict] = None
    turns: list = field(default_factory=list)          # [{"from": "vera"|"merchant"|"customer", "body": str}]
    mode: str = "pitch"                                 # pitch | action | closed
    auto_reply_count: int = 0
    hostile_count: int = 0
    off_topic_count: int = 0
    last_msg_repeats: int = 1
    unanswered_nudges: int = 0
    language: str = "english"
    ended: bool = False
    last_activity: float = field(default_factory=time.time)
    last_cta: Optional[str] = None                       # the CTA shape of the last message WE sent

    @property
    def sent_bodies(self) -> list[str]:
        return [t["body"] for t in self.turns if t["from"] == "vera"]

    @property
    def last_bot_body(self) -> str:
        return next((t["body"] for t in reversed(self.turns) if t["from"] == "vera"), "")


# merchant-level memory, shared across conversations (auto-replies often span conversations)
_merchant_msgs: dict[str, dict[str, int]] = {}
_opted_out: dict[str, float] = {}
_customer_opted_out: dict[tuple[str, str], float] = {}   # (merchant_id, customer_id) -> suppressed-until
_lock = threading.Lock()


def merchant_opted_out(merchant_id: Optional[str]) -> bool:
    return bool(merchant_id) and _opted_out.get(merchant_id, 0) > time.time()


def customer_opted_out(merchant_id: Optional[str], customer_id: Optional[str]) -> bool:
    if not merchant_id or not customer_id:
        return False
    return _customer_opted_out.get((merchant_id, customer_id), 0) > time.time()


def reset_memory() -> None:
    with _lock:
        _merchant_msgs.clear()
        _opted_out.clear()
        _customer_opted_out.clear()


# ---------------------------------------------------------------- the move


def _offer_from_last(body: str) -> Optional[str]:
    m = re.search(r"(?:want me to|shall i|should i|can i|let me)\s+(.+?)[?.]", body or "", re.I)
    return m.group(1).strip() if m else None


KIND_DELIVERABLE = {
    "research_digest": "the 1-page abstract summary + a patient-friendly WhatsApp draft",
    "regulation_change": "a 5-point compliance checklist for your setup",
    "cde_opportunity": "the registration details + a calendar reminder",
    "supply_alert": "the affected-customer WhatsApp note + replacement-pickup steps",
    "perf_dip": "a fresh Google post with your offer",
    "perf_spike": "a follow-up post in the same format",
    "renewal_due": "your renewal link",
    "winback_eligible": "your plan restart",
    "gbp_unverified": "the verification steps",
    "review_theme_emerged": "a polite public review reply + a fix announcement",
    "festival_upcoming": "the festive Google post + WhatsApp broadcast",
    "curious_ask_due": "the Google post + a ready price-reply",
    "active_planning_intent": "the final version + Google post + WhatsApp announcement",
    "seasonal_perf_dip": "a 4-week member attendance challenge",
    "milestone_reached": "a review-request message for recent happy customers",
    "competitor_opened": "a Google post highlighting what customers love about you",
    "ipl_match_today": "a delivery banner + Insta story",
    "category_seasonal": "a seasonal Google post",
    "dormant_with_vera": "the 2-step fix",
}


def _classify(state: ConversationState, msg: str) -> str:
    low = (msg or "").lower().strip()
    n = norm(msg)
    # repetition memory (conversation + merchant level)
    prev_merchant = [norm(t["body"]) for t in state.turns if t["from"] != "vera"]
    with _lock:
        mm = _merchant_msgs.setdefault(state.merchant_id or state.conversation_id, {})
        mm[n] = mm.get(n, 0) + 1
        merchant_repeats = mm[n]
    state.last_msg_repeats = max(merchant_repeats, prev_merchant.count(n) + 1)
    repeated = state.last_msg_repeats >= 2
    if _match(AUTO_REPLY_PATTERNS, low) or (repeated and len(n) > 25):
        return "auto_reply"
    # a qualified stop ("stop THIS one, but keep X") is not a full opt-out — treat as a real request,
    # never silently apply a blanket 30-day suppression the merchant/customer didn't ask for.
    qualified_stop = _match(OPT_OUT_PATTERNS, low) and re.search(r"\b(but|except|keep|only|just)\b", low)
    if _match(OPT_OUT_PATTERNS, low) and not qualified_stop:
        return "opt_out"
    if _match(HOSTILE_PATTERNS, low):
        return "hostile"
    if _match(OFF_TOPIC_PATTERNS, low):
        return "off_topic"
    if _match(NO_PATTERNS, low):
        return "no"
    if _match(THANKS_PATTERNS, low):
        return "thanks"
    # "ok and what else?" / "ok but what's the price?" are questions, not a yes — only an explicit go-ahead
    # ("ok let's do it, what's next?") inside a question still counts as commitment
    question = "?" in low and re.search(r"\b(what|whats|what's|which|how|why|when|where|who|kya|kaise|kab|kitna|kaun)\b", low)
    explicit_go = re.search(r"let'?s do|go ahead|\bdo it\b|proceed|\bstart\b|what'?s next|whats next|\bconfirm", low)
    if question and not explicit_go:
        return "engaged"
    if _match(COMMIT_PATTERNS, low):
        return "commit"
    # a bare "1" / "2" is only unambiguous as a slot pick when we actually offered numbered slots last
    if (state.last_cta == SLOTS or (state.customer_id and len(_slot_labels(state)) >= 2)) and re.match(r"^\s*[12]\s*$", low):
        return "commit"
    if _match(LATER_PATTERNS, low):
        return "later"
    return "engaged"


def respond(state: ConversationState, merchant_message: str) -> dict:
    """Decide and phrase the next move. Mutates `state`."""
    state.last_activity = time.time()
    label = _classify(state, merchant_message)
    state.turns.append({"from": "customer" if state.customer_id else "merchant", "body": merchant_message})
    state.language = detect_language(merchant_message, state.language)
    low = (merchant_message or "").lower()
    if re.search(r"\b(in|to|into)\s+hindi\b|\bhindi\s+(mein|me)\b", low):
        state.language = "hinglish"
    elif re.search(r"\b(in|to|into)\s+english\b", low):
        state.language = "english"

    if state.ended and label not in ("opt_out", "hostile", "auto_reply"):
        state.ended, state.mode = False, "pitch"  # merchant re-opened the conversation

    if label == "opt_out":
        state.ended, state.mode = True, "closed"
        if state.customer_id:
            # a customer's opt-out only suppresses that (merchant, customer) pair, never the whole merchant
            if state.merchant_id:
                _customer_opted_out[(state.merchant_id, state.customer_id)] = time.time() + 30 * 86400
            return {"action": "end", "rationale": "Customer explicitly opted out; closing politely and suppressing "
                                                  "future messages to this customer for 30 days. The merchant is unaffected."}
        if state.merchant_id:
            _opted_out[state.merchant_id] = time.time() + 30 * 86400
        return {"action": "end", "rationale": "Merchant explicitly opted out; closing politely and suppressing "
                                              "this merchant for 30 days. No further messages."}

    if label == "auto_reply":
        state.auto_reply_count += 1
        count = max(state.auto_reply_count, state.last_msg_repeats)
        if count >= 3:
            state.ended, state.mode = True, "closed"
            return {"action": "end", "rationale": f"Same canned auto-reply {count}x — owner is not reading; "
                                                  "closing instead of burning more turns."}
        if count == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Auto-reply again — owner not at the phone. Backing off 24h before one retry."}
        body = _say(state, "auto_reply", merchant_message)
        return _send(state, body, YES_STOP, "Detected WhatsApp Business auto-reply (canned phrasing). One short "
                                            "nudge addressed to the owner, then back off if it repeats.")

    if label == "hostile":
        state.hostile_count += 1
        if state.hostile_count >= 2:
            state.ended, state.mode = True, "closed"
            if state.customer_id and state.merchant_id:
                _customer_opted_out[(state.merchant_id, state.customer_id)] = time.time() + 30 * 86400
            elif state.merchant_id:
                _opted_out[state.merchant_id] = time.time() + 30 * 86400
            return {"action": "end", "rationale": "Repeated frustration; exiting gracefully and suppressing for 30 days."}
        body = _say(state, "hostile", merchant_message)
        return _send(state, body, NONE, "Merchant frustrated: brief apology, no pitch, clear opt-out path. "
                                        "Will end the conversation if frustration continues.")

    if label == "off_topic":
        state.off_topic_count += 1
        body = _say(state, "off_topic", merchant_message)
        return _send(state, body, OPEN, "Out-of-scope request politely declined in one line; redirected to the "
                                        "original topic without losing the thread.")

    if label == "no":
        state.ended, state.mode = True, "closed"
        return {"action": "end", "rationale": "Merchant declined; respecting it and closing without another push."}

    if label == "later":
        wait = 86400 if re.search(r"tomorrow|\bkal\b", merchant_message.lower()) else 14400
        return {"action": "wait", "wait_seconds": wait,
                "rationale": f"Merchant asked for time; backing off {wait // 3600}h instead of pushing."}

    if label == "thanks":
        state.ended, state.mode = True, "closed"
        return {"action": "end", "rationale": "Merchant/customer said thanks — nothing left to ask for; closing politely "
                                              "instead of re-pitching."}

    if label == "commit" and state.mode == "action" and state.customer_id:
        # the customer's booking/reminder was already confirmed last turn — nothing more to ask
        state.ended, state.mode = True, "closed"
        return {"action": "end", "rationale": "Customer re-confirmed an already-confirmed booking; closing instead of "
                                              "repeating the confirmation."}

    if label == "commit" and state.mode == "action" and _match(APPROVAL_PATTERNS, low) \
            and "confirm" in state.last_bot_body.lower():
        # the draft was already delivered and they approved it: close the loop, don't resend the same draft
        state.ended, state.mode = True, "closed"
        body = _h(state, "Approved ✅ — that's the final version. I won't send or publish anything else without your "
                         "go-ahead; reply here anytime if you want a tweak.",
                  "Approved ✅ — yahi final version hai. Aapki permission ke bina kuch aur send ya publish nahi karungi; "
                  "koi badlav chahiye to yahin reply kar dijiye.")
        return _send(state, body, NONE, "Merchant approved the delivered draft — confirmed and closed the loop "
                                        "instead of repeating the draft or asking another question.")

    if label == "commit":
        state.mode = "action"
        body = _say(state, "commit", merchant_message)
        return _send(state, body, CONFIRM, "Merchant committed — switched from pitch to action mode immediately: "
                                           "confirmed the work, delivered the concrete next artifact, single confirm CTA. "
                                           "No further qualifying questions.")

    body = _say(state, "engaged", merchant_message)
    if state.customer_id:
        cta = SLOTS if len(_slot_labels(state)) >= 2 else YES_STOP
        return _send(state, body, cta, "Customer asked a question: answered only from what's on file (no guessed hours, "
                                       "prices or medical claims, no business internals), then restated the booking step.")
    return _send(state, body, OPEN if state.mode == "pitch" else CONFIRM,
                 "Merchant engaged with a question/comment: answered from the facts on file and advanced one step.")


def _send(state: ConversationState, body: str, cta: str, rationale: str) -> dict:
    # anti-repetition: never send the same text twice in a conversation
    if norm(body) in {norm(b) for b in state.sent_bodies}:
        body = body.rstrip(".!") + " — just say the word and it's done."
    state.turns.append({"from": "vera", "body": body})
    state.last_cta = cta
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


# ---------------------------------------------------------------- wording


def _h(state: ConversationState, en: str, hing: str) -> str:
    return hing if state.language in ("hinglish", "hindi") else en


def _names(state: ConversationState) -> tuple[str, str]:
    ident = (state.merchant or {}).get("identity", {}) or {}
    owner = (ident.get("owner_first_name") or "").strip()
    if (state.merchant or {}).get("category_slug") == "dentists" and owner:
        owner = "Dr. " + re.sub(r"^dr\.?\s*", "", owner, flags=re.I)
    return owner, ident.get("name", "your business")


def _customer_commit_reply(state: ConversationState, msg: str) -> str:
    """The customer said yes — confirm THEIR booking/reminder using only facts already on file
    (their chosen slot if the trigger gave one, otherwise a plain confirmation). Never mention
    the merchant's marketing, drafts, or Google posts — that content is not for the customer."""
    payload = (state.trigger or {}).get("payload", {}) or {}
    slots = payload.get("available_slots") or payload.get("next_session_options") or []
    labels = [s.get("label") for s in slots if isinstance(s, dict) and s.get("label")]
    sender = (state.merchant or {}).get("identity", {}).get("name", "we")
    pick = re.match(r"^\s*([12])\s*$", (msg or "").strip())
    chosen = labels[int(pick.group(1)) - 1] if (pick and labels and int(pick.group(1)) <= len(labels)) else (labels[0] if labels else None)
    if chosen:
        return _h(state, f"Great, confirmed for {chosen} — {sender} will see you then. Reply STOP anytime if plans change.",
                  f"Great, {chosen} ke liye confirm ho gaya — {sender} aapka wahan intezaar karenge. Plans change ho to STOP reply kar dijiye.")
    return _h(state, f"Great, confirmed — {sender} will be in touch with the details shortly. Reply STOP anytime if you'd rather not be contacted.",
              f"Great, confirm ho gaya — {sender} jald hi details ke saath sampark karenge. Contact nahi chahiye to STOP reply kar dijiye.")


def _template(state: ConversationState, move: str, msg: str) -> str:
    owner, name = _names(state)
    kind = state.kind or (state.trigger or {}).get("kind", "")
    topic = humanize(kind) if kind else "what we discussed"
    if move == "auto_reply":
        return _h(state, f"Looks like an automated reply 🙂 {('For ' + owner + ': ') if owner else ''}whenever you see this, "
                         "just reply YES and I'll pick it up from there.",
                  f"Lagta hai yeh auto-reply hai 🙂 {(owner + ', ') if owner else ''}jab aap dekhein, bas YES reply kar dijiye — "
                  "main wahin se aage badhaungi.")
    if move == "hostile":
        return _h(state, "Sorry for the bother — that's not the experience I want to give you. I'll only message when "
                         "there's something genuinely useful for your business. If you'd prefer no messages at all, "
                         "reply STOP and I'll stop right away.",
                  "Sorry, pareshaan karne ka iraada nahi tha. Main sirf tabhi message karungi jab aapke business ke "
                  "liye kuch kaam ka ho. Bilkul message nahi chahiye to STOP likh dijiye, turant band kar dungi.")
    # one verb phrase for "what I'll do": either what the last message offered, or the kind's default artifact
    action = _offer_from_last(state.last_bot_body) or f"prepare {KIND_DELIVERABLE.get(kind, 'the first draft')}"
    action = re.sub(r"\b(for you|you can forward)\b", "", action).strip()
    if move == "off_topic":
        return _h(state, f"That one's outside what I can help with — a CA or specialist is the right person for it. "
                         f"What I can do today for {name} is {action} — shall I go ahead?",
                  f"Yeh mere scope ke bahar hai — iske liye CA/specialist sahi rahenge. {name} ke liye main aaj "
                  f"yeh kar sakti hoon: {action}. Shuru karoon?")
    if move == "commit":
        fs = build_facts(state.category or {}, state.merchant or {}, state.trigger or {}, state.customer)
        if state.customer_id:
            # a customer saying yes is confirming THEIR booking/reminder, not commissioning marketing content
            # for the business — never offer to "draft a Google post" or "prepare a draft for <merchant>" here.
            return _customer_commit_reply(state, msg)
        artifact = build_artifact(fs)
        change = re.search(r"\b(but|change|instead|edit|modify|badal|badlo)\b", (msg or "").lower())
        if artifact:
            return _h(state, (f"Noted your change — here's the draft for {name} to edit from:" if change
                              else f"Done — here's the draft for {name}:") + f"\n{artifact}\n"
                             + ("Tell me the exact edit, or reply CONFIRM to keep it as is." if change
                                else "Reply CONFIRM and I'll treat this as approved."),
                      (f"Aapka badlav note kar liya — {name} ka draft yeh raha, isi se edit karte hain:" if change
                       else f"Done — {name} ke liye draft ready hai:") + f"\n{artifact}\n"
                      + ("Exact badlav bata dijiye, ya aise hi rakhna ho to CONFIRM reply karein." if change
                         else "CONFIRM reply karein, approved maan lungi."))
        # no fact-grounded artifact available for this kind — say so honestly instead of promising one later
        return _h(state, f"Done — I'll {action} for {name}, using only what's on file so nothing is made up. "
                         "Reply CONFIRM once you've seen it and I'll treat it as approved.",
                  f"Done — {name} ke liye {action}, sirf jo file mein hai wahi use karke. "
                  "Dekhne ke baad CONFIRM reply karein, approved maan lungi.")
    # engaged / question
    fs = build_facts(state.category or {}, state.merchant or {}, state.trigger or {}, state.customer)
    answer = _answer(state, msg, fs)
    if state.customer_id:
        # customer-facing: never merchant analytics, marketing drafts or "why now" internals — answer, then booking CTA
        labels = _slot_labels(state)
        ask = (_h(state, f"Reply 1 for {labels[0]} or 2 for {labels[1]} to book.", f"Book karne ke liye {labels[0]} ke liye 1 ya {labels[1]} ke liye 2 reply karein.")
               if len(labels) >= 2 else
               _h(state, "Reply YES and the team will get in touch to fix a time.", "YES reply karein, team aapse time fix karne ke liye sampark karegi."))
        lead = answer or _h(state, f"Thanks for asking — the {name} team will answer that directly.",
                            f"Poochne ke liye shukriya — {name} ki team aapko seedha bata degi.")
        return f"{lead} {ask}"
    anchor = _weak_spot(fs) or _peer_line(fs) or ""
    if answer:
        return _h(state, f"{answer} Next step: I'll {action} for {name} — reply YES and the draft lands here.",
                  f"{answer} Agla step: {name} ke liye {action} — YES bolo, draft yahin bhej deti hoon.")
    return _h(state, f"Concretely: I'll {action} for {name}, and nothing goes live until you approve it. "
                     + (f"Why now — {anchor}. " if anchor else "")
                     + "Reply YES and the first draft lands here.",
              f"Seedha plan: {name} ke liye main yeh karungi — {action}; aapke approve kiye bina kuch live nahi hota. "
              + (f"Abhi karne ki wajah — {anchor}. " if anchor else "")
              + "YES bolo, pehla draft yahin bhej deti hoon.")


def _slot_labels(state: ConversationState) -> list[str]:
    payload = (state.trigger or {}).get("payload", {}) or {}
    slots = payload.get("available_slots") or payload.get("next_session_options") or []
    return [s.get("label") for s in slots if isinstance(s, dict) and s.get("label")]


def _answer(state: ConversationState, msg: str, fs) -> Optional[str]:
    """Deterministic answer to the most common questions, from facts on file only. None if unrecognised
    (the caller then falls back to restating the plan). Never guesses hours, prices or medical facts."""
    low = (msg or "").lower()
    _, name = _names(state)
    offers = fs.f.get("active_offers") or []
    if state.customer_id:
        if re.search(r"\b(time|timing|timings|open|opening|close|closing|hours?|kab|address|where|location|kahan)\b", low):
            return _h(state, f"I don't have that detail in front of me — the {name} team will confirm it when they reach out.",
                      f"Yeh detail abhi mere paas nahi hai — {name} ki team sampark karte waqt confirm kar degi.")
        if re.search(r"\b(price|cost|charges?|fees?|rate|kitna|kitne|how much)\b", low):
            return (_h(state, f"The current offer is {offers[0]}.", f"Abhi ka offer: {offers[0]}.") if offers else
                    _h(state, f"The {name} team will confirm the price for you.", f"{name} ki team aapko price confirm kar degi."))
        if re.search(r"\b(pain|painful|hurt|hurts|safe|side ?effects?|dard|risk)\b", low):
            return _h(state, "That's best answered by the team in person — they'll walk you through it before anything starts.",
                      "Iska sahi jawab team milkar degi — shuru karne se pehle sab samjha denge.")
        return None
    perf = fs.f.get("perf") or {}
    payload = (state.trigger or {}).get("payload", {}) or {}
    item = fs.f.get("digest_item") or {}
    if re.search(r"\b(who (is|are) (this|you)|kaun|what is vera|who's this)\b", low):
        return _h(state, f"I'm Vera, magicpin's assistant for {name} — I help with your Google profile, offers and customer messages.",
                  f"Main Vera hoon, magicpin ki taraf se {name} ki assistant — Google profile, offers aur customer messages mein madad karti hoon.")
    if re.search(r"\b(ctr|views?|calls?|numbers?|stats|performance|kitne log|data)\b", low) and perf.get("views") is not None:
        line = (f"Last 30 days: {perf.get('views'):,} profile views, {perf.get('calls', 0):,} calls"
                + (f", CTR {perf['ctr'] * 100:.1f}%".replace(".0%", "%") if perf.get("ctr") is not None else ""))
        peer = (fs.f.get("peer") or {}).get("avg_ctr")
        if peer and perf.get("ctr") is not None:
            line += f" (peer average {peer * 100:.1f}%)".replace(".0%)", "%)")
        return line + "."
    if re.search(r"\b(price|cost|charges?|fees?|pay|paisa|kitna|how much)\b", low):
        if payload.get("renewal_amount"):
            return _h(state, f"Your {payload.get('plan', '')} renewal is ₹{int(payload['renewal_amount']):,}.".replace("  ", " "),
                      f"Aapka {payload.get('plan', '')} renewal ₹{int(payload['renewal_amount']):,} ka hai.".replace("  ", " "))
        return _h(state, "I don't have pricing for this on file, so I won't guess — the draft itself is just text for you to "
                         "review, and nothing goes live without your OK.",
                  "Iski pricing mere paas file mein nahi hai, isliye andaaza nahi lagaungi — draft sirf aapke review ke liye "
                  "hai, aapke OK ke bina kuch live nahi hota.")
    if re.search(r"\b(batch|batches|which|source|study|paper|link|details?|kaunsa|kaunse)\b", low):
        if payload.get("affected_batches"):
            return f"The affected batches are {', '.join(payload['affected_batches'])}" + \
                   (f" ({item['source']})." if item.get("source") else ".")
        if item.get("title"):
            return f"It's \"{item['title']}\"" + (f" — {item['source']}." if item.get("source") else ".")
    if re.search(r"\b(offer|offers|discount|deal)\b", low):
        return (f"Your live offer{'s are' if len(offers) > 1 else ' is'}: {'; '.join(offers)}." if offers else
                _h(state, "You don't have a live offer on your profile right now.", "Abhi aapke profile pe koi live offer nahi hai."))
    return None


REPLY_SYSTEM = """You are Vera, magicpin's WhatsApp growth assistant for Indian merchants, mid-conversation.
Write the NEXT message only. Rules:
- Use only facts from the FACT SHEET and the conversation. Never invent numbers, names, prices, dates, research.
- No self-introduction (unless they ask who you are), no preamble, no URLs. 1-4 short sentences. Exactly one call to action at the end.
- Match the requested language.
MOVE-SPECIFIC RULES:
- commit (merchant audience): the merchant said yes. Switch to ACTION: confirm you are doing it now ("Done —",
  "Drafting"), give the concrete artifact or its first lines (e.g. the actual draft post text) built from the
  facts, IN THIS MESSAGE — never promise to produce it later ("within the hour", "I'll send it soon"). End with
  "Reply CONFIRM ...". Do not claim an external action already happened (published, registered, booked, renewed,
  dispatched) — you can only draft/prepare; say the draft is ready and ask for one CONFIRM before anything is
  treated as approved. NEVER ask a qualifying question (no "would you", "do you", "can you tell", "what if", "how about").
- commit (customer audience): the CUSTOMER said yes — they are confirming THEIR OWN booking/reminder, not
  commissioning marketing work. Confirm their slot/reminder only, using their preferred slot from the facts if
  given. NEVER offer to draft a Google post, WhatsApp broadcast, or any content for the business — that is not
  for the customer to see or approve.
- engaged: answer their question directly from the facts (say honestly if you don't have that data — never guess
  opening hours, prices or medical facts), then advance one step. For a CUSTOMER, never mention the business's
  analytics, Google posts or drafts; answer, then offer the booking step.
- off_topic: decline in one friendly line (suggest the right kind of professional), then steer back to the original topic.
- hostile: one-line sincere apology, no pitch, tell them they can reply STOP to stop messages.
- auto_reply: one short line noting it looks automated, asking the owner to reply YES when they see it.
Return ONLY JSON: {"body": "..."}"""


def _say(state: ConversationState, move: str, msg: str) -> str:
    fallback = _template(state, move, msg)
    llm = get_llm()
    # LLM only where a template genuinely can't do the job: answering a free-form question ("engaged")
    # and producing the actual artifact after a yes ("commit"). Everything else is rule + template.
    if not llm.enabled or move not in ("engaged", "commit"):
        return fallback
    fs = build_facts(state.category or {}, state.merchant or {}, state.trigger or {}, state.customer)
    convo = "\n".join(f"{t['from'].upper()}: {t['body']}" for t in state.turns[-8:])
    lang = {"hinglish": "Hindi-English code-mix (Roman script)", "hindi": "Roman-script Hindi"}.get(state.language, "English")
    audience = "the merchant's customer (you write as the business)" if state.customer_id else "the merchant/owner"
    prompt = (f"MOVE: {move}\nAUDIENCE: {audience}\nLANGUAGE: {lang}\nTRIGGER KIND: {state.kind}\n\n"
              f"FACT SHEET:\n{fs.render(exclude=MERCHANT_ONLY if state.customer_id else ())}\n\nCONVERSATION SO FAR:\n{convo}\n\n"
              f"SAFE FALLBACK (improve on it, keep it truthful):\n{fallback}\n\nJSON only.")
    out = parse_json(llm.complete(REPLY_SYSTEM, prompt, timeout=8.0, max_tokens=500))
    body = str((out or {}).get("body", "")).strip()
    if not body:
        return fallback
    if validate(body, fs, state.sent_bodies, customer_facing=bool(state.customer_id)):
        return fallback
    if move == "commit" and any(q in body.lower() for q in QUALIFYING_PHRASES):
        return fallback
    return body
