"""Vera bot — magicpin AI Challenge submission.

Run:  uvicorn bot:app --host 0.0.0.0 --port 8080
Also exposes compose(category, merchant, trigger, customer) for offline use.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fwait
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib import request as urlrequest

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

import conversation_handlers as ch
from vera import composer
from vera.facts import customer_language, merchant_language, parse_date
from vera.llm import get_llm
from vera.store import SCOPES, ContextStore

START = time.time()
TICK_BUDGET = float(os.getenv("TICK_BUDGET_SECONDS", "8"))
REPLY_BUDGET = float(os.getenv("REPLY_BUDGET_SECONDS", "9"))
MAX_ACTIONS = 20

app = FastAPI(title="Vera — magicpin merchant assistant")
store = ContextStore()
pool = ThreadPoolExecutor(max_workers=int(os.getenv("COMPOSE_WORKERS", "12")))

_lock = threading.RLock()
_precomposed: dict[str, tuple[tuple, dict]] = {}      # trigger_id -> (version_key, result)
_inflight: dict[str, Any] = {}                          # trigger_id -> Future
_sent_keys: set[str] = set()                            # suppression keys already used
_sent_topics: set[tuple] = set()                        # (merchant, customer, kind) for detail-less triggers
_conversations: dict[str, ch.ConversationState] = {}
_merchant_unanswered: dict[str, int] = {}               # merchant_id -> consecutive bot sends with no reply


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Public contract from challenge-brief §7.1."""
    out = composer.compose(category, merchant, trigger, customer)
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}


# ---------------------------------------------------------------- helpers


def _now_date(now_iso: Optional[str]) -> Optional[date]:
    return parse_date(now_iso) if now_iso else None


def _resolve(trigger_id: str) -> Optional[tuple[dict, dict, dict, Optional[dict]]]:
    trg = store.get("trigger", trigger_id)
    if not trg:
        return None
    merchant = store.get("merchant", trg.get("merchant_id"))
    if not merchant:
        return None
    category = store.get("category", merchant.get("category_slug")) or {}
    customer = store.get("customer", trg.get("customer_id")) if trg.get("customer_id") else None
    return category, merchant, trg, customer


def _version_key(trigger_id: str) -> tuple:
    trg = store.get("trigger", trigger_id) or {}
    merchant = store.get("merchant", trg.get("merchant_id")) or {}
    return (store.version("trigger", trigger_id), store.version("merchant", trg.get("merchant_id")),
            store.version("category", merchant.get("category_slug")), store.version("customer", trg.get("customer_id")))


def _compose_trigger(trigger_id: str, now: Optional[date] = None) -> Optional[dict]:
    res = _resolve(trigger_id)
    if not res:
        return None
    category, merchant, trg, customer = res
    vkey = _version_key(trigger_id)
    out = composer.compose(category, merchant, trg, customer, now=now, time_budget=20.0)
    with _lock:
        _precomposed[trigger_id] = (vkey, out)
        _inflight.pop(trigger_id, None)
    return out


def _schedule_precompose(trigger_id: str) -> None:
    with _lock:
        fut = _inflight.get(trigger_id)
        if fut is not None and not fut.done():
            return
        _inflight[trigger_id] = pool.submit(_compose_trigger, trigger_id)


def _affected_triggers(scope: str, context_id: str) -> list[str]:
    out = []
    for tid, trg in store.all("trigger").items():
        if scope == "merchant" and trg.get("merchant_id") == context_id:
            out.append(tid)
        elif scope == "customer" and trg.get("customer_id") == context_id:
            out.append(tid)
        elif scope == "category":
            m = store.get("merchant", trg.get("merchant_id")) or {}
            if m.get("category_slug") == context_id:
                out.append(tid)
    return out


def _consent_ok(customer: Optional[dict]) -> bool:
    if not customer:
        return True
    prefs = customer.get("preferences", {}) or {}
    scope = (customer.get("consent", {}) or {}).get("scope") or []
    return bool(scope) and prefs.get("reminder_opt_in", True) is not False


def _conv_id(trg: dict) -> str:
    mid = str(trg.get("merchant_id", "m"))
    short_m = "_".join(mid.split("_")[:2])
    who = trg.get("customer_id")
    who = ("_" + "_".join(str(who).split("_")[:3])) if who else ""
    key = re.sub(r"[^a-z0-9]+", "_", str(trg.get("suppression_key") or trg.get("id", "")).lower()).strip("_")[-24:]
    return f"conv_{short_m}{who}_{trg.get('kind', 'msg')}_{key}"


def _trigger_rank(trg: dict, exp: Optional[date], now: Optional[date]) -> tuple:
    """Deterministic value ranking for candidates competing for the same tick slot (lower sorts first).
    Order: urgency (desc) > has real payload detail over a placeholder > closer to expiry > trigger id.
    A detailed urgency-3 trigger should win over an empty placeholder urgency-3 one; a same-urgency
    trigger closing sooner should be preferred over one with more runway."""
    urgency = -(trg.get("urgency") or 0)
    is_placeholder = 1 if (trg.get("payload") or {}).get("placeholder") else 0
    days_to_expiry = (exp - now).days if (exp and now) else 9999
    return (urgency, is_placeholder, days_to_expiry)


# ---------------------------------------------------------------- endpoints


def _keepalive_loop(url: str, interval: float) -> None:
    # explicit User-Agent: Render sits behind Cloudflare, which can reject urllib's default
    req = urlrequest.Request(url, headers={"User-Agent": "vera-keepalive/1.0"})
    while True:
        time.sleep(interval)
        try:
            urlrequest.urlopen(req, timeout=30).read()
        except Exception as e:  # never let a failed ping kill the loop
            print(f"[keepalive] ping failed: {e}", flush=True)


@app.on_event("startup")
def _start_keepalive() -> None:
    """Render Free sleeps after ~15 min without inbound traffic, which wipes in-memory state. Pinging our own
    public URL goes through Render's proxy, so it counts as traffic. RENDER_EXTERNAL_URL is set by Render itself;
    locally neither var is set and this is a no-op."""
    base = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    interval = float(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "300"))
    if base and interval > 0:
        threading.Thread(target=_keepalive_loop, args=(f"{base}/v1/healthz", interval),
                         name="keepalive", daemon=True).start()


@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": store.counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera+"),
        "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Suryansh Singh").split(",") if m.strip()],
        "model": get_llm().describe(),
        "approach": ("fact-sheet grounded composer: deterministic fact extraction from the 4 contexts -> per-trigger-kind "
                     "playbook -> deterministic, validated templates for every proactive message (judge-visible facts first, "
                     "no invented specifics); LLM (temp 0) only for free-form replies and post-commit artifacts, checked by the "
                     "same validator with an instant template fallback; rule-first multi-turn state machine (auto-reply, "
                     "opt-out, intent-to-action, off-topic, approval close)"),
        "version": "1.0.0",
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-04-26T08:00:00Z"),
    }


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_json", "details": "body is not JSON"})
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": f"scope={scope!r}"})
    if not cid or not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_payload",
                                                      "details": "context_id and object payload required"})
    try:
        version = int(version)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_version", "details": str(version)})

    accepted, current = store.put(scope, cid, version, payload)
    if not accepted:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current})

    # adaptive: (re)compose triggers whose inputs just changed
    if scope == "trigger":
        _schedule_precompose(cid)
    else:
        for tid in _affected_triggers(scope, cid):
            _schedule_precompose(tid)
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}",
            "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


@app.post("/v1/tick")
def tick(body: dict = Body(default={})):
    # sync handler: FastAPI runs it in a worker thread, so waiting on composes never blocks /v1/healthz
    started = time.monotonic()
    now = _now_date(body.get("now"))
    available = [t for t in body.get("available_triggers") or [] if isinstance(t, str)]

    candidates = []
    for tid in available:
        res = _resolve(tid)
        if not res:
            continue
        _, merchant, trg, customer = res
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        skey = trg.get("suppression_key") or tid
        if skey in _sent_keys:
            continue
        topic = (mid, trg.get("customer_id"), trg.get("kind"))
        if (trg.get("payload") or {}).get("placeholder") and topic in _sent_topics:
            continue  # same detail-less nudge already sent to this target — would just repeat itself
        # defensive expiry check: dates are truncated (no time-of-day), so "expires today" is still sendable
        # today — only skip once `now` is strictly past the expiry date, never on the expiry day itself.
        exp = parse_date(trg.get("expires_at"))
        if exp and now and now > exp:
            continue
        if ch.merchant_opted_out(mid):
            continue
        if ch.customer_opted_out(mid, trg.get("customer_id")):
            continue
        if not _consent_ok(customer):
            continue
        if not customer and _merchant_unanswered.get(mid, 0) >= 3:
            continue  # 3 unanswered nudges -> stop
        candidates.append((_trigger_rank(trg, exp, now), tid, trg, mid))
    candidates.sort(key=lambda x: (x[0], x[1]))

    # one action per merchant/customer target per tick
    chosen, seen = [], set()
    for _, tid, trg, mid in candidates:
        target = (mid, trg.get("customer_id"))
        if target in seen:
            continue
        seen.add(target)
        chosen.append(tid)
        if len(chosen) >= MAX_ACTIONS:
            break

    # use precomposed results where fresh; compose the rest in parallel within the budget
    results: dict[str, dict] = {}
    futures = {}
    for tid in chosen:
        with _lock:
            pre = _precomposed.get(tid)
            fut = _inflight.get(tid)
        if pre and pre[0] == _version_key(tid):
            results[tid] = pre[1]
        elif fut is not None and not fut.done():
            futures[tid] = fut
        else:
            futures[tid] = pool.submit(_compose_trigger, tid, now)
    if futures:
        remaining = max(0.5, TICK_BUDGET - (time.monotonic() - started))
        fwait(list(futures.values()), timeout=remaining)
    for tid, fut in futures.items():
        if fut.done() and fut.result():
            results[tid] = fut.result()
        else:  # too slow — deterministic template (no LLM) so we never time out
            res = _resolve(tid)
            if res:
                results[tid] = composer.compose(*res, now=now, use_llm=False)

    actions = []
    for tid in chosen:
        out = results.get(tid)
        if not out:
            continue
        trg = store.get("trigger", tid) or {}
        conv_id = _conv_id(trg)
        n = 2
        while conv_id in _conversations:
            conv_id = f"{_conv_id(trg)}_{n}"
            n += 1
        category, merchant, _, customer = _resolve(tid)
        state = ch.ConversationState(
            conversation_id=conv_id, merchant_id=trg.get("merchant_id"), customer_id=trg.get("customer_id"),
            trigger_id=tid, kind=trg.get("kind"), category=category, merchant=merchant, trigger=trg, customer=customer,
            language=customer_language(customer) if customer else merchant_language(merchant),
            last_cta=out.get("cta"),
        )
        state.turns.append({"from": "vera", "body": out["body"]})
        with _lock:
            _conversations[conv_id] = state
            _sent_keys.add(out["suppression_key"])
            _sent_topics.add((trg.get("merchant_id"), trg.get("customer_id"), trg.get("kind")))
            if not trg.get("customer_id"):
                _merchant_unanswered[state.merchant_id] = _merchant_unanswered.get(state.merchant_id, 0) + 1
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": trg.get("merchant_id"),
            "customer_id": trg.get("customer_id"),
            "send_as": out["send_as"],
            "trigger_id": tid,
            "template_name": out["template_name"],
            "template_params": out["template_params"],
            "body": out["body"],
            "cta": out["cta"],
            "suppression_key": out["suppression_key"],
            "rationale": out["rationale"],
        })
    return {"actions": actions}


@app.post("/v1/reply")
def reply(body: dict = Body(default={})):
    conv_id = body.get("conversation_id") or f"conv_adhoc_{int(time.time())}"
    message = str(body.get("message") or "")
    with _lock:
        state = _conversations.get(conv_id)
        if state is None:  # conversation we didn't start (or after a restart) — rebuild from store
            mid = body.get("merchant_id")
            merchant = store.get("merchant", mid) or {}
            category = store.get("category", merchant.get("category_slug")) or {}
            customer = store.get("customer", body.get("customer_id")) if body.get("customer_id") else None
            state = ch.ConversationState(
                conversation_id=conv_id, merchant_id=mid, customer_id=body.get("customer_id"),
                category=category, merchant=merchant, customer=customer,
                language=customer_language(customer) if customer else (merchant_language(merchant) if merchant else "english"),
            )
            _conversations[conv_id] = state
        if state.merchant_id:
            _merchant_unanswered[state.merchant_id] = 0

    fut = pool.submit(ch.respond, state, message)
    try:
        result = fut.result(timeout=REPLY_BUDGET)
    except Exception:
        result = {"action": "wait", "wait_seconds": 1800,
                  "rationale": "Could not compose a quality reply in time; backing off briefly rather than sending filler."}
    if result.get("action") == "send" and not str(result.get("body", "")).strip():
        result = {"action": "wait", "wait_seconds": 1800, "rationale": "Nothing useful to add right now."}
    return result


@app.post("/v1/teardown")
async def teardown():
    store.clear()
    with _lock:
        _precomposed.clear()
        _inflight.clear()
        _sent_keys.clear()
        _sent_topics.clear()
        _conversations.clear()
        _merchant_unanswered.clear()
    ch.reset_memory()
    return {"ok": True}
