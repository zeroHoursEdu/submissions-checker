"""Split one quiz draw into disjoint, near-equal slices — one per squad member.

Pure and DB-free. Required questions are dealt round-robin first so no member gets all
of them, then the optional ones continue the same deal; slice sizes therefore differ by
at most one and every id appears in exactly one slice.
"""

from __future__ import annotations

from typing import Any


def split_draw(questions: list[dict[str, Any]], member_count: int) -> list[list[int]]:
    if member_count < 1:
        raise ValueError("member_count must be >= 1")
    ordered = [q for q in questions if q.get("is_required")] + [
        q for q in questions if not q.get("is_required")
    ]
    slices: list[list[int]] = [[] for _ in range(member_count)]
    for position, q in enumerate(ordered):
        slices[position % member_count].append(int(q["id"]))
    return slices
