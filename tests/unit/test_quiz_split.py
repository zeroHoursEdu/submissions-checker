"""Pure split of one quiz draw into near-equal, disjoint per-member slices."""

from __future__ import annotations

import pytest

from submissions_checker.services.quiz_split import split_draw


def _q(i: int, required: bool = False) -> dict:
    return {"id": i, "is_required": required}


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("total", [1, 7, 15, 16])
def test_every_id_lands_exactly_once_and_sizes_differ_by_at_most_one(n: int, total: int) -> None:
    slices = split_draw([_q(i) for i in range(total)], n)
    assert len(slices) == n
    flat = [i for s in slices for i in s]
    assert sorted(flat) == list(range(total))
    sizes = [len(s) for s in slices]
    assert max(sizes) - min(sizes) <= 1


def test_required_questions_are_spread_before_optional_ones() -> None:
    qs = [_q(0, True), _q(1, True), _q(2), _q(3), _q(4)]
    slices = split_draw(qs, 2)
    assert 0 in slices[0] and 1 in slices[1]


def test_member_count_must_be_positive() -> None:
    with pytest.raises(ValueError):
        split_draw([_q(0)], 0)
