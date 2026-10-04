"""A cache that is never cleared: the classic leak, next to code that is fine."""

CACHE = {}


def cached(n):
    """Keeps every result forever. This is the leak."""
    key = "k%d" % n
    if key not in CACHE:
        CACHE[key] = [n] * 500      # LEAK
    return CACHE[key][0]


def tidy(n):
    """Allocates just as much, and gives it all back."""
    scratch = [n] * 500
    return scratch[0]


total = 0
for i in range(4000):
    total += cached(i % 1500)
    total += tidy(i)

print(total, len(CACHE))
