"""A sample program with a real bug in it, for practising.

It is meant to compute the average of each group. It gets one of them
wrong, and nothing crashes -- which is the hard kind of bug. Record it and
find the wrong line:

    nd run bug_demo.py
    nd watch bug_demo.nd total
"""

ROWS = [
    ("a", 10), ("a", 20), ("a", 30),
    ("b", 5), ("b", 15),
    ("c", 100),
]


def averages(rows):
    sums = {}
    counts = {}
    for key, value in rows:
        if key not in sums:
            sums[key] = 0
            counts[key] = 0
        sums[key] += value
        counts[key] = counts[key] + 1

    out = {}
    total = 0
    for key in sums:
        total = total + sums[key]
        # The bug: `total` is used where `sums[key]` was meant, so every
        # group after the first is averaged against a running total.
        out[key] = total / counts[key]
    return out, total


result, total = averages(ROWS)
for name in sorted(result):
    print("%s -> %.2f" % (name, result[name]))
print("total", total)
