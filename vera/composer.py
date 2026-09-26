"""compose(category, merchant, trigger, customer) -> message dict."""

from __future__ import annotations

import os
import re
import time
from datetime import date
from typing import Iterable, Optional

from .facts import FactSheet, build_facts, humanize
from .llm import get_llm, parse_json
from .playbooks import CONFIRM, NONE, OPEN, SLOTS, YES_STOP, playbook, template_name
from .templates import render
from .validator import validate

VALID_CTAS = {YES_STOP, OPEN, SLOTS, CONFIRM, NONE}

# business-internal sections a merchant's CUSTOMER must never see (analytics, peers, our chat with the owner)
MERCHANT_ONLY = ("PERFORMANCE", "PEER_BENCHMARK", "SIGNALS", "CUSTOMER_BASE", "RECENT_CONVERSATION",
                 "CATEGORY_OFFER_CATALOG", "OTHER_DIGEST", "SEARCH_TRENDS", "SEASONAL", "TRIGGER_DIGEST_ITEM")

SYSTEM_BASE = """You are Vera, magicpin's growth assistant for Indian local merchants, writing ONE WhatsApp message.
You are scored by a strict judge on: specificity (verifiable numbers/dates/sources), category voice fit,
merchant personalisation, clear why-now (the trigger), and how likely the merchant is to reply.

HARD RULES
1. Use ONLY facts from the FACT SHEET. Every number, date, price, name, source you write must appear there.
   Never invent competitor names, research, customer counts, prices, dates or slots. If a detail is missing, leave it out.
   Offers from CATEGORY_OFFER_CATALOG may be SUGGESTED ("try X"), never described as already running.
2. Open with the salutation given, then immediately the why-now. No preamble ("Hope you're well", "I am reaching out").
   Do not introduce yourself.
3. Anchor on 2-3 concrete facts (numbers, dates, source citations). Prefer service+price ("Hair Spa @ ₹499") over "% off".
4. Exactly ONE call to action, in the last sentence. Low friction: offer to do the work yourself
   ("I've drafted X — reply YES"). No multi-option menus except booking slots for customers.
5. No URLs. No hashtags. No ALL-CAPS hype. Max ~2 emojis, none for clinical/pharmacy merchant messages.
6. Keep it tight: 2-5 short sentences, ideally 250-480 characters.
7. Add judgment, not just templating: interpret the numbers (is it good/bad/normal, what to do about it).
8. Use at least one engagement lever: loss aversion, social proof from peer benchmarks, curiosity, reciprocity,
   effort externalisation, or asking the merchant a question about their business.

Return ONLY JSON: {"body": "...", "cta": "<one of: binary_yes_stop | open_ended | multi_choice_slot | binary_confirm_cancel | none>",
"rationale": "<1-2 sentences: why this message now, which facts and levers>"}"""

LANG_RULES = {
    "hinglish": "LANGUAGE: natural Hindi-English code-mix in Roman script (the way an Indian business owner texts) — "
                "mostly English, with Hindi connectors/phrases (e.g. 'aapka', 'is hafte', 'kar doon?'). Vera speaks as female ('kar deti hoon').",
    "hindi": "LANGUAGE: simple Roman-script Hindi with common English words (medicine names, dates stay as-is). Respectful ('aap', 'ji').",
    "english": "LANGUAGE: clear, simple Indian English.",
}


def _voice_block(fs: FactSheet, customer_facing: bool) -> str:
    v = fs.f.get("voice") or {}
    lines = [f"CATEGORY: {fs.f.get('category')}",
             f"VOICE: tone={v.get('tone')}, register={v.get('register')}"]
    if v.get("vocab_allowed"):
        lines.append("Domain vocabulary you may use: " + ", ".join(v["vocab_allowed"][:16]))
    if v.get("vocab_taboo"):
        lines.append("NEVER use: " + ", ".join(v["vocab_taboo"]))
    if v.get("tone_examples"):
        lines.append("Tone examples: " + " | ".join(v["tone_examples"][:3]))
    if customer_facing:
        lines.append("AUDIENCE: this goes to the merchant's CUSTOMER, sent from the merchant's WhatsApp number "
                     "(write as the business, e.g. '<business> here'). Warm, respectful, no medical/result claims, "
                     "no guilt. Use the customer's first name (or the parent's name for a child). NEVER mention the "
                     "business's views/calls/ratings/peer comparisons, magicpin, or Vera — the customer must not see internals.")
    else:
        lines.append("AUDIENCE: the merchant/owner. Peer-to-peer, like a sharp colleague who has already done the homework.")
    return "\n".join(lines)


def _build_prompt(fs: FactSheet, trigger: dict, draft: str, draft_cta: str, customer_facing: bool) -> tuple[str, str]:
    pb = playbook(trigger.get("kind", ""))
    lang = fs.f.get("customer_language") if customer_facing else fs.f.get("language")
    system = SYSTEM_BASE + "\n\n" + _voice_block(fs, customer_facing) + "\n" + LANG_RULES.get(lang, LANG_RULES["english"])
    strategy = pb["placeholder"] if fs.f.get("placeholder") else pb["angle"]
    prompt = (
        f"TRIGGER KIND: {trigger.get('kind')}  (scope={trigger.get('scope')}, urgency={trigger.get('urgency')})\n"
        f"STRATEGY FOR THIS KIND: {pb['angle']}\n"
        + (f"NOTE: {strategy}\n" if fs.f.get("placeholder") else "")
        + f"PREFERRED LEVERS: {pb['levers']}\nPREFERRED CTA TYPE: {draft_cta}\n\n"
        f"FACT SHEET (the only facts you may use):\n{fs.render(exclude=MERCHANT_ONLY if customer_facing else ())}\n\n"
        f"SAFE BASELINE DRAFT (fact-checked; improve voice, judgment and compulsion — keep it truthful):\n{draft}\n\n"
        "Write the final message now. JSON only."
    )
    return system, prompt


def _template_params(salutation: str, body: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", body.strip())
    head = parts[0] if parts else body
    tail = parts[-1] if len(parts) > 1 else ""
    middle = " ".join(parts[1:-1]) if len(parts) > 2 else ""
    return [salutation, head, middle, tail]


def _default_rationale(fs: FactSheet, trigger: dict, body: str, cta: str) -> str:
    """Describe what THIS message actually does, so the rationale always matches the body."""
    pb = playbook(trigger.get("kind", ""))
    kind = humanize(trigger.get("kind", "trigger"))
    anchors = re.findall(r"₹[\d,]+|\d[\d,.]*\s?(?:%|km|days?|calls|profile views|views|reviews|mSv|CDE credits)"
                         r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} \w{3}", body)
    anchors = list(dict.fromkeys(a.strip() for a in anchors))[:5]
    who = "the customer, sent as the merchant" if trigger.get("scope") == "customer" else f"{fs.f.get('salutation')}"
    why = ("trigger payload had no details, so the why-now is anchored only on this merchant's own data — nothing invented"
           if fs.f.get("placeholder") else f"{kind} trigger")
    return (f"Why now: {why}. To {who}; verifiable anchors used: {', '.join(anchors) if anchors else 'names/dates from context'}. "
            f"Levers: {pb['levers']}. One ask, last ({cta}); language matched to "
            f"{'customer preference' if trigger.get('scope') == 'customer' else 'merchant languages'}.")


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Optional[date] = None, previous_bodies: Iterable[str] = (),
            time_budget: float = 20.0, use_llm: bool = True) -> dict:
    """Main entry point. Deterministic for the same inputs (temperature 0 + cache)."""
    started = time.monotonic()
    category, merchant, trigger = category or {}, merchant or {}, trigger or {}
    customer_facing = trigger.get("scope") == "customer" or customer is not None
    fs = build_facts(category, merchant, trigger, customer if customer_facing else None, now=now)
    previous_bodies = list(previous_bodies)

    draft, draft_cta = render(fs)
    body, cta, rationale, source = draft, draft_cta, _default_rationale(fs, trigger, draft, draft_cta), "template"

    # Proactive messages are template-first (deterministic, instant, never rate-limited).
    # Set LLM_COMPOSE=on to let the LLM rewrite them too.
    llm = get_llm()
    if use_llm and llm.enabled and os.getenv("LLM_COMPOSE", "off").lower() in ("on", "1", "true"):
        system, prompt = _build_prompt(fs, trigger, draft, draft_cta, customer_facing)
        for attempt in range(2):
            remaining = time_budget - (time.monotonic() - started)
            if remaining < 3:
                break
            out = parse_json(llm.complete(system, prompt, timeout=min(12.0, remaining - 1)))
            if not out or not out.get("body"):
                break
            cand = str(out["body"]).strip()
            errs = validate(cand, fs, previous_bodies, customer_facing=customer_facing)
            if not errs:
                body = cand
                c = str(out.get("cta", "")).strip()
                cta = c if c in VALID_CTAS else draft_cta
                rationale = str(out.get("rationale") or rationale).strip()
                source = llm.describe()
                break
            prompt += ("\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n" + cand + "\nPROBLEMS: " + "; ".join(errs)
                       + "\nFix every problem and return JSON again.")

    return {
        "body": body,
        "cta": cta,
        "send_as": "merchant_on_behalf" if customer_facing else "vera",
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('kind')}:{merchant.get('merchant_id')}",
        "rationale": rationale,
        "template_name": template_name(trigger.get("kind", ""), trigger.get("scope", "merchant")),
        "template_params": _template_params(
            fs.f.get("customer_name") if customer_facing and fs.f.get("customer_name") else fs.f.get("salutation", ""), body),
        "_source": source,
    }
