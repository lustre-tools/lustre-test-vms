"""The NUMA distance table (SLIT) of a VM with simulated NUMA nodes.

QEMU's default gives 10 to a node's own memory and 20 to every other
node.  ``--numa-dist`` gives a real one, for example a customer's
multi-socket table, so the guest's ``numactl -H`` shows it.

A table is a list of rows, row *i* the distances from node *i*.  It may
be asymmetric, as real firmware tables are.  The ``.info`` file stores
it as ``format_matrix`` writes it: rows split by ';', values by ','.
"""

from __future__ import annotations

import re

LOCAL_DISTANCE = 10
# QEMU stores a distance in a byte.  Off the diagonal it must also be
# more than LOCAL_DISTANCE: QEMU takes 10, but the guest kernel then
# drops the whole table and falls back to 10/20 (arm64 of_numa, x86
# SLIT validation).
MAX_DISTANCE = 255

Matrix = list[list[int]]


class NumaDistError(ValueError):
    """A distance table that does not parse or does not fit the VM."""


def parse_matrix(text: str) -> Matrix:
    """Parse ``10,15;15,10``: rows split by ';', values by ',' or space."""
    if not text.strip():
        raise NumaDistError("--numa-dist is empty")
    matrix = []
    for n, row in enumerate(text.strip().split(";")):
        values = [v for v in re.split(r"[,\s]+", row.strip()) if v]
        if not values:
            raise NumaDistError(f"--numa-dist: row {n} is empty")
        matrix.append([_int(v) for v in values])
    return matrix


def parse_numactl(text: str) -> Matrix:
    """The ``node distances:`` block of ``numactl -H``, from anywhere in *text*.

    The whole ``numactl -H`` output pastes in, or only the block.  Node
    numbers must run 0..N-1; a host with memoryless or offline nodes
    prints gaps, which a VM cannot reproduce.
    """
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("node distances"):
            start = i + 1
            break
    header: list[int] | None = None
    matrix: Matrix = []
    for line in lines[start:]:
        words = line.split()
        if not words:
            if header is not None and matrix:
                break
            continue
        if words[0] == "node" and header is None:
            header = [_int(w) for w in words[1:]]
            continue
        if header is None:
            continue
        m = re.match(r"^\s*(\d+):\s*(.*)$", line)
        if not m:
            break
        if int(m.group(1)) != len(matrix):
            raise NumaDistError(
                f"numactl table: row for node {m.group(1)} where node "
                f"{len(matrix)} was expected"
            )
        matrix.append([_int(v) for v in m.group(2).split()])
    if header is None or not matrix:
        raise NumaDistError(
            "no 'node distances:' table found (expected the output of "
            "`numactl -H`)"
        )
    if header != list(range(len(header))):
        raise NumaDistError(
            f"numactl table: nodes {header} are not numbered 0..N-1"
        )
    return matrix


def problem(matrix: Matrix, nodes: int) -> str | None:
    """Why *matrix* cannot be the table of a VM with *nodes* nodes."""
    if nodes < 2:
        return f"--numa-dist needs --numa-nodes 2 or more (the VM has {nodes})"
    if len(matrix) != nodes or any(len(r) != nodes for r in matrix):
        shape = f"{len(matrix)} rows of " + "/".join(
            sorted({str(len(r)) for r in matrix})
        )
        return (
            f"--numa-dist must be {nodes}x{nodes} for {nodes} NUMA nodes "
            f"(got {shape})"
        )
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if i == j and v != LOCAL_DISTANCE:
                return (
                    f"--numa-dist: node {i} to itself is {v}; it must be "
                    f"{LOCAL_DISTANCE}"
                )
            if i != j and not LOCAL_DISTANCE < v <= MAX_DISTANCE:
                return (
                    f"--numa-dist: node {i} to node {j} is {v}; it must be "
                    f"{LOCAL_DISTANCE + 1}-{MAX_DISTANCE}"
                )
    return None


def resolve(inline: str | None, path: str | None, nodes: int | None) -> str:
    """The stored spelling of --numa-dist or --numa-dist-file.

    Empty when neither is given.  Raises NumaDistError for a table that
    does not parse or does not fit *nodes*; None skips the fit check.
    """
    if inline is not None and path is not None:
        raise NumaDistError("give --numa-dist or --numa-dist-file, not both")
    if path is not None:
        try:
            text = open(path).read()
        except OSError as e:
            raise NumaDistError(f"--numa-dist-file {path}: {e}") from e
        matrix = parse_numactl(text)
    elif inline is not None:
        matrix = parse_matrix(inline)
    else:
        return ""
    why = None if nodes is None else problem(matrix, nodes)
    if why:
        raise NumaDistError(why)
    return format_matrix(matrix)


def format_matrix(matrix: Matrix) -> str:
    """The stored spelling, which parse_matrix reads back."""
    return ";".join(",".join(str(v) for v in row) for row in matrix)


def qemu_args(matrix: Matrix) -> list[str]:
    """One ``-numa dist`` per off-diagonal entry.

    Every pair, both directions: QEMU fills in only the reverse of a
    pair it is not given, and that is wrong for an asymmetric table.
    """
    args = []
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if i != j:
                args += ["-numa", f"dist,src={i},dst={j},val={v}"]
    return args


def _int(value: str) -> int:
    if not value.isdigit():
        raise NumaDistError(f"NUMA distance {value!r} is not a number")
    return int(value)
