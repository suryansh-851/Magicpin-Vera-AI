# Vera+ — merchant assistant for the magicpin AI Challenge

## Approach
**Facts first, then wording.** Every message is built in four steps:

1. **Fact sheet** (`vera/facts.py`): a deterministic pass turns the 4 contexts into short, readable facts. Examples: "CTR 2.1% vs peer average 3.0% (30% below)", the digest item resolved by id with its source citation, active offers, current-month seasonal beats, customer recency and consent. Every number the bot may use exists here.
2. **Playbook per trigger kind** (`vera/playbooks.py`): 28 kinds, each with an angle, engagement levers and a CTA shape. Research uses a citation plus the merchant's own cohort. A seasonal dip gets reframed toward retention. `active_planning_intent` delivers the draft instead of asking another question. Triggers with empty (placeholder) payloads fall back to merchant and category facts, and the bot never invents event details.
3. **Composer — deterministic first** (`vera/templates.py`): every proactive message is composed without an LLM, in a fixed shape.
   - Why-now first, with the trigger's own numbers.
   - Anchored on facts the merchant (and judge) can verify: owner name, locality, 30-day views/calls/CTR, signals, live offers.
   - One engagement lever, then a single YES ask.
   - Benchmarks and research are always cited with their source.
   - This makes it deterministic, instant, and immune to LLM rate limits. An LLM rewrite exists behind `LLM_COMPOSE=on` but is off: in our benchmark it re-introduced facts a reader can't verify (7-day deltas, computed dates, assumed services), and it would compete with replies for the same token budget.
4. **LLM only where required** (Groq `gpt-oss-120b`): answering a merchant's free-form question mid-conversation, and writing the actual artifact after a "yes". Its output goes through the same validator.
   - The validator checks number provenance, taboo words, URLs, one ask, no repeats, and no business internals in customer-facing text.
   - On a failed check or a 429, the bot falls back to the rule/template reply instantly.

**Multi-turn** (`conversation_handlers.py`) is rule-first. The rules pick the move and the LLM only writes the wording.
- **Auto-reply**: detected from canned phrasing or the same text repeating, counted per conversation and per merchant. The bot nudges once, then waits 24h, then ends.
- **Opt-out**: ends immediately and suppresses the merchant for 30 days.
- **Frustration**: one apology with a STOP path, then the conversation ends.
- **Commitment** ("ok let's do it"): switches straight to action. The reply confirms the work, delivers the artifact and closes with a single CONFIRM, never another qualifying question.
- **Off-topic** (e.g. GST): a one-line decline, then back to the original topic.
- **Questions** ("what's my CTR?", "who is this?", "which batches?"): answered from the fact sheet even when the LLM is unavailable; customers never see business analytics, and hours/prices/medical facts are never guessed.
- **Approval**: CONFIRM after a delivered draft closes the loop; "thanks" ends the conversation. Customers picking slot 1/2 get that exact slot confirmed.
- **"Later"**: backs off for 4–24h.
- Replies follow the merchant's language turn by turn.

**Server** (`bot.py`, FastAPI):
- Contexts are versioned and idempotent; a stale version returns 409.
- Triggers are composed in the background as soon as they're pushed. They're re-composed when their merchant, category or customer version changes, so injected digests and performance numbers are picked up.
- A tick sends at most one action per merchant, respects suppression keys and customer consent, and stops after 3 unanswered nudges.
- Ticks stay within an 8s budget, using a template fallback for anything slow.

## Tradeoffs
- **Grounding over flair**: the validator may reject a clever line containing a number it can't trace. We accepted this because the rubric penalises fabrication far more than it rewards flourish.
- **No invented specifics**: we don't invent specifics the case studies invent (named nearby offices, "22 affected customers"). Where the data only gives a total, we say "you have 240 chronic-Rx customers, I can filter the affected ones".
- **In-memory state**: simple and fast, but it needs a single always-on instance.
- **Language**: light Hinglish for Hindi-belt merchants and English for South-India merchants. Customers get exactly their `language_pref`.

## What would have helped most
1. Real trigger payloads for the generated triggers (75 of the 100 are placeholders).
2. Merchant appointment and slot availability, and per-customer service history for generated customers.
3. Which of a merchant's customers are affected by an alert (batch-level dispensing data).
4. Past message outcomes (reply or ignore per template), so the bot could learn which levers work for each merchant.

## Run
```
pip install -r requirements.txt
cp .env.example .env                  # then set LLM_API_KEY (Groq); the bot also runs with no key, templates only
python dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded
uvicorn bot:app --port 8080
python scripts/make_submission.py     # writes submission.jsonl
```

Tests, packaging and deployment (Render + keep-alive): see `DEPLOY.md`.
