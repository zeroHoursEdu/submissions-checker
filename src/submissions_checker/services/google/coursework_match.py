"""Guess which Classroom coursework belongs to which assignment, for one-click linking.

Pure: titles in, suggestions out. Labs are numbered on both sides ("ЛР №4 — Сокети" vs
"4. Клієнт-серверна архітектура… сокети"), so the first number in a title decides; title
similarity breaks ties and covers assignments without a number. The teacher reviews every
suggestion before saving, so a missing guess is fine and a wrong one is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher

_NUMBER = re.compile(r"\d+")
_WORD = re.compile(r"[^\W\d_]+")
MIN_SIMILARITY = 0.6
MARGIN = 0.1


def _number(title: str) -> int | None:
    m = _NUMBER.search(title)
    return int(m.group()) if m else None


def _words(title: str) -> str:
    return " ".join(_WORD.findall(title.lower()))


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _words(a), _words(b)).ratio()


def suggest(
    assignments: Sequence[tuple[int, str]],
    courseworks: Sequence[tuple[str, str]],
    taken: Iterable[str] = (),
) -> dict[int, str]:
    """assignment id -> coursework id; each coursework at most once, `taken` never."""
    free = {cw_id: title for cw_id, title in courseworks if cw_id not in set(taken)}
    out: dict[int, str] = {}

    for a_id, a_title in assignments:
        n = _number(a_title)
        if n is None:
            continue
        same = [cw_id for cw_id, title in free.items() if _number(title) == n]
        if not same:
            continue
        best = max(same, key=lambda cw_id: _similarity(a_title, free[cw_id]))
        out[a_id] = best
        del free[best]

    for a_id, a_title in assignments:
        if a_id in out or not free:
            continue
        scored = sorted(
            ((_similarity(a_title, title), cw_id) for cw_id, title in free.items()), reverse=True
        )
        top, cw_id = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if top >= MIN_SIMILARITY and top - runner_up >= MARGIN:
            out[a_id] = cw_id
            del free[cw_id]
    return out
