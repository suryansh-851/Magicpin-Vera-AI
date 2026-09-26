"""Push the expanded dataset (categories, merchants, customers, triggers) into a running bot.

Usage: python scripts/load_dataset.py [base_url]     (default http://127.0.0.1:8080)
"""

import json
import sys
from pathlib import Path
from urllib import error, request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
EXP = Path(__file__).resolve().parent.parent / "dataset" / "expanded"


def push(scope: str, cid: str, payload: dict) -> int:
    body = {"scope": scope, "context_id": cid, "version": 1, "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"}
    req = request.Request(f"{BASE}/v1/context", data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=10) as r:
            return r.status
    except error.HTTPError as e:
        return e.code


for scope, sub, key in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                        ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
    codes = [push(scope, d[key], d) for d in (json.loads(f.read_text(encoding="utf-8")) for f in (EXP / sub).glob("*.json"))]
    print(f"{scope:9} pushed {len(codes):3}  (200 new: {codes.count(200)}, 409 already loaded: {codes.count(409)})")

with request.urlopen(f"{BASE}/v1/healthz", timeout=10) as r:
    print("healthz:", json.loads(r.read()))
