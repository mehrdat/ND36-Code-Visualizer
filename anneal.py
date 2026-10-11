"""Simulated annealing on a 12-city tour.

It prints a best cost, and then the cost of the tour it hands back.
The two numbers disagree. Nothing crashes, nothing warns you, and no
print statement will tell you why.

    nd run anneal.py
    nd x anneal.nd
"""
import math
import random

random.seed(11)

CITIES = [(random.randint(0, 100), random.randint(0, 100)) for _ in range(12)]


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def tour_cost(tour):
    total = 0.0
    for i in range(len(tour)):
        total += distance(CITIES[tour[i]], CITIES[tour[i - 1]])
    return total


def swap(tour, i, j):
    tour[i], tour[j] = tour[j], tour[i]


def anneal(tour, rounds):
    temp = 50.0
    cur_cost = tour_cost(tour)
    best_tour = tour
    best_cost = cur_cost
    accepted = 0
    rejected = 0

    for step in range(rounds):
        i = random.randrange(len(tour))
        j = random.randrange(len(tour))
        if i != j:
            swap(tour, i, j)
            new_cost = tour_cost(tour)
            delta = new_cost - cur_cost

            if delta < 0 or random.random() < math.exp(-delta / temp):
                cur_cost = new_cost
                accepted += 1
                if new_cost < best_cost:
                    best_cost = new_cost
                    best_tour = tour
            else:
                swap(tour, i, j)
                rejected += 1

        temp *= 0.999
        if temp < 0.001:
            temp = 0.001

    return best_tour, best_cost, accepted, rejected


tour, cost, ok, no = anneal(list(range(12)), 3000)
print("best cost reported :", round(cost, 2))
print("cost of that tour  :", round(tour_cost(tour), 2))
print("accepted / rejected:", ok, "/", no)
