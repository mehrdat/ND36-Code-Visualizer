"""Insertion sort -- the classic 'watch the array change' case."""

import random

random.seed(7)
data = [random.randint(0, 99) for _ in range(12)]

n = len(data)
for i in range(1, n):
    key = data[i]
    j = i - 1
    while j >= 0 and data[j] > key:
        data[j + 1] = data[j]
        j -= 1
    data[j + 1] = key

total = sum(data)
print("sorted:", data)
print("total:", total)
