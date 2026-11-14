#!/usr/bin/env python3
"""Markdown tables from bench-matrix.sh CSVs.

usage: tables.py LABEL=FILE[:label] ...

Each argument names a column: rows are tests, cells are MiB/s for bulk
tests and RPC/s for ping and 4K tests.
"""

import csv
import sys

cols = []
for arg in sys.argv[1:]:
    name, spec = arg.split("=", 1)
    path, _, want = spec.partition(":")
    rows = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            lab, _, tr = want.partition("/")
            if lab and r["label"] != lab:
                continue
            if tr and r["transport"] != tr:
                continue
            key = (r["test"], r["size"], int(r["conc"]))
            rows[key] = r
    cols.append((name, rows))

order = []
for _, rows in cols:
    for k in rows:
        if k not in order:
            order.append(k)
rank = {"write": 0, "read": 1, "ping": 2}
order.sort(key=lambda k: (k[1] != "1M", rank.get(k[0], 3), k[1], k[2]))


def cell(r, k):
    if r is None:
        return "-"
    if k[1] == "1M":
        v = float(r["MiB_s"])
        return f"{v / 1024:.1f} GiB/s" if v >= 1024 else f"{v:.0f} MiB/s"
    return f"{int(float(r['RPC_s'] or 0)):,} RPC/s"


def test_name(k):
    t, size, conc = k
    if t == "ping":
        return f"ping, {conc} in flight"
    return f"{t} {size}, {conc} in flight"


print("| test | " + " | ".join(n for n, _ in cols) + " |")
print("|---|" + "---|" * len(cols))
for k in order:
    print(
        f"| {test_name(k)} | "
        + " | ".join(cell(rows.get(k), k) for _, rows in cols)
        + " |"
    )
