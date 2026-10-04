
puzzle=[
    [5,3,0,0,7,0,0,0,0],
    [6,0,0,1,9,5,0,0,0],
    [0,9,8,0,0,0,0,6,0],
    [8,0,0,0,6,0,0,0,3],
    [4,0,0,8,0,3,0,0,1],
    [7,0,0,0,2,0,0,0,6],
    [0,6,0,0,0,0,2,8,0],
    [0,0,0,4,1,9,0,0,5],
    [0,0,0,0,8,0,0,7,9]
]


def find_empty(puzzle):
    for x in range(len(puzzle)):
        for y in range(len(puzzle)):
            if puzzle[x][y]==0:
                return x,y
    return None

def is_valid(puzzle,n,x,y):
    for i in range(len(puzzle)):
        if puzzle[x][i]==n or puzzle[i][y]==n:
                    return False
    x0 = (x // 3) * 3
    y0 = (y // 3) * 3
    for i in range(x0,x0+3):
        for j in range(y0,y0+3):
            if puzzle[i][j] == n:
                return False
    return True

def solve(puzzle):
    find = find_empty(puzzle)
    if not find:
        return True
    else:
        x,y = find
        for n in range(1,10):
            if is_valid(puzzle,n,x,y):
                puzzle[x][y] = n
                if solve(puzzle):
                    return True
                puzzle[x][y] = 0

    return False

def main():
    print("Sudoku Puzzle:")
    for row in puzzle:
        print(*row)
    if solve(puzzle):
        print("\nSolved Sudoku Puzzle:")
        for row in puzzle:
            print(*row)
    else:
        print("No solution exists.")

main()
