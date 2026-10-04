"""A loop big enough to prove the trace scales past Python Tutor's 1,000 steps."""

total = 0
best = 0
for i in range(40000):
    v = (i * 7919) % 1000
    total += v
    if v > best:
        best = v
print(total, best)
