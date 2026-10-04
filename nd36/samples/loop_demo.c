/* A sample C program for `nd demo --c`.
 * A growing array and a function that is called in a loop -- enough to see
 * what the native backend can do. */
#include <stdio.h>
#include <stdlib.h>

static int sum_to(int n) {
    int total = 0;
    for (int i = 1; i <= n; i++) {
        total += i;
    }
    return total;
}

int main(void) {
    int cap = 2;
    int n = 0;
    int *xs = malloc(cap * sizeof(int));

    for (int i = 0; i < 9; i++) {
        if (n == cap) {
            cap *= 2;
            xs = realloc(xs, cap * sizeof(int));
        }
        xs[n++] = sum_to(i);
    }

    for (int i = 0; i < n; i++) {
        printf("%d ", xs[i]);
    }
    printf("\ncap=%d n=%d\n", cap, n);
    free(xs);
    return 0;
}
