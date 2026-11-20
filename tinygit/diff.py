"""Myers' O(ND) diff and unified-format output.

This is the greedy forward algorithm from "An O(ND) Difference Algorithm and
Its Variations" (Myers, 1986), keeping one V array per edit distance so we
can walk the path back afterwards. Memory is O(D^2) instead of the
linear-space refinement, which is fine for source files.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Edit:
    op: str        # " ", "-", "+"
    a_line: int    # 0-based index into a (or -1)
    b_line: int    # 0-based index into b (or -1)
    text: str


def myers(a: Sequence[str], b: Sequence[str]) -> list[Edit]:
    n, m = len(a), len(b)
    vmax = n + m
    v = {1: 0}
    trace = []
    for d in range(vmax + 1):
        trace.append(dict(v))
        for k in range(-d, d + 1, 2):
            # step down (insert) unless we're on the edge or the left
            # diagonal got further
            if k == -d or (k != d and v[k - 1] < v[k + 1]):
                x = v[k + 1]
            else:
                x = v[k - 1] + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x, y = x + 1, y + 1  # the "snake"
            v[k] = x
            if x >= n and y >= m:
                return _backtrack(trace, a, b)
    raise AssertionError("unreachable")


def _backtrack(trace, a, b) -> list[Edit]:
    x, y = len(a), len(b)
    edits: list[Edit] = []
    for d in range(len(trace) - 1, -1, -1):
        v = trace[d]
        k = x - y
        if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v[prev_k]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            x, y = x - 1, y - 1
            edits.append(Edit(" ", x, y, a[x]))
        if d > 0:
            if x == prev_x:
                edits.append(Edit("+", -1, prev_y, b[prev_y]))
            else:
                edits.append(Edit("-", prev_x, -1, a[prev_x]))
        x, y = prev_x, prev_y
    edits.reverse()
    return edits


def unified(a_text: str, b_text: str, a_name="a", b_name="b", context=3) -> str:
    a, b = a_text.splitlines(), b_text.splitlines()
    edits = myers(a, b)
    if all(e.op == " " for e in edits):
        return ""
    changed = [i for i, e in enumerate(edits) if e.op != " "]

    # group changes whose context windows overlap into one hunk
    hunks, start, end = [], None, None
    for i in changed:
        lo, hi = max(0, i - context), min(len(edits), i + context + 1)
        if start is None:
            start, end = lo, hi
        elif lo <= end:
            end = hi
        else:
            hunks.append((start, end))
            start, end = lo, hi
    hunks.append((start, end))

    out = [f"--- {a_name}", f"+++ {b_name}"]
    for s, e in hunks:
        chunk = edits[s:e]
        # line numbers: first a/b position at or after the hunk start
        a_start = _first_pos(edits, s, "a")
        b_start = _first_pos(edits, s, "b")
        a_len = sum(1 for x in chunk if x.op != "+")
        b_len = sum(1 for x in chunk if x.op != "-")
        out.append(f"@@ -{_rng(a_start, a_len)} +{_rng(b_start, b_len)} @@")
        out += [x.op + x.text for x in chunk]
    return "\n".join(out) + "\n"


def _first_pos(edits, s, side):
    count = 0
    for x in edits[:s]:
        if (side == "a" and x.op != "+") or (side == "b" and x.op != "-"):
            count += 1
    return count


def _rng(start, length):
    # unified diff quirk: an empty range is reported at the line *before* it
    if length == 0:
        return f"{start},0"
    return f"{start + 1}" if length == 1 else f"{start + 1},{length}"


def diffstat(a_text: str, b_text: str) -> tuple[int, int]:
    edits = myers(a_text.splitlines(), b_text.splitlines())
    return sum(e.op == "+" for e in edits), sum(e.op == "-" for e in edits)
