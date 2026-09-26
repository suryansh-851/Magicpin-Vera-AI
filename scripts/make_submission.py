"""Generate submission.jsonl for the 30 canonical test pairs.

Usage (from repo root):
    PYTHONUTF8=1 python dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded
    PYTHONUTF8=1 python scripts/make_submission.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402

EXP = ROOT / "dataset" / "expanded"


def load_dir(sub: str, key: str) -> dict:
    out = {}
    for f in (EXP / sub).glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        out[d[key]] = d
    return out


def main() -> None:
    if not (EXP / "test_pairs.json").exists():
        sys.exit("Run dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded first")
    categories = load_dir("categories", "slug")
    merchants = load_dir("merchants", "merchant_id")
    customers = load_dir("customers", "customer_id")
    triggers = load_dir("triggers", "id")
    pairs = json.loads((EXP / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]

    lines = []
    for p in pairs:
        trg = triggers[p["trigger_id"]]
        m = merchants[p["merchant_id"]]
        cat = categories[m["category_slug"]]
        cust = customers.get(p.get("customer_id")) if p.get("customer_id") else None
        out = compose(cat, m, trg, cust)
        lines.append({"test_id": p["test_id"], **out})
        print(f"{p['test_id']} [{trg['kind']}] {m['merchant_id']}\n  {out['body']}\n")

    with open(ROOT / "submission.jsonl", "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    print(f"wrote {len(lines)} lines to submission.jsonl")


if __name__ == "__main__":
    main()
