// Watching a std::vector grow, and its buffer move when it reallocates.
#include <cstdio>
#include <vector>

struct Point { int x; int y; };

int main() {
    std::vector<int> xs;
    for (int i = 1; i <= 6; i++) {
        xs.push_back(i * i);
    }
    Point p{3, 4};
    int area = p.x * p.y;
    int sum = 0;
    for (size_t k = 0; k < xs.size(); k++) { sum += xs[k]; }
    std::printf("%d %d\n", sum, area);
    return 0;
}
