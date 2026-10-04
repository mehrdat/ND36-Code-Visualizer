"""A small ETL pipeline with one planted bug.

`orders.region` has trailing whitespace in the raw data. The filter on line
with `== "north"` therefore matches almost nothing, and 90% of the rows
quietly disappear. The run still succeeds and still prints a number.

This is the shape of bug the DataFrame ribbon exists to catch.
"""

import numpy as np
import pandas as pd

rng = np.random.default_rng(7)
N = 4000

raw = pd.DataFrame({
    "order_id": np.arange(N),
    # The trailing space is the bug's seed.
    "region": rng.choice(["north ", "south", "east", "west"], N),
    "amount": rng.uniform(1, 500, N).round(2),
    "qty": rng.integers(1, 9, N),
})
raw.loc[rng.choice(N, 180, replace=False), "amount"] = np.nan

clean = raw.dropna(subset=["amount"])
clean = clean.assign(total=clean["amount"] * clean["qty"])
clean["qty"] = clean["qty"].astype("float64")

# THE BUG: the data says "north ", this says "north".
north = clean[clean["region"] == "north"]

summary = north.groupby("region", as_index=False)["total"].sum()
print("rows kept:", len(north), "of", len(clean))
print(summary.to_string(index=False))
