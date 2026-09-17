"""Parity test: the production worker delegates to the shared check core and persists
exactly what the core returns — proving there is one check code path (no drift).

No real database or Docker: the DB is faked and check_core.run_check is replaced with a
recorder that returns a canned outcome. We assert the resolved plan reaches the core and
the core's outcome is what lands in submission.test_results.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from submissions_checker.core import metrics
from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services import check_core
from submissions_checker.workers.tasks import check_tasks

_CONFIG = {
    "subjectCode": "demo",
    "assignments": {
        "lab1": {
            "variants_required": True,
            "review_mode": "tests_only",
            "common": {
                "sandbox": {
                    "image": "demo-checker:local",
                    "check_command": "assignments/lab1/check_common.py",
                    "min_pass_score": 60,
                }
            },
            "variants": {"3": {"sandbox": {"check_command": "assignments/lab1/check.py"}}},
        }
    },
}


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
        # Used by check_tasks._notify_student's User.id lookup — no matching
        # user in this minimal mock, so the notification push is a no-op.
        return None

    def add(self, obj):
        self.added.append(obj)


async def test_worker_persists_core_outcome(tmp_path, monkeypatch) -> None:
    # A real ZIP the worker can extract.
    zip_path = tmp_path / "s.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("solution.py", "print('hi')\n")

    subject = SimpleNamespace(id=5)
    subjects_assignment = SimpleNamespace(
        id=7,
        code="lab1",
        title="Lab 1",
        subject=subject,
        subject_id=5,
        config={},
        min_grade=0,
        max_grade=100,
    )
    student_assignment = SimpleNamespace(
        variant="3", subjects_assignment=subjects_assignment, student_id=42, grade=None
    )
    submission = SimpleNamespace(
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
    config_record = SimpleNamespace(id=99, version=2, config=_CONFIG)
    db = _FakeDB(submission, config_record)

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(
        check_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            plugins_dir=str(tmp_path),
            host_plugins_dir=None,
            sandbox_max_memory="512m",
            sandbox_max_cpus=1.0,
        ),
    )

    canned_tests = [{"name": "v1", "passed": True, "points_earned": 100, "max_points": 100}]
    recorded: dict = {}

    async def fake_run_check(*, plan, submission_dir, plugin_dir, sandbox):
        recorded["plan"] = plan
        recorded["submission_dir"] = submission_dir
        recorded["plugin_dir"] = plugin_dir
        return check_core.CheckOutcome("passed", 100, 100, canned_tests)

    monkeypatch.setattr(check_tasks.check_core, "run_check", fake_run_check)
    passed_before = metrics.checks_total.labels(outcome="passed")._value.get()
    observed_before = metrics.check_duration_seconds._sum.get()

    await check_tasks.execute_check_task(db, {"submission_id": 1})

    assert metrics.checks_total.labels(outcome="passed")._value.get() == passed_before + 1
    assert metrics.check_duration_seconds._sum.get() >= observed_before

    # Core received the resolved plan (variant check_command from variant 3).
    assert isinstance(recorded["plan"], check_core.CheckPlan)
    assert recorded["plan"].variant_check == "assignments/lab1/check.py"
    assert recorded["plugin_dir"] == Path(str(tmp_path)) / "demo"

    # Worker persisted exactly the core's outcome, plus the pinned config version.
    assert submission.test_results == {
        "passed": True,
        "score": 100,
        "max_score": 100,
        "tests": canned_tests,
        "plugin_config_version": 2,
    }
    assert submission.status == SubmissionStatus.COMPLETED
    # Tests-only completion finalizes the grade: works=100%, default weights → 100.
    assert student_assignment.grade == 100
    assert submission.grade_breakdown["grade"] == 100


async def test_worker_config_error_records_reason(tmp_path, monkeypatch) -> None:
    subject = SimpleNamespace(id=5)
    subjects_assignment = SimpleNamespace(code="missing", subject=subject)
    student_assignment = SimpleNamespace(variant=None, subjects_assignment=subjects_assignment)
    submission = SimpleNamespace(
        id=1,
        plugin_config_id=99,
        source_metadata={"saved_as": "s.zip"},
        status=SubmissionStatus.PENDING,
        test_results=None,
        students_assignment=student_assignment,
    )
    config_record = SimpleNamespace(id=99, version=2, config=_CONFIG)
    db = _FakeDB(submission, config_record)
    monkeypatch.setattr(
        check_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            plugins_dir=str(tmp_path),
            host_plugins_dir=None,
            sandbox_max_memory="512m",
            sandbox_max_cpus=1.0,
        ),
    )

    # A config error is detected while the submission is still PENDING. _fail_validation
    # steps through start_validation (PENDING -> VALIDATING) before validation_failed, so
    # the submission converges cleanly on VALIDATION_FAILED with a teacher-facing reason
    # instead of raising InvalidTransitionError.
    await check_tasks.execute_check_task(db, {"submission_id": 1})
    assert submission.status == SubmissionStatus.VALIDATION_FAILED
    assert "check_reason" in submission.test_results


async def test_worker_check_execution_error_fails_validation_not_wedged(
    tmp_path, monkeypatch
) -> None:
    """A crashed check script (non-zero exit, bad result.json) must fail the
    submission cleanly instead of leaving it stuck in VALIDATING with no visible
    error (docs/known_bugs.md #6): execute_check_task must not propagate
    check_core.CheckExecutionError, and the outbox processor must therefore never
    see an exception to retry — there is no second attempt at all."""
    zip_path = tmp_path / "s.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("solution.py", "print('hi')\n")

    subject = SimpleNamespace(id=5)
    subjects_assignment = SimpleNamespace(code="lab1", subject=subject)
    student_assignment = SimpleNamespace(variant="3", subjects_assignment=subjects_assignment)
    submission = SimpleNamespace(
        id=1,
        plugin_config_id=99,
        source_metadata={"saved_as": "s.zip"},
        status=SubmissionStatus.PENDING,
        test_results=None,
        students_assignment=student_assignment,
    )
    config_record = SimpleNamespace(id=99, version=2, config=_CONFIG)
    db = _FakeDB(submission, config_record)

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(
        check_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            plugins_dir=str(tmp_path),
            host_plugins_dir=None,
            sandbox_max_memory="512m",
            sandbox_max_cpus=1.0,
        ),
    )

    async def crashing_run_check(*, plan, submission_dir, plugin_dir, sandbox):
        raise check_core.CheckExecutionError("check script exited 1: NameError: boom")

    monkeypatch.setattr(check_tasks.check_core, "run_check", crashing_run_check)
    error_before = metrics.checks_total.labels(outcome="error")._value.get()

    # Must not raise — this is exactly what previously propagated out of
    # execute_check_task, got caught by the outbox processor's generic handler,
    # and retried against a submission already past PENDING.
    await check_tasks.execute_check_task(db, {"submission_id": 1})

    assert submission.status == SubmissionStatus.VALIDATION_FAILED
    assert "boom" in submission.test_results["check_reason"]
    assert metrics.checks_total.labels(outcome="error")._value.get() == error_before + 1


async def test_worker_refuses_path_like_subject_code(tmp_path, monkeypatch) -> None:
    """A stored config whose subjectCode is not a plain identifier must never be joined
    onto the plugins root: the check fails validation and the sandbox is never reached."""
    zip_path = tmp_path / "s.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("solution.py", "print('hi')\n")

    subject = SimpleNamespace(id=5)
    subjects_assignment = SimpleNamespace(
        id=7,
        code="lab1",
        title="Lab 1",
        subject=subject,
        subject_id=5,
        config={},
        min_grade=0,
        max_grade=100,
    )
    student_assignment = SimpleNamespace(
        variant="3", subjects_assignment=subjects_assignment, student_id=42, grade=None
    )
    submission = SimpleNamespace(
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
    evil = {**_CONFIG, "subjectCode": "../templates"}
    db = _FakeDB(submission, SimpleNamespace(id=99, version=2, config=evil))

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(
        check_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            plugins_dir=str(tmp_path),
            host_plugins_dir=None,
            sandbox_max_memory="512m",
            sandbox_max_cpus=1.0,
        ),
    )
    called: list = []

    async def fake_run_check(**kwargs):
        called.append(kwargs)
        return check_core.CheckOutcome("passed", 100, 100, [])

    monkeypatch.setattr(check_tasks.check_core, "run_check", fake_run_check)

    await check_tasks.execute_check_task(db, {"submission_id": 1})

    assert called == []
    assert submission.status == SubmissionStatus.VALIDATION_FAILED
    assert "subjectCode" in submission.test_results["check_reason"]
