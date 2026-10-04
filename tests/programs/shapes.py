"""A tiny network with one wrong number, which fails the way they always do.

The conv stack ends up at 32 channels of 7x7, so the flattened features are
32*7*7 = 1568. The linear layer below says 800. PyTorch's message names the
two matrices and nothing else -- not the variable, not the line, and not
what the shape was three operations ago.
"""

import torch
import torch.nn as nn


class Net(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, 3)
        self.conv2 = nn.Conv2d(16, 32, 3)
        self.pool = nn.MaxPool2d(2)
        self.fc = nn.Linear(800, 10)       # WRONG: should be 1568

    def forward(self, x):
        h = torch.relu(self.conv1(x))
        h = self.pool(h)
        h = torch.relu(self.conv2(h))
        flat = h.flatten(1)
        return self.fc(flat)


torch.manual_seed(0)
model = Net()
batch = torch.randn(8, 1, 20, 20)
logits = model(batch)
print(logits.shape)
