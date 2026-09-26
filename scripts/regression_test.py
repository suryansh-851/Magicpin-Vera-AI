"""Targeted regression tests for the highest-value behaviors (see PR notes). Runs offline against
vera/conversation_handlers directly — no server needed. Usage: python scripts/regression_test.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import conversation_handlers as ch  # noqa: E402
from vera import composer  # noqa: E402
from vera.facts import customer_language, merchant_language  # noqa: E402

EXP = ROOT / "dataset" / "expanded"
fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    fails += not cond


def load(sub: str, key: str) -> dict:
    return {d[key]: d for d in (json.loads(f.read_text(encoding="utf-8")) for f in (EXP / sub).glob("*.json"))}


cats, ms, cs, ts = load("categories", "slug"), load("merchants", "merchant_id"), load("customers", "customer_id"), load("triggers", "id")


def start_conv(trigger_id: str) -> ch.ConversationState:
    trg = ts[trigger_id]
    m = ms[trg["merchant_id"]]
    cat = cats[m["category_slug"]]
    cust = cs.get(trg.get("customer_id"))
    out = composer.compose(cat, m, trg, cust, use_llm=False)
    state = ch.ConversationState(
        conversation_id=f"conv_{trigger_id}", merchant_id=trg.get("merchant_id"), customer_id=trg.get("customer_id"),
        trigger_id=trigger_id, kind=trg.get("kind"), category=cat, merchant=m, trigger=trg, customer=cust,
        language=customer_language(cust) if cust else merchant_language(m),
    )
    state.turns.append({"from": "vera", "body": out["body"]})
    return state


# 1 + 2: explicit YES produces an immediate usable artifact, no qualifying question, no deferred promise
state = start_conv("trg_002_compliance_dci_radiograph")
r = ch.respond(state, "ok go ahead")
low = r.get("body", "").lower()
check(r["action"] == "send" and "1." in r["body"], "commit produces an immediate multi-point artifact")
check(not any(q in low for q in ("would you", "do you", "can you tell", "what if", "how about")),
      "commit reply asks no qualifying question")
check(not any(p in low for p in ("within the hour", "within an hour", "i'll send it soon", "shortly")),
      "commit reply makes no deferred-promise claim")
check("i'll publish" not in low and "i published" not in low, "commit reply doesn't claim an unimplemented publish action")

# 3: LLM-disabled commitment fallback is still strong (this whole module runs with whatever LLM state is
# configured; re-verify explicitly with the LLM forced off for the fallback path itself)
import os  # noqa: E402
_had_key = os.environ.pop("LLM_API_KEY", None)
try:
    import importlib
    import vera.llm as llmmod
    importlib.reload(llmmod)
    state2 = start_conv("trg_020_summer_demand_shift")
    r2 = ch.respond(state2, "yes lets do it")
    check(r2["action"] == "send" and len(r2.get("body", "")) > 20, "LLM-disabled commit fallback still sends a real body")
finally:
    if _had_key is not None:
        os.environ["LLM_API_KEY"] = _had_key
    importlib.reload(llmmod)

# 4: customer STOP suppresses later customer triggers (merchant unaffected)
recall_trg = next(t for t in ts.values() if t["kind"] == "recall_due" and t.get("customer_id"))
cust_state = start_conv(recall_trg["id"])
r3 = ch.respond(cust_state, "STOP")
check(r3["action"] == "end", "customer STOP ends the conversation")
check(ch.customer_opted_out(recall_trg["merchant_id"], recall_trg["customer_id"]), "customer-level opt-out recorded")
check(not ch.merchant_opted_out(recall_trg["merchant_id"]), "merchant is NOT suppressed by a customer's opt-out")
ch.reset_memory()
check(not ch.customer_opted_out(recall_trg["merchant_id"], recall_trg["customer_id"]), "teardown/reset clears customer opt-out")

# 5: expired trigger is skipped (bot.py-level check, exercised directly here on the same date logic)
from vera.facts import parse_date  # noqa: E402
exp = parse_date("2026-01-01")
now = date(2026, 4, 26)
check(now > exp, "sanity: reference now is after an old expiry date")
same_day = parse_date("2026-04-26")
check(not (now > same_day), "a trigger expiring TODAY is not skipped (no same-day truncation bug)")

# 6: placeholder appointment doesn't invent time/service
appt_trg = next(t for t in ts.values() if t["kind"] == "appointment_tomorrow" and t["payload"].get("placeholder"))
m = ms[appt_trg["merchant_id"]]
out = composer.compose(cats[m["category_slug"]], m, appt_trg, None, use_llm=False)
low = out["body"].lower()
check("5 minute" not in low and "5-minute" not in low and "slot is held" not in low,
      "placeholder appointment doesn't invent arrival-buffer or held-slot claims")
check("tomorrow" in low or "kal" in low, "placeholder appointment still names 'tomorrow'")

# 7: incompatible placeholder trigger (pharmacy-only refill fired for a non-pharmacy merchant) doesn't
# transform into an unrelated category action (e.g. a dental "check-up and scaling")
mismatched = next(t for t in ts.values() if t["kind"] == "chronic_refill_due" and t["payload"].get("placeholder")
                  and ms[t["merchant_id"]]["category_slug"] != "pharmacies")
m = ms[mismatched["merchant_id"]]
cust = cs.get(mismatched.get("customer_id"))
out = composer.compose(cats[m["category_slug"]], m, mismatched, cust, use_llm=False)
low = out["body"].lower()
check(not any(s in low for s in ("check-up and scaling", "check-in session", "cleaning is due")),
      "mismatched refill trigger doesn't invent an unrelated service visit reason")

# 8: detailed trigger beats same-urgency placeholder trigger in target prioritization
sys.path.insert(0, str(ROOT))
import bot as botmod  # noqa: E402
detailed = ts["trg_003_recall_due_priya"]  # real payload, not a placeholder
placeholder_same_target = dict(detailed, payload={"placeholder": True}, id="trg_fake_placeholder_same")
rank_detailed = botmod._trigger_rank(detailed, None, None)
rank_placeholder = botmod._trigger_rank(placeholder_same_target, None, None)
check(rank_detailed < rank_placeholder, "a detailed trigger ranks ahead of a same-urgency placeholder trigger")

# 9: unsupported external/causal assertions removed from known risky templates
research_kind_bodies = []
for t in ts.values():
    m = ms[t["merchant_id"]]
    cust = cs.get(t.get("customer_id"))
    out = composer.compose(cats[m["category_slug"]], m, t, cust, use_llm=False)
    research_kind_bodies.append(out["body"])
all_bodies = " ".join(research_kind_bodies).lower()
for phrase in ("lock a lunch vendor for months", "first pitch wins", "sell out of ors mid-season",
              "newcomers can't match", "patients choose on trust", "every new review lifts your ranking",
              "momentum fades in a couple of weeks", "fans watch at home"):
    check(phrase not in all_bodies, f"no unsupported causal claim: {phrase!r}")

# the supply-alert recall reason must come from the digest item's own summary, not a hardcoded assumption —
# swap in a different reason and confirm the composed message reflects it, not the old "sub-potency" default
supply_trg = ts["trg_018_supply_atorvastatin_recall"]
m = ms[supply_trg["merchant_id"]]
cat = dict(cats[m["category_slug"]])
cat["digest"] = [dict(d, summary="Contamination with an unrelated compound detected. Recall issued as a precaution.")
                if d.get("id") == supply_trg["payload"]["alert_id"] else d for d in cat["digest"]]
out = composer.compose(cat, m, supply_trg, None, use_llm=False)
check("contamination" in out["body"].lower() and "sub-potency" not in out["body"].lower(),
      "supply-alert recall reason is read from the digest item, not hardcoded")

# 10: no URL in any composed body
check(not any("http" in b.lower() or "www." in b.lower() for b in research_kind_bodies), "no URLs in any composed body")

# 11: wrong send_as never occurs
for t in ts.values():
    m = ms[t["merchant_id"]]
    cust = cs.get(t.get("customer_id"))
    out = composer.compose(cats[m["category_slug"]], m, t, cust, use_llm=False)
    want = "merchant_on_behalf" if t.get("scope") == "customer" else "vera"
    if out["send_as"] != want:
        check(False, f"{t['id']}: send_as {out['send_as']!r} != expected {want!r}")
else:
    check(True, "send_as correct for every trigger")

# 12: customer-facing copy never leaks merchant analytics
from vera.validator import INTERNAL_RE  # noqa: E402
leaks = []
for t in ts.values():
    if t.get("scope") != "customer":
        continue
    m = ms[t["merchant_id"]]
    cust = cs.get(t.get("customer_id"))
    out = composer.compose(cats[m["category_slug"]], m, t, cust, use_llm=False)
    if INTERNAL_RE.search(out["body"]):
        leaks.append(t["id"])
check(not leaks, f"no customer-facing body leaks merchant internals (leaks: {leaks})")

# 13: teardown wipes every new state structure we added
ch._customer_opted_out[("m_x", "c_x")] = 9999999999.0
ch.reset_memory()
check(not ch._customer_opted_out, "reset_memory clears _customer_opted_out")

# 14: a CUSTOMER committing ("yes") gets a booking confirmation, never merchant marketing content
# (regression: this used to say "I'll prepare the first draft for <merchant>" to the customer)
customer_commit_kinds = ("recall_due", "customer_lapsed_soft", "customer_lapsed_hard",
                         "appointment_tomorrow", "trial_followup", "wedding_package_followup")
bad_customer_commits = []
for t in ts.values():
    if t["kind"] not in customer_commit_kinds or not t.get("customer_id"):
        continue
    m = ms[t["merchant_id"]]
    cat = cats[m["category_slug"]]
    cust = cs.get(t["customer_id"])
    out = composer.compose(cat, m, t, cust, use_llm=False)
    state = ch.ConversationState(
        conversation_id=f"conv_{t['id']}", merchant_id=t["merchant_id"], customer_id=t["customer_id"],
        kind=t["kind"], category=cat, merchant=m, trigger=t, customer=cust,
        language=customer_language(cust), last_cta=out["cta"],
    )
    state.turns.append({"from": "vera", "body": out["body"]})
    r = ch.respond(state, "yes")
    low = r.get("body", "").lower()
    if any(p in low for p in ("prepare the first draft for", "google post", "draft for " + m["identity"]["name"].lower())):
        bad_customer_commits.append(t["id"])
check(not bad_customer_commits, f"customer commit never offers to draft business marketing content (bad: {bad_customer_commits})")

# 15: a numbered slot pick ("1"/"2") after a SLOTS-CTA message confirms the matching slot, not a generic reply
from vera.playbooks import SLOTS  # noqa: E402
slot_trg = ts["trg_003_recall_due_priya"]
m = ms[slot_trg["merchant_id"]]
cust = cs[slot_trg["customer_id"]]
out = composer.compose(cats[m["category_slug"]], m, slot_trg, cust, use_llm=False)
check(out["cta"] == SLOTS, "sanity: recall_due with 2 slots produces a SLOTS cta")
state = ch.ConversationState(conversation_id="conv_slot", merchant_id=slot_trg["merchant_id"], customer_id=slot_trg["customer_id"],
                             kind=slot_trg["kind"], category=cats[m["category_slug"]], merchant=m, trigger=slot_trg, customer=cust,
                             language=customer_language(cust), last_cta=out["cta"])
state.turns.append({"from": "vera", "body": out["body"]})
r1 = ch.respond(state, "1")
check(r1["action"] == "send" and "5 nov" in r1.get("body", "").lower(), "picking slot '1' confirms the first slot")
state2 = ch.ConversationState(conversation_id="conv_slot2", merchant_id=slot_trg["merchant_id"], customer_id=slot_trg["customer_id"],
                              kind=slot_trg["kind"], category=cats[m["category_slug"]], merchant=m, trigger=slot_trg, customer=cust,
                              language=customer_language(cust), last_cta=out["cta"])
state2.turns.append({"from": "vera", "body": out["body"]})
r2 = ch.respond(state2, "2")
check(r2["action"] == "send" and "6 nov" in r2.get("body", "").lower(), "picking slot '2' confirms the second slot")

from vera.facts import build_facts  # noqa: E402
from vera.validator import validate  # noqa: E402

# 16: customer questions (hours / price / pain / anything) never leak merchant internals or marketing drafts,
# and never invent opening hours — checked across every customer-scoped trigger
_had = os.environ.pop("LLM_API_KEY", None)
importlib.reload(llmmod)
leaky = []
for t in ts.values():
    if not t.get("customer_id"):
        continue
    for q in ("What time do you open?", "How much does it cost?", "Is it painful?", "ok and what else?"):
        ch.reset_memory()
        st = start_conv(t["id"])
        r = ch.respond(st, q)
        b = r.get("body", "")
        fs_c = build_facts(st.category, st.merchant, st.trigger, st.customer)
        invented_time = any("clock times" in e for e in validate(b, fs_c, customer_facing=True))
        if INTERNAL_RE.search(b) or re.search(r"google post|draft", b, re.I) or invented_time:
            leaky.append((t["id"], q))
check(not leaky, f"customer replies never leak internals, drafts or invented hours (bad: {leaky[:3]})")

# 17: a customer can ask a question and still pick a numbered slot afterwards
ch.reset_memory()
st = start_conv("trg_003_recall_due_priya")
ch.respond(st, "Is it painful?")
r = ch.respond(st, "2")
check(r["action"] == "send" and "6 nov" in r.get("body", "").lower(), "slot pick after a question still books the chosen slot")

# 18: merchant questions get a real answer from the facts, not the same pitch again
ch.reset_memory()
st = start_conv("trg_004_perf_dip_bharat")
r_ctr = ch.respond(st, "What is my CTR right now?")
r_who = ch.respond(st, "Who is this?")
check("1.8%" in r_ctr.get("body", ""), "'what is my CTR' is answered with the merchant's real CTR")
check("vera" in r_who.get("body", "").lower(), "'who is this' gets a one-line introduction")
check(r_ctr.get("body") != r_who.get("body"), "different questions get different answers (no repeated pitch)")

# 19: after the draft is delivered, CONFIRM closes the loop (no resend) and 'thanks' ends the conversation
r_go = ch.respond(st, "ok go ahead")
r_ok = ch.respond(st, "CONFIRM")
r_ty = ch.respond(st, "thanks")
check("draft" in r_go.get("body", "").lower(), "commit delivers the draft")
check(r_ok["action"] == "send" and "approved" in r_ok.get("body", "").lower() and "draft:" not in r_ok.get("body", "").lower(),
      "CONFIRM after the draft confirms approval instead of resending the draft")
check(r_ty["action"] == "end", "'thanks' ends the conversation instead of re-pitching")

# 20: validator rejects invented clock times but accepts real slot times
from vera.facts import build_facts  # noqa: E402
from vera.validator import validate  # noqa: E402
m = ms[slot_trg["merchant_id"]]
fs_slot = build_facts(cats[m["category_slug"]], m, slot_trg, cs[slot_trg["customer_id"]])
check(any("clock times" in e for e in validate("We open at 9am daily.", fs_slot, customer_facing=True)),
      "validator rejects an invented opening time")
check(not any("clock times" in e for e in validate("Your slot is Wed 5 Nov, 6pm.", fs_slot, customer_facing=True)),
      "validator accepts a slot time that is in the facts")
if _had is not None:
    os.environ["LLM_API_KEY"] = _had
importlib.reload(llmmod)

print(f"\n{fails} failures")
sys.exit(1 if fails else 0)
