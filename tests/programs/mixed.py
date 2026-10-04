"""Functions, recursion, classes, exceptions, dicts -- the correctness case."""


class Counter:
    def __init__(self, start):
        self.value = start
        self.history = []

    def bump(self, by):
        self.value += by
        self.history.append(self.value)
        return self.value


def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def risky(x):
    try:
        return 10 // x
    except ZeroDivisionError:
        return None


c = Counter(5)
for step in range(4):
    c.bump(step + 1)

results = {}
for k in range(6):
    results[k] = fib(k)

safe = [risky(v) for v in (2, 0, 5)]
label = "done"
print(c.value, results, safe, label)
