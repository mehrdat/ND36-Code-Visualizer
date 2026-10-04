"""Realistic work: parse, group, aggregate. The shape of actual analysis code.

`heavy.py` is the worst case for a tracer (a tight arithmetic loop, where
every line is pure overhead). This is closer to real code, so its overhead
number is the honest one.
"""

import random

random.seed(3)

REGIONS = ["north", "south", "east", "west"]
rows = []
for i in range(3000):
    rows.append({
        "id": i,
        "region": random.choice(REGIONS),
        "amount": round(random.uniform(1, 500), 2),
        "label": "order-%04d" % i,
    })


def clean(records):
    out = []
    for rec in records:
        if rec["amount"] < 5:
            continue
        rec = dict(rec)
        rec["label"] = rec["label"].upper().replace("-", "_")
        out.append(rec)
    return out


def group_totals(records):
    totals = {}
    counts = {}
    for rec in records:
        key = rec["region"]
        totals[key] = totals.get(key, 0.0) + rec["amount"]
        counts[key] = counts.get(key, 0) + 1
    return {k: (round(v, 2), counts[k]) for k, v in totals.items()}


cleaned = clean(rows)
summary = group_totals(cleaned)
biggest = max(summary.items(), key=lambda kv: kv[1][0])

print("kept %d of %d rows" % (len(cleaned), len(rows)))
for region, (total, n) in sorted(summary.items()):
    print("  %-6s %10.2f  (%d orders)" % (region, total, n))
print("biggest:", biggest[0])
