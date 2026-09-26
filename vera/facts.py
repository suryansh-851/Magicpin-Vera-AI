"""Deterministic fact-sheet builder.

Everything the composer is allowed to say comes from here. Each fact is a
human-readable line built only from the four input contexts, so the LLM (and the
template fallback) can be specific without inventing data. The validator later
checks that every number in an outgoing message appears in `allowed_numbers`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

REFERENCE_NOW = date(2026, 4, 26)  # dataset authoring date; used when no clock is given

HINDI_BELT_CITIES = {"delhi", "jaipur", "lucknow", "chandigarh", "mumbai", "pune", "ahmedabad",
                     "noida", "gurgaon", "gurugram", "kanpur", "indore", "bhopal", "patna"}

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


# ---------------------------------------------------------------- formatting

def fmt_num(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return str(n)
    if v.is_integer():
        return f"{int(v):,}"
    return f"{v:,.1f}"


def fmt_pct(x: Any, signed: bool = False) -> str:
    """0.021 -> '2.1%', -0.5 -> '-50%' (or '50%' when unsigned)."""
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return str(x)
    if not signed:
        v = abs(v)
    r = round(v, 1)
    s = f"{int(r)}" if float(r).is_integer() else f"{r}"
    if signed and v > 0:
        s = "+" + s
    return s + "%"


def fmt_inr(n: Any) -> str:
    try:
        return f"₹{int(float(n)):,}"
    except (TypeError, ValueError):
        return f"₹{n}"


def humanize(token: Any) -> str:
    return str(token).replace("_", " ").strip()


def norm_number(tok: str) -> Optional[str]:
    t = tok.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    s = ("%f" % v).rstrip("0").rstrip(".")
    return s or "0"


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in NUM_RE.findall(text or ""):
        n = norm_number(m)
        if n is not None:
            out.add(n)
    return out


def parse_date(s: Any) -> Optional[date]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def pretty_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"


def month_in_range(month_range: str, month: int) -> bool:
    """'Nov-Feb' / 'Apr-Jun' / 'Jan' / 'Feb 14' -> does month (1-12) fall in it?"""
    parts = [p.strip()[:3].lower() for p in re.split(r"[-–]", month_range or "") if p.strip()]
    idx = [MONTHS.index(p) + 1 for p in parts if p in MONTHS]
    if not idx:
        return False
    if len(idx) == 1:
        return month == idx[0]
    start, end = idx[0], idx[-1]
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


# ---------------------------------------------------------------- fact sheet

class FactSheet:
    """Holds labelled facts + structured fields for templates."""

    def __init__(self) -> None:
        self.sections: dict[str, list[str]] = {}
        self.f: dict[str, Any] = {}  # structured values for templates

    def add(self, section: str, line: Optional[str]) -> None:
        if line:
            self.sections.setdefault(section, []).append(line)

    def render(self, exclude: tuple[str, ...] = ()) -> str:
        out = []
        for sec, lines in self.sections.items():
            if any(sec.startswith(x) for x in exclude):
                continue
            out.append(f"[{sec}]")
            out.extend(f"- {l}" for l in lines)
        return "\n".join(out)

    @property
    def allowed_numbers(self) -> set[str]:
        nums = numbers_in(self.render())
        # generic small counts ("2-min", "3 posts", "Reply 1") are always fine
        nums.update(str(i) for i in range(0, 11))
        nums.update({"24", "48", "15", "30", "60", "90"})  # common time units (hours/min/days)
        return nums


def salutation(category_slug: str, merchant: dict) -> str:
    ident = merchant.get("identity", {}) or {}
    owner = (ident.get("owner_first_name") or "").strip()
    if not owner:
        return ident.get("name") or "there"
    bare = re.sub(r"^(dr\.?\s*)", "", owner, flags=re.I).strip()
    if category_slug == "dentists":
        return f"Dr. {bare}"
    return bare


def merchant_language(merchant: dict) -> str:
    ident = merchant.get("identity", {}) or {}
    langs = [str(l).lower() for l in ident.get("languages", []) or []]
    city = str(ident.get("city", "")).lower()
    if "hi" in langs and city in HINDI_BELT_CITIES:
        return "hinglish"
    return "english"


def customer_language(customer: dict) -> str:
    pref = str((customer.get("identity", {}) or {}).get("language_pref", "")).lower()
    if pref in ("hi",) or pref.startswith("hindi"):
        return "hindi"
    if "hi" in pref and "mix" in pref:
        return "hinglish"
    return "english"


def _signal_line(sig: str) -> str:
    if ":" in sig:
        k, v = sig.split(":", 1)
        return f"{humanize(k)} ({v})"
    return humanize(sig)


def resolve_digest_item(category: dict, trigger: dict) -> Optional[dict]:
    payload = trigger.get("payload", {}) or {}
    digest = category.get("digest", []) or []
    for key in ("top_item_id", "digest_item_id", "alert_id", "item_id"):
        iid = payload.get(key)
        if iid:
            for d in digest:
                if d.get("id") == iid:
                    return d
    top = payload.get("top_item")
    if isinstance(top, dict):
        return top
    return None


DIGEST_KIND_FOR_TRIGGER = {
    "research_digest": ["research", "trend", "tech"],
    "regulation_change": ["compliance"],
    "cde_opportunity": ["cde"],
    "supply_alert": ["alert", "supply"],
    "category_seasonal": ["seasonal"],
    "category_trend_movement": ["trend"],
    "ipl_match_today": ["seasonal"],
}


def pick_digest_for_kind(category: dict, kind: str) -> Optional[dict]:
    wanted = DIGEST_KIND_FOR_TRIGGER.get(kind)
    if not wanted:
        return None
    for w in wanted:
        for d in category.get("digest", []) or []:
            if d.get("kind") == w:
                return d
    return None


def best_catalog_offer(category: dict, prefer_words: tuple[str, ...] = ()) -> Optional[dict]:
    catalog = category.get("offer_catalog", []) or []
    for w in prefer_words:
        for o in catalog:
            if w.lower() in o.get("title", "").lower():
                return o
    for o in catalog:
        if o.get("type") == "service_at_price":
            return o
    return catalog[0] if catalog else None


def build_facts(category: dict, merchant: dict, trigger: dict,
                customer: Optional[dict] = None, now: Optional[date] = None) -> FactSheet:
    fs = FactSheet()
    now = now or REFERENCE_NOW
    cat_slug = category.get("slug") or merchant.get("category_slug", "")
    ident = merchant.get("identity", {}) or {}
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    payload = trigger.get("payload", {}) or {}
    kind = trigger.get("kind", "")

    # ---------------- merchant identity
    sal = salutation(cat_slug, merchant)
    fs.f.update(
        salutation=sal, merchant_name=ident.get("name", ""), locality=ident.get("locality", ""),
        city=ident.get("city", ""), owner=ident.get("owner_first_name", ""), category=cat_slug,
        kind=kind, language=merchant_language(merchant), now=now,
    )
    fs.add("MERCHANT", f"Business: {ident.get('name')} ({cat_slug}), {ident.get('locality')}, {ident.get('city')}")
    fs.add("MERCHANT", f"Address the owner as: {sal}")
    fs.add("MERCHANT", f"Google profile verified: {'yes' if ident.get('verified') else 'NO (unverified)'}")
    if ident.get("established_year"):
        fs.add("MERCHANT", f"Established {ident['established_year']}")

    sub = merchant.get("subscription", {}) or {}
    if sub:
        st = sub.get("status")
        if st == "active":
            fs.add("MERCHANT", f"magicpin plan: {sub.get('plan')} active, {sub.get('days_remaining')} days remaining")
        elif st == "expired":
            fs.add("MERCHANT", f"magicpin plan: EXPIRED {sub.get('days_since_expiry') or '?'} days ago")
        elif st == "trial":
            fs.add("MERCHANT", f"magicpin plan: trial, {sub.get('days_remaining')} days left")
        fs.f["subscription"] = sub

    # ---------------- performance vs peers
    wd = perf.get("window_days", 30)
    if perf:
        fs.add("PERFORMANCE", f"Last {wd} days: {fmt_num(perf.get('views', 0))} profile views, "
                              f"{fmt_num(perf.get('calls', 0))} calls, {fmt_num(perf.get('directions', 0))} direction requests"
               + (f", {fmt_num(perf['leads'])} leads" if perf.get("leads") is not None else ""))
        d7 = perf.get("delta_7d", {}) or {}
        parts = []
        for k, label in (("views_pct", "views"), ("calls_pct", "calls"), ("ctr_pct", "CTR")):
            if d7.get(k) is not None:
                parts.append(f"{label} {fmt_pct(d7[k], signed=True)}")
        if parts:
            fs.add("PERFORMANCE", "Last 7 days vs previous week: " + ", ".join(parts))
        fs.f["perf"] = perf
        fs.f["delta_7d"] = d7

    ctr, pctr = perf.get("ctr"), peer.get("avg_ctr")
    if ctr is not None and pctr:
        gap = (ctr - pctr) / pctr
        rel = "below" if gap < 0 else "above"
        fs.add("PEER_BENCHMARK", f"CTR {fmt_pct(ctr)} vs peer average {fmt_pct(pctr)} "
                                 f"({fmt_pct(gap)} {rel} peers) [peer scope: {humanize(peer.get('scope', 'metro'))}]")
        fs.f["ctr_gap"] = gap
    calls, pcalls = perf.get("calls"), peer.get("avg_calls_30d")
    if calls is not None and pcalls:
        gap = (calls - pcalls) / pcalls
        fs.add("PEER_BENCHMARK", f"Calls {fmt_num(calls)} vs peer average {fmt_num(pcalls)} "
                                 f"({fmt_pct(gap)} {'below' if gap < 0 else 'above'})")
        fs.f["calls_gap"] = gap
    views, pviews = perf.get("views"), peer.get("avg_views_30d")
    if views is not None and pviews:
        gap = (views - pviews) / pviews
        fs.add("PEER_BENCHMARK", f"Views {fmt_num(views)} vs peer average {fmt_num(pviews)} "
                                 f"({fmt_pct(gap)} {'below' if gap < 0 else 'above'})")
        fs.f["views_gap"] = gap
    for k, label in (("avg_rating", "peer avg rating"), ("avg_review_count", "peer avg review count"),
                     ("avg_photos", "peer avg photos on profile"),
                     ("avg_post_freq_days", "peers post on Google every N days, N=")):
        if peer.get(k) is not None:
            fs.add("PEER_BENCHMARK", f"{label} {peer[k]}")

    # ---------------- offers
    offers = merchant.get("offers", []) or []
    active = [o["title"] for o in offers if o.get("status") == "active" and o.get("title")]
    inactive = [f"{o['title']} ({o.get('status')})" for o in offers if o.get("status") != "active" and o.get("title")]
    fs.f["active_offers"] = active
    if active:
        fs.add("OFFERS", "Active offers: " + "; ".join(active))
    else:
        fs.add("OFFERS", "Merchant has NO active offers right now")
    if inactive:
        fs.add("OFFERS", "Past/inactive offers: " + "; ".join(inactive))
    fs.f["catalog"] = category.get("offer_catalog", []) or []
    catalog = [o.get("title") for o in fs.f["catalog"] if o.get("title")]
    if catalog:
        fs.add("CATEGORY_OFFER_CATALOG (proven formats you may SUGGEST, not claim they run)", "; ".join(catalog))

    # ---------------- customers aggregate, signals, reviews, history
    agg = merchant.get("customer_aggregate", {}) or {}
    for k, v in agg.items():
        if v is None:
            continue
        val = fmt_pct(v) if (k.endswith("_pct") and isinstance(v, (int, float)) and v <= 1) else fmt_num(v)
        fs.add("CUSTOMER_BASE", f"{humanize(k)}: {val}")
    fs.f["customer_aggregate"] = agg

    sigs = merchant.get("signals", []) or []
    for s in sigs:
        fs.add("SIGNALS", _signal_line(s))
    fs.f["signals"] = sigs

    themes = merchant.get("review_themes", []) or []
    for t in themes:
        line = f"{humanize(t.get('theme'))} ({t.get('sentiment')}, {t.get('occurrences_30d')} mentions in 30d)"
        if t.get("common_quote"):
            line += f' — quote: "{t["common_quote"]}"'
        fs.add("REVIEW_THEMES", line)
    fs.f["review_themes"] = themes

    hist = merchant.get("conversation_history", []) or []
    for h in hist[-4:]:
        fs.add("RECENT_CONVERSATION", f"{h.get('ts', '')[:10]} {h.get('from')}: \"{h.get('body')}\" [{h.get('engagement')}]")
    fs.f["history"] = hist

    # ---------------- category knowledge
    voice = category.get("voice", {}) or {}
    fs.f["voice"] = voice
    fs.f["peer"] = peer
    month = now.month
    beats = [b for b in category.get("seasonal_beats", []) or [] if month_in_range(b.get("month_range", ""), month)]
    for b in beats:
        fs.add("SEASONAL_NOW", f"{b.get('month_range')}: {b.get('note')}")
    fs.f["seasonal_now"] = beats
    fs.f["seasonal_all"] = category.get("seasonal_beats", []) or []
    upcoming = [b for b in category.get("seasonal_beats", []) or []
                if month_in_range(b.get("month_range", ""), (month % 12) + 1) and b not in beats]
    for b in upcoming:
        fs.add("SEASONAL_NEXT_MONTH", f"{b.get('month_range')}: {b.get('note')}")
    trends = category.get("trend_signals", []) or []
    for t in trends[:4]:
        fs.add("SEARCH_TRENDS", f"'{t.get('query')}' searches {fmt_pct(t.get('delta_yoy', 0), signed=True)} YoY"
                                + (f" (age {t.get('segment_age')})" if t.get("segment_age") else ""))
    fs.f["trends"] = trends

    item = resolve_digest_item(category, trigger) or pick_digest_for_kind(category, kind)
    fs.f["digest_item"] = item
    if item:
        fs.add("TRIGGER_DIGEST_ITEM", f"Title: {item.get('title')}")
        if item.get("source"):
            fs.add("TRIGGER_DIGEST_ITEM", f"Source (cite it): {item['source']}")
        for k in ("trial_n", "patient_segment", "date", "credits"):
            if item.get(k) is not None:
                fs.add("TRIGGER_DIGEST_ITEM", f"{humanize(k)}: {humanize(item[k])}")
        if item.get("summary"):
            fs.add("TRIGGER_DIGEST_ITEM", f"Summary: {item['summary']}")
        if item.get("actionable"):
            fs.add("TRIGGER_DIGEST_ITEM", f"Suggested action: {item['actionable']}")
    others = [d for d in category.get("digest", []) or [] if d is not item][:4]
    for d in others:
        fs.add("OTHER_DIGEST_THIS_WEEK", f"{d.get('title')} — {d.get('source')}")
    lib = category.get("patient_content_library", []) or []
    for c in lib[:3]:
        fs.add("SHAREABLE_CONTENT_LIBRARY", f"\"{c.get('title')}\" ({c.get('channel')})")
    fs.f["content_library"] = lib

    # ---------------- trigger
    is_placeholder = bool(payload.get("placeholder"))
    fs.f["placeholder"] = is_placeholder
    fs.f["payload"] = payload
    fs.add("TRIGGER", f"kind: {kind} ({trigger.get('source', '')}, urgency {trigger.get('urgency', '?')}/5)")
    if is_placeholder:
        fs.add("TRIGGER", "Trigger payload has NO details — anchor the why-now on the merchant/category facts above; "
                          "do NOT invent event specifics (no names, dates, amounts that are not listed)")
    else:
        for k, v in payload.items():
            if k in ("category",):
                continue
            fs.add("TRIGGER", f"{humanize(k)}: {_payload_value(k, v)}")
        if isinstance(payload.get("value_now"), (int, float)) and isinstance(payload.get("milestone_value"), (int, float)):
            gap = payload["milestone_value"] - payload["value_now"]
            if gap > 0:
                fs.add("TRIGGER", f"only {fmt_num(gap)} away from the milestone")
        if isinstance(payload.get("vs_baseline"), (int, float)) and isinstance(payload.get("delta_pct"), (int, float)):
            cur = round(payload["vs_baseline"] * (1 + payload["delta_pct"]))
            fs.add("TRIGGER", f"{humanize(payload.get('metric', 'metric'))}: about {fmt_num(cur)} this "
                              f"{payload.get('window', 'week')} vs usual {fmt_num(payload['vs_baseline'])}")
    if kind == "appointment_tomorrow":
        tmr = now + timedelta(days=1)
        fs.add("TRIGGER", f"appointment date (tomorrow): {tmr.strftime('%a')} {tmr.day} {tmr.strftime('%b')} {tmr.year}")
    exp = parse_date(trigger.get("expires_at"))
    if exp:
        fs.add("TRIGGER", f"relevant until {pretty_date(exp)}")

    # ---------------- customer
    if customer:
        cid = customer.get("identity", {}) or {}
        rel = customer.get("relationship", {}) or {}
        prefs = customer.get("preferences", {}) or {}
        consent = customer.get("consent", {}) or {}
        cname = str(cid.get("name", "")).strip()
        parent = None
        m = re.match(r"^(.*?)\s*\(parent:\s*(.*?)\)\s*$", cname)
        if m:
            cname, parent = m.group(1).strip(), m.group(2).strip()
        fs.f.update(customer_name=cname, customer_parent=parent,
                    customer_language=customer_language(customer), customer=customer)
        fs.add("CUSTOMER", f"Name: {cname}" + (f" (a child; message goes to parent {parent})" if parent else ""))
        fs.add("CUSTOMER", f"Language preference: {cid.get('language_pref')}")
        if cid.get("age_band"):
            fs.add("CUSTOMER", f"Age band: {cid['age_band']}" + (" (senior citizen)" if cid.get("senior_citizen") else ""))
        fs.add("CUSTOMER", f"State: {customer.get('state')}")
        if rel.get("visits_total") is not None:
            fs.add("CUSTOMER", f"Visits so far: {rel['visits_total']}; first {rel.get('first_visit')}, last {rel.get('last_visit')}")
        svcs = [humanize(s) for s in rel.get("services_received", []) or [] if s and s != "..."]
        if svcs:
            fs.add("CUSTOMER", "Services received: " + ", ".join(dict.fromkeys(svcs)))
        for k in ("favourite_dish", "chronic_conditions"):
            if rel.get(k):
                fs.add("CUSTOMER", f"{humanize(k)}: {humanize(rel[k]) if isinstance(rel[k], str) else ', '.join(map(humanize, rel[k]))}")
        pref_bits = [f"{humanize(k)}={humanize(v)}" for k, v in prefs.items() if v not in (None, "")]
        if pref_bits:
            fs.add("CUSTOMER", "Preferences: " + ", ".join(pref_bits))
        fs.add("CUSTOMER", "Consent scope: " + (", ".join(consent.get("scope", []) or []) or "NONE"))
        last = parse_date(rel.get("last_visit"))
        ref = _customer_reference_date(payload, now)
        if last and ref and ref > last and (ref - last).days >= 7:
            fs.f["days_since_visit"] = (ref - last).days
            fs.add("CUSTOMER", f"Days since last visit: {(ref - last).days} (about {round((ref - last).days / 7)} weeks)")
        if last and ref and ref > last:
            months = (ref.year - last.year) * 12 + (ref.month - last.month) - (1 if ref.day < last.day else 0)
            if months >= 1:
                fs.add("CUSTOMER", f"Months since last visit: {months}")
                fs.f["months_since_visit"] = months

        # explicit due-date on the trigger payload always outranks a computed "months since" guess
        due = parse_date(payload.get("due_date"))
        if due:
            fs.f["due_date"] = due
            fs.f["due_status"] = "overdue" if due < now else ("today" if due == now else "upcoming")
            fs.add("CUSTOMER", f"Due date on file: {pretty_date(due)} "
                               f"({'already past' if due < now else 'today' if due == now else 'upcoming, not yet due'}, "
                               f"relative to {pretty_date(now)})")
        last_service = parse_date(payload.get("last_service_date"))
        if last_service:
            fs.f["last_service_date"] = last_service
            fs.add("CUSTOMER", f"Last service on file: {pretty_date(last_service)}")

    return fs


def _customer_reference_date(payload: dict, now: date) -> date:
    slots = payload.get("available_slots") or payload.get("next_session_options") or []
    for s in slots:
        d = parse_date(s.get("iso") if isinstance(s, dict) else None)
        if d:
            return max(d, now)
    return now


def _payload_value(key: str, v: Any) -> str:
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return "; ".join(str(x.get("label") or x) for x in v)
        return ", ".join(humanize(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{humanize(k)}={humanize(x)}" for k, x in v.items())
    if isinstance(v, float) and key.endswith("_pct"):
        return fmt_pct(v, signed=True)
    if isinstance(v, (int, float)) and ("amount" in key or "price" in key):
        return fmt_inr(v)
    if isinstance(v, str):
        d = parse_date(v) if re.match(r"^\d{4}-\d{2}-\d{2}", v) else None
        if d:
            return pretty_date(d)
        return humanize(v)
    return str(v)
