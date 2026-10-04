/* A linked list: the classic pointer-diagram program. */
#include <stdio.h>
#include <stdlib.h>

typedef struct Node { int value; struct Node *next; } Node;

Node *push(Node *head, int value) {
    Node *n = malloc(sizeof(Node));
    n->value = value;
    n->next = head;
    return n;
}

int total(Node *head) {
    int sum = 0;
    for (Node *p = head; p != NULL; p = p->next) { sum += p->value; }
    return sum;
}

int main(void) {
    Node *head = NULL;
    for (int i = 1; i <= 4; i++) { head = push(head, i * 10); }
    int sum = total(head);
    printf("sum=%d\n", sum);
    return 0;
}
