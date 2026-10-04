"""A sample program for `nd demo` and `nd bench`.

Small, but it has the three things worth looking at: a loop that mutates a
list in place, a function that calls itself, and a dictionary that grows.
"""


def insertion_sort(xs):
    for i in range(1, len(xs)):
        key = xs[i]
        j = i - 1
        while j >= 0 and xs[j] > key:
            xs[j + 1] = xs[j]
            j -= 1
        xs[j + 1] = key
    return xs


def fib(n, seen):
    if n < 2:
        return n
    if n in seen:
        return seen[n]
    seen[n] = fib(n - 1, seen) + fib(n - 2, seen)
    return seen[n]


def main():
    data = [37, 4, 91, 12, 58, 3, 77, 25, 60, 8, 44, 19]
    insertion_sort(data)
    seen = {}
    total = fib(22, seen)
    buckets = {}
    for v in data:
        buckets.setdefault(v % 5, []).append(v)
    print("sorted:", data)
    print("fib(22):", total)
    print("buckets:", buckets)


main()
