"""Unit coverage for the pure quiz scoring rule.

`score_attempt` is the scoring half of what used to live inside
`student_quiz._grade_and_finalize`: it answers "what is this attempt worth?" without
touching the database, the submission state machine or the outbox. The dispute regrade
needs exactly that half, which is why it is its own function.

No database: an attempt is a plain object with `answers`, `questions_snapshot`,
`config_snapshot` and `violations`.
"""

from __future__ import annotations

from types import SimpleNamespace

from submissions_checker.services.quiz_scoring import apply_question_overrides, score_attempt


def _answer(question_id: int, points: int | None, *, is_correct: bool | None = True):
    return SimpleNamespace(
        question_id=question_id,
        points_earned=points,
        is_correct=is_correct,
    )


def _attempt(
    *,
    answers: list,
    question_points: list[int],
    threshold: float | None = None,
    violations: dict | None = None,
):
    config: dict = {}
    if threshold is not None:
        config["pass_threshold_pct"] = threshold
    return SimpleNamespace(
        answers=answers,
        questions_snapshot=[
            {
                "id": i,
                "type": "SINGLE_CHOICE",
                "text": f"q{i}",
                "points": p,
                "time_limit_seconds": None,
                "config": {"options": ["a", "b"], "correct": 0},
            }
            for i, p in enumerate(question_points)
        ],
        config_snapshot=config,
        violations=violations,
    )


def test_score_sums_points_earned_and_max_sums_the_snapshot() -> None:
    attempt = _attempt(
        answers=[_answer(0, 2), _answer(1, 0, is_correct=False), _answer(2, 3)],
        question_points=[2, 1, 3],
    )
    score, max_score, is_passed, force_failed = score_attempt(attempt)
    assert (score, max_score) == (5, 6)
    assert is_passed is True
    assert force_failed is False


def test_default_threshold_is_sixty_percent() -> None:
    # 3/5 == 0.6 exactly, which the default threshold accepts.
    passing = _attempt(answers=[_answer(0, 3)], question_points=[5])
    assert score_attempt(passing)[2] is True

    # 2/5 == 0.4 does not.
    failing = _attempt(answers=[_answer(0, 2)], question_points=[5])
    assert score_attempt(failing)[2] is False


def test_configured_threshold_overrides_the_default() -> None:
    attempt = _attempt(answers=[_answer(0, 7)], question_points=[10], threshold=0.8)
    assert score_attempt(attempt)[2] is False

    attempt.config_snapshot["pass_threshold_pct"] = 0.7
    assert score_attempt(attempt)[2] is True


def test_empty_quiz_cannot_be_passed() -> None:
    attempt = _attempt(answers=[], question_points=[])
    assert score_attempt(attempt) == (0, 0, False, False)


def test_no_answers_scores_zero_rather_than_erroring() -> None:
    attempt = _attempt(answers=[], question_points=[1, 1])
    assert score_attempt(attempt) == (0, 2, False, False)


def test_force_fail_keeps_the_score_but_never_passes() -> None:
    """A cheat flag zeroes the outcome, not the arithmetic — the teacher still sees the score."""
    attempt = _attempt(
        answers=[_answer(0, 5)],
        question_points=[5],
        violations={"_force_fail": True, "tab_switch": 3},
    )
    score, max_score, is_passed, force_failed = score_attempt(attempt)
    assert (score, max_score) == (5, 5)
    assert is_passed is False
    assert force_failed is True


def test_violations_without_force_fail_do_not_block_a_pass() -> None:
    attempt = _attempt(
        answers=[_answer(0, 5)],
        question_points=[5],
        violations={"tab_switch": 1, "_time_penalty_seconds": 30},
    )
    assert score_attempt(attempt) == (5, 5, True, False)


def test_missing_violations_dict_is_treated_as_clean() -> None:
    attempt = _attempt(answers=[_answer(0, 1)], question_points=[1], violations=None)
    assert score_attempt(attempt) == (1, 1, True, False)


# ── apply_question_overrides ─────────────────────────────────────────────────


class _Recorder:
    """Stands in for the session: only ``add`` is reachable from pure override code."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)


def _gradable_attempt(answers: list, question_points: list[int]):
    attempt = _attempt(answers=answers, question_points=question_points)
    attempt.id = 7
    return attempt


def test_override_credits_a_wrong_answer() -> None:
    wrong = _answer(0, 0, is_correct=False)
    attempt = _gradable_attempt([wrong], [3, 1])
    db = _Recorder()

    changed = apply_question_overrides(attempt, {0}, db)

    assert changed == 1
    assert wrong.is_correct is True
    assert wrong.points_earned == 3
    assert db.added == []  # the row already existed


def test_override_preserves_the_timed_out_flag() -> None:
    """The result page must still say 'lost to the clock' — only the credit changes."""
    burned = _answer(0, 0, is_correct=False)
    burned.timed_out = True
    attempt = _gradable_attempt([burned], [2])

    apply_question_overrides(attempt, {0}, _Recorder())

    assert burned.timed_out is True
    assert burned.points_earned == 2


def test_override_creates_a_row_for_a_question_never_reached() -> None:
    attempt = _gradable_attempt([], [5])
    db = _Recorder()

    changed = apply_question_overrides(attempt, {0}, db)

    assert changed == 1
    created = db.added[0]
    assert created.attempt_id == 7
    assert created.question_id == 0
    assert created.is_correct is True
    assert created.points_earned == 5
    assert created.timed_out is False
    assert created in attempt.answers


def test_override_is_idempotent() -> None:
    """Sets, never increments — re-accepting a dispute must not double the credit."""
    answer = _answer(0, 0, is_correct=False)
    attempt = _gradable_attempt([answer], [4])
    db = _Recorder()

    apply_question_overrides(attempt, {0}, db)
    first = score_attempt(attempt)
    apply_question_overrides(attempt, {0}, db)

    assert score_attempt(attempt) == first
    assert first[0] == 4
    assert len(attempt.answers) == 1


def test_override_ignores_a_question_this_attempt_never_drew() -> None:
    answer = _answer(0, 0, is_correct=False)
    attempt = _gradable_attempt([answer], [1])
    db = _Recorder()

    assert apply_question_overrides(attempt, {99}, db) == 0
    assert answer.points_earned == 0
    assert db.added == []


def test_override_with_no_ids_is_a_no_op() -> None:
    answer = _answer(0, 0, is_correct=False)
    attempt = _gradable_attempt([answer], [1])
    assert apply_question_overrides(attempt, set(), _Recorder()) == 0
    assert answer.points_earned == 0


def test_override_flips_a_failing_attempt_to_a_pass() -> None:
    """The whole point: crediting the disputed question clears the threshold."""
    attempt = _gradable_attempt(
        [_answer(0, 1), _answer(1, 0, is_correct=False)],
        [1, 1],
    )
    assert score_attempt(attempt)[2] is False

    apply_question_overrides(attempt, {1}, _Recorder())

    score, max_score, is_passed, _ = score_attempt(attempt)
    assert (score, max_score, is_passed) == (2, 2, True)
