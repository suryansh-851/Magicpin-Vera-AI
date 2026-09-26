"""Compose for every trigger in the expanded dataset and validate the output.

Usage: PYTHONUTF8=1 python scripts/smoke_test.py [--show]
Fails (exit 1) on: empty body, URL, taboo word, unsourced number, duplicate body,
wrong send_as for customer triggers.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vera.composer import compose  # noqa: E402
from vera.facts import build_facts  # noqa: E402
from vera.validator import validate  # noqa: E402

EXP = ROOT / "dataset" / "expanded"


def load_dir(sub: str, key: str) -> dict:
    return {d[key]: d for d in (json.loads(f.read_text(encoding="utf-8")) for f in (EXP / sub).glob("*.json"))}


def main() -> None:
    show = "--show" in sys.argv
    cats, ms, cs, ts = (load_dir("categories", "slug"), load_dir("merchants", "merchant_id"),
                        load_dir("customers", "customer_id"), load_dir("triggers", "id"))
    problems, bodies, sources = 0, {}, {}
    t0 = time.time()
    for tid, trg in sorted(ts.items()):
        m = ms[trg["merchant_id"]]
        cat = cats[m["category_slug"]]
        cust = cs.get(trg.get("customer_id")) if trg.get("customer_id") else None
        out = compose(cat, m, trg, cust)
        fs = build_facts(cat, m, trg, cust if trg.get("scope") == "customer" else None)
        errs = validate(out["body"], fs, customer_facing=trg.get("scope") == "customer")
        if trg.get("scope") == "customer" and out["send_as"] != "merchant_on_behalf":
            errs.append("customer trigger not sent as merchant_on_behalf")
        # identical inputs (generator emits duplicate placeholder triggers) may give identical output;
        # the server suppresses those. Different inputs must never collapse to the same text.
        sig = (trg["merchant_id"], trg.get("customer_id"), trg["kind"], json.dumps(trg.get("payload"), sort_keys=True))
        if out["body"] in bodies and bodies[out["body"]][1] != sig:
            errs.append(f"duplicate body of {bodies[out['body']][0]}")
        bodies[out["body"]] = (tid, sig)
        sources[out["_source"]] = sources.get(out["_source"], 0) + 1
        if errs or show:
            print(f"{'FAIL' if errs else 'ok  '} {tid} [{trg['kind']}] ({len(out['body'])} chars, {out['_source']})")
            print(f"     {out['body']}")
            for e in errs:
                print(f"     !! {e}")
        problems += bool(errs)
    print(f"\n{len(ts)} triggers, {problems} with problems, sources={sources}, {time.time() - t0:.1f}s")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
