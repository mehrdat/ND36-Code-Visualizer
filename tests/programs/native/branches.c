/* One branch is never taken, and one function is never called. */
#include <stdio.h>

int classify(int n) {
    if (n < 0) {
        return -1;              /* DEAD */
    } else if (n == 0) {
        return 0;
    }
    return 1;
}

int never_called(int x) {
    return x * 2;               /* DEAD */
}

int main(void) {
    int hits = 0;
    int values[4] = {3, 0, 7, 0};
    for (int i = 0; i < 4; i++) {
        hits += classify(values[i]);
    }
    printf("%d\n", hits);
    return 0;
}
