"""The four shapes of in-place change, plus one branch that is never taken."""


class Box:
    def __init__(self):
        self.total = 0

    def add(self, v):
        self.total += v          # attribute mutation


def record(xs, v):
    xs.append(v)                 # method mutation of the caller's list


def never_called(x):
    return x * 2             # DEAD


big = list(range(200))
big[7] = 999                     # subscript assign on a big list

box = Box()
box.add(5)
box.add(7)

log = []
record(log, "a")
record(log, "b")

counts = {}
counts["x"] = 1
counts["y"] = 2

if big[7] < 0:
    unreachable = True       # DEAD

print(big[7], box.total, log, counts)
