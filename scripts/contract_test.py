"""Plays the judge harness against a running bot. Usage: python contract_test.py [base_url]"""
import json, sys, time
from pathlib import Path
from urllib import request, error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
EXP = Path(__file__).resolve().parent.parent / "dataset" / "expanded"
fails = 0


def call(method, path, body=None, timeout=30):
    req = request.Request(BASE + path, method=method, headers={"Content-Type": "application/json"},
                          data=json.dumps(body).encode() if body is not None else None)
    t = time.time()
    try:
        with request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t
    except error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.time() - t


def check(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    fails += not cond


def load(sub, key):
    return {d[key]: d for d in (json.loads(f.read_text(encoding="utf-8")) for f in (EXP / sub).glob("*.json"))}


cats, ms, cs, ts = load("categories", "slug"), load("merchants", "merchant_id"), load("customers", "customer_id"), load("triggers", "id")

s, b, _ = call("GET", "/v1/healthz"); check(s == 200 and b["status"] == "ok", f"healthz {b}")
s, b, _ = call("GET", "/v1/metadata"); check(s == 200 and "team_name" in b, "metadata")

for scope, data in (("category", cats), ("merchant", ms), ("customer", cs)):
    for cid, p in data.items():
        s, b, _ = call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": 1, "payload": p,
                                              "delivered_at": "2026-04-26T10:00:00Z"})
        if s != 200:
            check(False, f"push {scope}/{cid} -> {s} {b}")
s, b, _ = call("GET", "/v1/healthz")
check(b["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}, f"counts {b['contexts_loaded']}")

s, b, _ = call("POST", "/v1/context", {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 1,
                                       "payload": ms["m_001_drmeera_dentist_delhi"], "delivered_at": "x"})
check(s == 409 and b.get("reason") == "stale_version" and b.get("current_version") == 1, f"re-push same version -> {s} {b}")
s, b, _ = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
check(s == 400, f"bad scope -> {s}")

# version bump with new numbers
m1 = json.loads(json.dumps(ms["m_001_drmeera_dentist_delhi"])); m1["performance"]["views"] = 2580
s, b, _ = call("POST", "/v1/context", {"scope": "merchant", "context_id": m1["merchant_id"], "version": 2, "payload": m1, "delivered_at": "x"})
check(s == 200 and b["accepted"], "version bump accepted")

tids = sorted(ts)
for tid in tids:
    call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1, "payload": ts[tid], "delivered_at": "x"})

s, b, dt = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": tids[:40]})
acts = b.get("actions", [])
check(s == 200 and dt < 10, f"tick 40 triggers -> {len(acts)} actions in {dt:.2f}s")
req = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name", "template_params",
       "body", "cta", "suppression_key", "rationale"}
check(all(req <= set(a) for a in acts), "all actions have required fields")
check(len(acts) <= 20, "<= 20 actions")
check(len({a["merchant_id"] + str(a["customer_id"]) for a in acts}) == len(acts), "one action per target per tick")
check(all("http" not in a["body"] for a in acts), "no URLs")
s, b2, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": [a["trigger_id"] for a in acts]})
check(len(b2["actions"]) == 0, f"suppression: resent {len(b2['actions'])} already-sent triggers")

conv = next(a for a in acts if a["merchant_id"] == "m_001_drmeera_dentist_delhi")
print("  first msg:", conv["body"][:120])

# replay 1: auto-reply hell (same conversation)
auto = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
seq = []
for turn in range(2, 6):
    s, r, dt = call("POST", "/v1/reply", {"conversation_id": conv["conversation_id"], "merchant_id": conv["merchant_id"],
                                          "customer_id": None, "from_role": "merchant", "message": auto,
                                          "received_at": "x", "turn_number": turn})
    seq.append(r["action"])
    if r["action"] == "end":
        break
check(seq[:3] == ["send", "wait", "end"], f"auto-reply sequence {seq}")

# simulator-style auto-reply: new conversation each time, different merchant
seq = []
for i in range(1, 5):
    s, r, _ = call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}", "merchant_id": "m_003_studio11_salon_hyderabad",
                                         "customer_id": None, "from_role": "merchant",
                                         "message": "Thank you for contacting us! Our team will respond shortly.",
                                         "received_at": "x", "turn_number": i + 1})
    seq.append(r["action"])
    if r["action"] == "end":
        break
check("end" in seq, f"simulator auto-reply across convs {seq}")

# replay 2: intent transition
c2 = next(a for a in acts if a["merchant_id"] != "m_001_drmeera_dentist_delhi" and not a["customer_id"])
for msg in ("Hmm tell me more, what exactly would you do?", "Ok lets do it. Whats next?"):
    s, r, dt = call("POST", "/v1/reply", {"conversation_id": c2["conversation_id"], "merchant_id": c2["merchant_id"],
                                          "customer_id": None, "from_role": "merchant", "message": msg,
                                          "received_at": "x", "turn_number": 2})
    print(f"  [{msg}] -> {r['action']}: {r.get('body', '')[:160]}")
low = r.get("body", "").lower()
check(r["action"] == "send" and not any(q in low for q in ["would you", "do you", "can you tell", "what if", "how about"])
      and any(w in low for w in ["done", "sending", "draft", "confirm", "next"]), "intent -> action mode")

# replay 3: hostile then off-topic
c3 = next(a for a in acts if a["merchant_id"] not in (conv["merchant_id"], c2["merchant_id"]) and not a["customer_id"])
for msg in ("Why are you bothering me. This is useless.", "Can you also help me file my GST?"):
    s, r, dt = call("POST", "/v1/reply", {"conversation_id": c3["conversation_id"], "merchant_id": c3["merchant_id"],
                                          "customer_id": None, "from_role": "merchant", "message": msg,
                                          "received_at": "x", "turn_number": 2})
    print(f"  [{msg}] -> {r['action']}: {r.get('body', '')[:160]}")
check(r["action"] == "send" and r.get("body"), "off-topic politely redirected")
s, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_hostile", "merchant_id": c3["merchant_id"], "customer_id": None,
                                     "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                                     "received_at": "x", "turn_number": 2})
check(r["action"] == "end", f"opt-out -> {r['action']}")
s, b, _ = call("POST", "/v1/tick", {"now": "2026-04-26T11:00:00Z", "available_triggers": tids})
check(all(a["merchant_id"] != c3["merchant_id"] for a in b["actions"]), "opted-out merchant not messaged again")

# customer-scoped
cust = [a for a in acts if a["customer_id"]]
check(all(a["send_as"] == "merchant_on_behalf" for a in cust), f"{len(cust)} customer actions sent as merchant_on_behalf")

# expired trigger is skipped at /v1/tick, but a same-day expiry is NOT (no timezone/truncation over-suppression)
expired = dict(ts["trg_006_festival_diwali"], id="trg_test_expired", suppression_key="test:expired",
              expires_at="2026-01-01T00:00:00Z", merchant_id="m_004_glamour_salon_pune")
call("POST", "/v1/context", {"scope": "trigger", "context_id": "trg_test_expired", "version": 1, "payload": expired, "delivered_at": "x"})
s, b, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_test_expired"]})
check(len(b["actions"]) == 0, "trigger past its expires_at is skipped at tick time")

same_day = dict(ts["trg_006_festival_diwali"], id="trg_test_expires_today", suppression_key="test:expires_today",
               expires_at="2026-04-26T23:59:59Z", merchant_id="m_004_glamour_salon_pune")
call("POST", "/v1/context", {"scope": "trigger", "context_id": "trg_test_expires_today", "version": 1, "payload": same_day, "delivered_at": "x"})
s, b, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ["trg_test_expires_today"]})
check(len(b["actions"]) == 1, "a trigger expiring TODAY is still sendable today")

# customer opt-out only suppresses that (merchant, customer) pair, never the whole merchant
cust_action = next((a for a in acts if a["customer_id"]), None)
if cust_action:
    s, r, _ = call("POST", "/v1/reply", {"conversation_id": cust_action["conversation_id"], "merchant_id": cust_action["merchant_id"],
                                         "customer_id": cust_action["customer_id"], "from_role": "customer",
                                         "message": "STOP", "received_at": "x", "turn_number": 2})
    check(r["action"] == "end", f"customer STOP -> {r['action']}")
    s, b, _ = call("POST", "/v1/tick", {"now": "2026-04-26T11:05:00Z", "available_triggers": tids})
    check(all(a["merchant_id"] != cust_action["merchant_id"] or a["customer_id"] != cust_action["customer_id"] for a in b["actions"]),
          "opted-out customer not messaged again")
    # prove the merchant itself is not suppressed: push one fresh merchant-facing trigger for the same
    # merchant and confirm it can still fire (a real assertion, not just "no error")
    fresh = dict(ts["trg_025_dormancy_glamour"], id="trg_test_merchant_unaffected",
                suppression_key="test:merchant_unaffected", merchant_id=cust_action["merchant_id"], customer_id=None)
    call("POST", "/v1/context", {"scope": "trigger", "context_id": "trg_test_merchant_unaffected", "version": 1,
                                 "payload": fresh, "delivered_at": "x"})
    s, b2, _ = call("POST", "/v1/tick", {"now": "2026-04-26T11:10:00Z", "available_triggers": ["trg_test_merchant_unaffected"]})
    check(len(b2["actions"]) == 1, "merchant-facing trigger for the same merchant still fires after the customer's opt-out")

s, b, _ = call("POST", "/v1/teardown", {})
s, b, _ = call("GET", "/v1/healthz")
check(sum(b["contexts_loaded"].values()) == 0, "teardown wipes state")
print(f"\n{fails} failures")

