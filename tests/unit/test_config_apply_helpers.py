"""Unit tests for the *pure* helpers of services.config_apply.ConfigApplyService.

Only the side-effect-free methods are exercised — deadline parsing, field-level
diffing, config/content-file builders, and plan computation (with a fake
subject and a tmp_path for content-file existence checks). No DB session and no
S3 storage are constructed (storage=None).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from submissions_checker.services.config_apply import (
    ConfigApplyPlan,
    ConfigApplyService,
)


@pytest.fixture
def svc(tmp_path: Path) -> ConfigApplyService:
    return ConfigApplyService(storage=None, plugins_dir=tmp_path)


# ── _parse_deadline ───────────────────────────────────────────────────────────

def test_parse_deadline_valid_iso(svc: ConfigApplyService) -> None:
    dt = svc._parse_deadline("2026-07-01T12:00:00")
    assert dt == datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    assert dt.tzinfo is UTC


def test_parse_deadline_none_and_empty(svc: ConfigApplyService) -> None:
    assert svc._parse_deadline(None) is None
    assert svc._parse_deadline("") is None


def test_parse_deadline_garbage_returns_none(svc: ConfigApplyService) -> None:
    assert svc._parse_deadline("not-a-date") is None


def test_parse_deadline_date_only(svc: ConfigApplyService) -> None:
    dt = svc._parse_deadline("2026-07-01")
    assert dt == datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)


# ── _diff_assignment ──────────────────────────────────────────────────────────

def test_diff_assignment_identical_has_no_changes(svc: ConfigApplyService) -> None:
    a = {"title": "T", "min_grade": 0, "sandbox": {"image": "x"}}
    assert svc._diff_assignment(a, dict(a)) == []


def test_diff_assignment_simple_field_change(svc: ConfigApplyService) -> None:
    changed = svc._diff_assignment({"title": "New"}, {"title": "Old"})
    assert changed == ["title"]


def test_diff_assignment_config_grouped_under_config(svc: ConfigApplyService) -> None:
    changed = svc._diff_assignment(
        {"sandbox": {"image": "v2"}}, {"sandbox": {"image": "v1"}}
    )
    assert changed == ["config"]


def test_diff_assignment_content_files_change(svc: ConfigApplyService) -> None:
    changed = svc._diff_assignment(
        {"contentFiles": [{"filename": "a.pdf"}]},
        {"contentFiles": []},
    )
    assert changed == ["content_files"]


def test_diff_assignment_multiple_changes(svc: ConfigApplyService) -> None:
    changed = svc._diff_assignment(
        {"title": "B", "max_grade": 90, "variants": {"1": {}}},
        {"title": "A", "max_grade": 100},
    )
    assert set(changed) == {"title", "max_grade", "config"}


# ── _build_assignment_config ──────────────────────────────────────────────────

def test_build_assignment_config_filters_to_known_keys(svc: ConfigApplyService) -> None:
    cfg = svc._build_assignment_config(
        {
            "title": "ignored-here",
            "review_mode": "TEACHER",
            "sandbox": {"image": "x"},
            "unknown": 1,
        }
    )
    assert cfg == {"review_mode": "TEACHER", "sandbox": {"image": "x"}}


def test_build_assignment_config_empty(svc: ConfigApplyService) -> None:
    assert svc._build_assignment_config({"title": "only-meta"}) == {}


# ── _build_content_files ──────────────────────────────────────────────────────

def test_build_content_files_maps_urls(svc: ConfigApplyService) -> None:
    a_cfg = {"contentFiles": [{"filename": "spec.pdf", "displayName": "Spec"}]}
    url_map = {"subjects/demo/assignments/lab1/spec.pdf": "https://cdn/spec.pdf"}
    out = svc._build_content_files(a_cfg, url_map, "demo", "lab1")
    assert out == [
        {"url": "https://cdn/spec.pdf", "display_name": "Spec", "filename": "spec.pdf"}
    ]


def test_build_content_files_display_name_defaults_to_filename(svc: ConfigApplyService) -> None:
    out = svc._build_content_files(
        {"contentFiles": [{"filename": "a.txt"}]}, {}, "s", "c"
    )
    assert out[0]["display_name"] == "a.txt"
    assert out[0]["url"] == ""  # no url in map


def test_build_content_files_skips_entries_without_filename(svc: ConfigApplyService) -> None:
    out = svc._build_content_files(
        {"contentFiles": [{"displayName": "no file"}, {"filename": "ok.txt"}]},
        {}, "s", "c",
    )
    assert [e["filename"] for e in out] == ["ok.txt"]


# ── _compute_plan ─────────────────────────────────────────────────────────────

class _FakeSubject:
    """Stand-in for a Subject ORM row for diffing (attribute access only)."""

    def __init__(self, **kw) -> None:
        self.name = kw.get("name")
        self.description = kw.get("description")
        self.github_repo = kw.get("github_repo")
        self.grid_picture_url = kw.get("grid_picture_url")
        self.main_picture_url = kw.get("main_picture_url")


def test_compute_plan_create_when_subject_is_none(svc: ConfigApplyService, tmp_path: Path) -> None:
    new_cfg = {
        "subjectCode": "demo",
        "name": "Demo",
        "assignments": {"lab1": {"title": "One"}},
    }
    plan = svc._compute_plan(new_cfg, prev_cfg={}, subject=None, tmp_dir=tmp_path)
    assert plan.subject_action == "create"
    assert plan.assignments_to_create == ["lab1"]
    assert plan.assignments_to_update == []
    assert plan.assignments_to_delete == []


def test_compute_plan_unchanged_marks_none(svc: ConfigApplyService, tmp_path: Path) -> None:
    cfg = {
        "subjectCode": "demo",
        "name": "Demo",
        "assignments": {"lab1": {"title": "One"}},
    }
    subject = _FakeSubject(name="Demo")
    # prev config mirrors new exactly -> nothing changes
    plan = svc._compute_plan(cfg, prev_cfg=cfg, subject=subject, tmp_dir=tmp_path)
    assert plan.subject_action == "none"
    assert plan.subject_fields_changed == []
    assert plan.assignments_to_create == []
    assert plan.assignments_to_update == []
    assert plan.assignments_to_delete == []


def test_compute_plan_detects_name_change(svc: ConfigApplyService, tmp_path: Path) -> None:
    new_cfg = {"subjectCode": "demo", "name": "Renamed", "assignments": {}}
    subject = _FakeSubject(name="Old")
    plan = svc._compute_plan(new_cfg, prev_cfg={"name": "Old"}, subject=subject, tmp_dir=tmp_path)
    assert plan.subject_action == "update"
    assert "name" in plan.subject_fields_changed


def test_compute_plan_assignment_delete_and_update(svc: ConfigApplyService, tmp_path: Path) -> None:
    new_cfg = {
        "subjectCode": "demo",
        "name": "Demo",
        "assignments": {"lab1": {"title": "Changed"}},
    }
    prev_cfg = {
        "name": "Demo",
        "assignments": {
            "lab1": {"title": "Original"},
            "lab2": {"title": "Gone"},
        },
    }
    subject = _FakeSubject(name="Demo")
    plan = svc._compute_plan(new_cfg, prev_cfg=prev_cfg, subject=subject, tmp_dir=tmp_path)
    assert plan.assignments_to_delete == ["lab2"]
    assert plan.assignments_to_update and plan.assignments_to_update[0][0] == "lab1"
    assert "title" in plan.assignments_to_update[0][1]


def test_compute_plan_returns_dataclass(svc: ConfigApplyService, tmp_path: Path) -> None:
    plan = svc._compute_plan(
        {"subjectCode": "d", "name": "D", "assignments": {}}, {}, None, tmp_path
    )
    assert isinstance(plan, ConfigApplyPlan)
