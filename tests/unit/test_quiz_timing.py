"""Unit coverage for per-question quiz timing and the check-free review modes.

No database and no Docker: `_build_questions_from_config` and `_advance_expired` are pure
enough to drive with plain objects, and `execute_check_task`'s quiz-first branch is exercised
against a fake session with a sandbox that fails the test if it is ever touched.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from submissions_checker.api.routes import student_quiz
from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services.grading import compute_grade
from submissions_checker.workers.tasks import check_tasks

# ── _build_questions_from_config: per-question seconds ───────────────────────


def _q(text: str, **extra) -> dict:
    return {
        "type": "single_choice",
        "text": text,
        "points": 1,
        "options": ["a", "b"],
        "correct": 0,
        **extra,
    }


def test_question_limit_overrides_quiz_default() -> None:
    snap = student_quiz._build_questions_from_config(
        {
            "questions": [_q("easy"), _q("hard", time_limit_seconds=60)],
            "shuffle_questions": False,
            "question_time_default_seconds": 30,
        }
    )
    by_text = {q["text"]: q["time_limit_seconds"] for q in snap}
    assert by_text == {"easy": 30, "hard": 60}


def test_no_timing_keys_means_no_limits() -> None:
    snap = student_quiz._build_questions_from_config(
        {
            "questions": [_q("one"), _q("two")],
            "shuffle_questions": False,
        }
    )
    assert [q["time_limit_seconds"] for q in snap] == [None, None]


def test_question_limit_without_quiz_default() -> None:
    snap = student_quiz._build_questions_from_config(
        {
            "questions": [_q("timed", time_limit_seconds=45), _q("untimed")],
            "shuffle_questions": False,
        }
    )
    assert [q["time_limit_seconds"] for q in snap] == [45, None]


# ── _advance_expired ─────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)


def _attempt(
    seconds: list[int | None],
    *,
    started_ago: float,
    index: int = 0,
    paused_at: datetime | None = None,
    paused_seconds: int = 0,
):
    return SimpleNamespace(
        id=1,
        current_index=index,
        paused_at=paused_at,
        paused_seconds=paused_seconds,
        question_started_at=datetime.now(UTC) - timedelta(seconds=started_ago),
        questions_snapshot=[
            {
                "id": i,
                "type": "SINGLE_CHOICE",
                "text": f"q{i}",
                "points": 1,
                "time_limit_seconds": s,
                "config": {"options": ["a"], "correct": 0},
            }
            for i, s in enumerate(seconds)
        ],
        answers=[],
        config_snapshot={"per_question_timing": True},
    )


def test_advance_expired_leaves_a_live_question_alone() -> None:
    attempt = _attempt([30, 30], started_ago=5)
    db = _Recorder()
    assert student_quiz._advance_expired(attempt, db) == 0
    assert attempt.current_index == 0
    assert db.added == []


def test_advance_expired_burns_one_elapsed_window() -> None:
    attempt = _attempt([30, 30], started_ago=31)
    db = _Recorder()
    assert student_quiz._advance_expired(attempt, db) == 1
    assert attempt.current_index == 1
    assert len(db.added) == 1
    burned = db.added[0]
    assert burned.question_id == 0
    assert burned.timed_out is True
    assert burned.points_earned == 0
    assert burned.is_correct is False


def test_advance_expired_burns_every_window_missed_while_away() -> None:
    # Gone for 10 minutes: three 30s windows cannot have survived.
    attempt = _attempt([30, 30, 30], started_ago=600)
    db = _Recorder()
    assert student_quiz._advance_expired(attempt, db) == 3
    assert attempt.current_index == 3
    assert student_quiz._current_question(attempt) is None
    assert [a.question_id for a in db.added] == [0, 1, 2]


def test_advance_expired_stops_at_an_untimed_question() -> None:
    attempt = _attempt([30, None, 30], started_ago=600)
    db = _Recorder()
    assert student_quiz._advance_expired(attempt, db) == 1
    assert attempt.current_index == 1
    assert student_quiz._question_seconds_remaining(attempt) is None


def test_seconds_remaining_never_goes_negative() -> None:
    attempt = _attempt([30], started_ago=29)
    assert student_quiz._question_seconds_remaining(attempt) == 1
    attempt.question_started_at = datetime.now(UTC) - timedelta(seconds=120)
    assert student_quiz._question_seconds_remaining(attempt) == 0


# ── grading with no test component ───────────────────────────────────────────


def test_grade_is_the_quiz_alone_when_nothing_ran() -> None:
    breakdown = compute_grade(
        {"code_weight": 0, "quiz_weight": 1},
        0,
        8,
        works_pct=None,
        ai_mark=None,
        quiz_pct=75.0,
    )
    assert breakdown.works_score is None
    assert breakdown.quiz_score == 75.0
    assert breakdown.grade == 6  # 75% of an 8-point band


def test_full_quiz_score_reaches_the_top_of_the_band() -> None:
    breakdown = compute_grade(
        {"code_weight": 0, "quiz_weight": 1},
        0,
        8,
        works_pct=None,
        ai_mark=None,
        quiz_pct=100.0,
    )
    assert breakdown.grade == 8


# ── check-free review modes ──────────────────────────────────────────────────


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, submission, config_record):
        self._submission = submission
        self._config_record = config_record
        self.added: list = []

    async def execute(self, _stmt):
        return _Result(self._submission)

    async def get(self, _model, _pk):
        return self._config_record

    async def scalar(self, _stmt):
        return None

    def add(self, obj):
        self.added.append(obj)


def _quiz_first_config(review_mode: str) -> dict:
    # Deliberately no sandbox/check_command: that is the whole point of these modes.
    return {
        "subjectCode": "winapi",
        "assignments": {"lab1": {"review_mode": review_mode, "quiz": {"questions": [_q("x")]}}},
    }


def _quiz_first_submission():
    subject = SimpleNamespace(id=5)
    subjects_assignment = SimpleNamespace(
        id=7,
        code="lab1",
        title="Lab 1",
        subject=subject,
        subject_id=5,
        config={},
        min_grade=0,
        max_grade=8,
    )
    student_assignment = SimpleNamespace(
        variant=None, subjects_assignment=subjects_assignment, student_id=42, grade=None
    )
    return SimpleNamespace(
        id=1,
        plugin_config_id=99,
        source_metadata={"saved_as": "s.zip"},
        status=SubmissionStatus.PENDING,
        test_results=None,
        ai_review=None,
        grade_breakdown=None,
        quiz_attempts=[],
        students_assignment=student_assignment,
    )


def _explode(*_a, **_kw):
    raise AssertionError("the sandbox must never be touched in a quiz-first review mode")


@pytest.mark.parametrize("review_mode", ["quiz_only", "quiz_then_teacher"])
async def test_quiz_first_mode_skips_the_sandbox(tmp_path, monkeypatch, review_mode) -> None:
    with zipfile.ZipFile(tmp_path / "s.zip", "w") as zf:
        zf.writestr("report.md", "# lab 1\n")

    submission = _quiz_first_submission()
    db = _FakeDB(
        submission, SimpleNamespace(id=99, version=2, config=_quiz_first_config(review_mode))
    )
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks.check_core, "run_check", _explode)
    monkeypatch.setattr(check_tasks.check_core, "resolve_check_plan", _explode)

    await check_tasks.execute_check_task(db, {"submission_id": 1})

    assert submission.status == SubmissionStatus.QUIZ_SENT
    assert submission.test_results == {"skipped": True, "reason": review_mode}
    # No score/max_score, so the works component drops out of the grade entirely.
    assert "score" not in submission.test_results


async def test_quiz_first_mode_still_rejects_a_corrupt_archive(tmp_path, monkeypatch) -> None:
    (tmp_path / "s.zip").write_bytes(b"this is not a zip file")

    submission = _quiz_first_submission()
    db = _FakeDB(
        submission,
        SimpleNamespace(id=99, version=2, config=_quiz_first_config("quiz_then_teacher")),
    )
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks.check_core, "run_check", _explode)

    await check_tasks.execute_check_task(db, {"submission_id": 1})

    assert submission.status == SubmissionStatus.VALIDATION_FAILED
    assert "ZIP" in submission.test_results["check_reason"]


async def test_quiz_first_mode_rejects_a_traversal_archive(tmp_path, monkeypatch) -> None:
    with zipfile.ZipFile(tmp_path / "s.zip", "w") as zf:
        zf.writestr("../escape.txt", "nope")

    submission = _quiz_first_submission()
    db = _FakeDB(
        submission, SimpleNamespace(id=99, version=2, config=_quiz_first_config("quiz_only"))
    )
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks.check_core, "run_check", _explode)

    await check_tasks.execute_check_task(db, {"submission_id": 1})

    assert submission.status == SubmissionStatus.VALIDATION_FAILED
    assert "unsafe" in submission.test_results["check_reason"].lower()


# ── air-raid pause: the frozen clock ─────────────────────────────────────────
#
# Every quiz clock is a delta from a stored timestamp, so freezing "now" is what stops all
# of them at once. These tests pin that: while `paused_at` is set no clock advances, and
# after the resume arithmetic each one continues from exactly where it stopped.


def _timed_attempt(
    *,
    limit_minutes: int | None = 10,
    started_ago: float = 60,
    paused_at: datetime | None = None,
    paused_seconds: int = 0,
    penalty_seconds: int = 0,
):
    """A single-page attempt with an attempt-wide clock."""
    violations: dict = {}
    if penalty_seconds:
        violations["_time_penalty_seconds"] = penalty_seconds
    config: dict = {}
    if limit_minutes is not None:
        config["time_limit_minutes"] = limit_minutes
    return SimpleNamespace(
        id=1,
        current_index=0,
        started_at=datetime.now(UTC) - timedelta(seconds=started_ago),
        question_started_at=None,
        paused_at=paused_at,
        paused_seconds=paused_seconds,
        questions_snapshot=[],
        answers=[],
        config_snapshot=config,
        violations=violations,
    )


def test_attempt_clock_is_frozen_while_paused() -> None:
    """Two pauses of very different ages report the same remaining time."""
    just_paused = _timed_attempt(started_ago=60, paused_at=datetime.now(UTC))
    long_paused = _timed_attempt(
        started_ago=60 + 3600, paused_at=datetime.now(UTC) - timedelta(seconds=3600)
    )
    assert student_quiz._seconds_remaining(just_paused) == pytest.approx(
        student_quiz._seconds_remaining(long_paused), abs=1
    )


def test_attempt_cannot_time_out_while_paused() -> None:
    """A student sheltering for two hours has not run out of time."""
    attempt = _timed_attempt(
        limit_minutes=10,
        started_ago=60 + 7200,
        paused_at=datetime.now(UTC) - timedelta(seconds=7200),
    )
    assert student_quiz._is_timed_out(attempt) is False
    assert student_quiz._seconds_remaining(attempt) == pytest.approx(540, abs=2)


def test_attempt_clock_excludes_closed_pauses() -> None:
    attempt = _timed_attempt(limit_minutes=20, started_ago=1260, paused_seconds=600)
    assert student_quiz._is_timed_out(attempt) is False
    # 1260s wall clock minus a 600s pause is 660s of quiz time against a 1200s limit.
    assert student_quiz._seconds_remaining(attempt) == pytest.approx(540, abs=2)


def test_resume_hands_back_the_exact_remaining_value() -> None:
    """Close the tab, come back later, continue — the timer reads what it read before."""
    paused_for = 1800
    attempt = _timed_attempt(
        limit_minutes=30,
        started_ago=300 + paused_for,
        paused_at=datetime.now(UTC) - timedelta(seconds=paused_for),
    )
    while_paused = student_quiz._seconds_remaining(attempt)

    student_quiz._close_open_pause(attempt)

    assert attempt.paused_at is None
    assert attempt.paused_seconds == pytest.approx(paused_for, abs=2)
    assert student_quiz._seconds_remaining(attempt) == pytest.approx(while_paused, abs=2)


def test_a_time_penalty_survives_a_pause() -> None:
    """A reduce_time violation taken before the raid is still owed after it."""
    attempt = _timed_attempt(
        limit_minutes=10, started_ago=720, penalty_seconds=60, paused_seconds=600
    )
    # 720s of wall clock less a 600s pause is 120s of quiz time; the 600s limit then owes
    # 600 - 60 penalty - 120 spent.
    assert student_quiz._seconds_remaining(attempt) == pytest.approx(420, abs=2)


def test_question_clock_is_frozen_while_paused() -> None:
    just_paused = _attempt([30], started_ago=10, paused_at=datetime.now(UTC))
    long_paused = _attempt(
        [30], started_ago=10 + 900, paused_at=datetime.now(UTC) - timedelta(seconds=900)
    )
    assert student_quiz._question_seconds_remaining(just_paused) == pytest.approx(20, abs=1)
    assert student_quiz._question_seconds_remaining(
        long_paused
    ) == student_quiz._question_seconds_remaining(just_paused)


def test_advance_expired_burns_nothing_while_paused() -> None:
    """The guard that stops a pause from costing the student their questions."""
    attempt = _attempt(
        [30, 30, 30], started_ago=600, paused_at=datetime.now(UTC) - timedelta(seconds=570)
    )
    db = _Recorder()
    assert student_quiz._advance_expired(attempt, db) == 0
    assert attempt.current_index == 0
    assert db.added == []


def test_resume_shifts_the_question_clock_forward() -> None:
    attempt = _attempt(
        [60], started_ago=20 + 900, paused_at=datetime.now(UTC) - timedelta(seconds=900)
    )
    while_paused = student_quiz._question_seconds_remaining(attempt)
    assert while_paused == pytest.approx(40, abs=1)

    student_quiz._close_open_pause(attempt)

    assert attempt.paused_at is None
    assert student_quiz._question_seconds_remaining(attempt) == pytest.approx(while_paused, abs=1)


def test_a_second_pause_accumulates() -> None:
    attempt = _timed_attempt(
        started_ago=1000, paused_seconds=300, paused_at=datetime.now(UTC) - timedelta(seconds=200)
    )
    student_quiz._close_open_pause(attempt)
    assert attempt.paused_seconds == pytest.approx(500, abs=2)


def test_closing_a_pause_that_is_not_open_does_nothing() -> None:
    attempt = _timed_attempt(started_ago=100, paused_seconds=42)
    student_quiz._close_open_pause(attempt)
    assert attempt.paused_at is None
    assert attempt.paused_seconds == 42
