"""Checks an outgoing message against the rules the judge penalises."""

from __future__ import annotations

import json
import re
from typing import Iterable, Optional

from .facts import FactSheet, NUM_RE, norm_number

URL_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|in|org|net|io|co)\b(/|\s|$))", re.I)
PREAMBLE_RE = re.compile(r"^\s*(i hope|hope you|greetings|dear sir|i am reaching out|i'm reaching out|hello,? i am vera)", re.I)


def _taboos(fs: FactSheet) -> list[str]:
    out = []
    for t in (fs.f.get("voice") or {}).get("vocab_taboo", []) or []:
        t = re.sub(r"\(.*?\)", "", str(t)).strip().lower()
        if t:
            out.append(t)
    return out


def _number_ok(tok: str, allowed: set[str]) -> bool:
    n = norm_number(tok)
    if n is None or n in allowed:
        return True
    try:
        v = float(n)
    except ValueError:
        return True
    for a in allowed:
        try:
            av = float(a)
        except ValueError:
            continue
        if abs(av - v) <= 0.051 or round(av) == v or abs(av * 100 - v) <= 0.51:
            return True
    return False


TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", re.I)


def _allowed_times(fs: FactSheet) -> set[str]:
    """Clock times the facts actually contain: slot labels in the rendered sheet + ISO timestamps in the payload."""
    out = {f"{h}{m or ''}{ap}".lower() for h, m, ap in ((a, (":" + b) if b else "", c) for a, b, c in TIME_RE.findall(fs.render()))}
    for hh, mm in re.findall(r"T(\d{2}):(\d{2})", json.dumps(fs.f.get("payload") or {}) + fs.render()):
        h = int(hh)
        h12, ap = (h % 12 or 12), ("am" if h < 12 else "pm")
        out.add(f"{h12}:{mm}{ap}")
        if mm == "00":
            out.add(f"{h12}{ap}")
    return out


INTERNAL_RE = re.compile(r"\b(views?|ctr|peer|peers|magicpin|vera|leads|click[- ]?through|neighbourhood average)\b", re.I)


def validate(body: str, fs: FactSheet, previous: Iterable[str] = (), cta: Optional[str] = None,
             customer_facing: bool = False) -> list[str]:
    errors: list[str] = []
    if not body or not body.strip():
        return ["empty body"]
    low = body.lower()
    if customer_facing and INTERNAL_RE.search(body):
        errors.append("customer-facing message mentions business internals (views/CTR/peers/magicpin) — remove them")
    if URL_RE.search(body):
        errors.append("contains a URL/domain — remove it")
    for t in _taboos(fs):
        if t in low:
            errors.append(f"uses taboo phrase '{t}'")
    allowed = fs.allowed_numbers
    bad = sorted({m for m in NUM_RE.findall(body) if not _number_ok(m, allowed)})
    if bad:
        errors.append("numbers not supported by the facts (fabrication risk): " + ", ".join(bad))
    times = _allowed_times(fs)
    bad_times = sorted({f"{h}{(':' + m) if m else ''}{ap}".lower() for h, m, ap in TIME_RE.findall(body)} - times)
    if bad_times:
        errors.append("clock times not in the facts (e.g. invented opening hours): " + ", ".join(bad_times))
    if PREAMBLE_RE.search(body):
        errors.append("starts with a preamble — open with the name and the reason")
    if len(body) > 750:
        errors.append(f"too long ({len(body)} chars) — keep under 600")
    if body.count("?") > 1:
        errors.append("more than one question — keep exactly one ask, as the last sentence")
    norm = re.sub(r"\s+", " ", low).strip()
    for p in previous:
        if norm == re.sub(r"\s+", " ", (p or "").lower()).strip():
            errors.append("identical to a message already sent — rephrase")
            break
    if fs.f.get("placeholder") and fs.f.get("kind") == "competitor_opened":
        # no competitor was named in the data; any capitalised "X opened" claim is fabricated
        if re.search(r"\b[A-Z][\w']+(?: [A-Z][\w']+)* (?:has )?(?:opened|launched)\b", body):
            errors.append("names a competitor that is not in the data")
    return errors
