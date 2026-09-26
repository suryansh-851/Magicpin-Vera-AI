"""Score submission.jsonl with the exact rubric/prompt from magicpin's judge_simulator.py (LLMScorer).

Uses the Groq key from .env. Free tier is ~8K tokens/min, so this waits out rate limits
(~15 min for 30 messages). Results are cached in .judge_cache.json, so re-runs only score changed bodies.

Usage: python scripts/self_judge.py [T01 T05 ...]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import vera  # noqa: E402,F401  (loads .env)
import judge_simulator as js  # noqa: E402

opener = request.build_opener()
opener.addheaders = [("User-Agent", "magicpin-self-judge/1.0")]  # Groq's Cloudflare blocks urllib's default UA
request.install_opener(opener)

EXP = ROOT / "dataset" / "expanded"
CACHE = ROOT / ".judge_cache.json"


def load(sub: str, key: str) -> dict:
    return {d[key]: d for d in (json.loads(f.read_text(encoding="utf-8")) for f in (EXP / sub).glob("*.json"))}


def main() -> None:
    only = set(sys.argv[1:])
    cats, ms, cs, ts = load("categories", "slug"), load("merchants", "merchant_id"), load("customers", "customer_id"), load("triggers", "id")
    pairs = {p["test_id"]: p for p in json.loads((EXP / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]}
    subs = [json.loads(l) for l in (ROOT / "submission.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    provider = js.GroqProvider(os.environ["LLM_API_KEY"], os.getenv("JUDGE_MODEL", "openai/gpt-oss-120b"))
    scorer = js.LLMScorer(provider, None)
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    totals = []
    for s in subs:
        if only and s["test_id"] not in only:
            continue
        p = pairs[s["test_id"]]
        trg, m = ts[p["trigger_id"]], ms[p["merchant_id"]]
        cat, cust = cats[m["category_slug"]], cs.get(p.get("customer_id")) if p.get("customer_id") else None
        key = hashlib.sha256((s["body"] + s["cta"] + s["send_as"]).encode()).hexdigest()
        if key not in cache:
            for attempt in range(8):
                try:
                    raw = provider.complete(scorer_prompt(scorer, s, cat, m, trg, cust), js.LLMScorer.SYSTEM)
                    break
                except error.HTTPError as e:
                    if e.code != 429 and e.code < 500:
                        raise
                    time.sleep(15)  # rate limit or provider outage: wait and retry
                except (error.URLError, TimeoutError, OSError):
                    time.sleep(15)
            else:
                print(f"{s['test_id']}: gave up (rate limit)")
                continue
            r = scorer._parse_response(raw, s)
            cache[key] = {k: getattr(r, k) for k in ("specificity", "category_fit", "merchant_fit", "decision_quality",
                                                     "engagement_compulsion", "hint", "specificity_reason",
                                                     "category_fit_reason", "merchant_fit_reason",
                                                     "decision_quality_reason", "engagement_reason")}
            CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
        r = cache[key]
        dims = [r["specificity"], r["category_fit"], r["merchant_fit"], r["decision_quality"], r["engagement_compulsion"]]
        totals.append(sum(dims))
        print(f"{s['test_id']} {trg['kind']:<26} {sum(dims):>2}/50  S{dims[0]} C{dims[1]} M{dims[2]} T{dims[3]} E{dims[4]}  | {r['hint']}")
        for label, k in (("S", "specificity_reason"), ("C", "category_fit_reason"), ("M", "merchant_fit_reason"),
                         ("T", "decision_quality_reason"), ("E", "engagement_reason")):
            if r[k.replace("_reason", "") if k != "engagement_reason" else "engagement_compulsion"] < 9:
                print(f"     {label}: {r[k]}")
    if totals:
        print(f"\nAVERAGE {sum(totals) / len(totals):.1f}/50 over {len(totals)} messages "
              f"(min {min(totals)}, max {max(totals)})")


def scorer_prompt(scorer: js.LLMScorer, action, category, merchant, trigger, customer) -> str:
    """Rebuild the exact user prompt LLMScorer.score() sends, without its console output."""
    captured = {}

    class Capture:
        def complete(self, prompt, system=None):
            captured["p"] = prompt
            raise RuntimeError("captured")

        def name(self):
            return "capture"

    real = scorer.llm
    scorer.llm = Capture()
    try:
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            scorer.score(action, category, merchant, trigger, customer)
    finally:
        scorer.llm = real
    return captured["p"]


if __name__ == "__main__":
    main()
