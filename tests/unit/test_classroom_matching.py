"""Tests for Classroom roster to student matching."""

from submissions_checker.db.models.enums import ClassroomLinkMethod
from submissions_checker.services.google.matching import (
    Candidate,
    RosterEntry,
    match,
    name_sim,
    name_tokens,
)

# Test fixtures
C = [
    Candidate(1, "Репетуха Микита", "a@gmail.com", "ІП-43"),
    Candidate(2, "Комін Іван", "x@edu.kpi.ua", "ІП-44"),
    Candidate(3, "Комін Ігор", "y@edu.kpi.ua", "ІП-44"),
    Candidate(4, "Гарбєр Маргарита", "m@gmail.com", "ІА-з41"),
]


def test_email_exact_case_insensitive():
    """Email match should be case-insensitive."""
    r = match(RosterEntry("u", "whatever", "X@EDU.KPI.UA"), C)
    assert (r.method, r.student_id) == (ClassroomLinkMethod.EMAIL, 2)


def test_group_prefix_latin_patronymic_matches_cyrillic():
    """Group prefix should be stripped; Latin name should match Cyrillic."""
    r = match(
        RosterEntry("u", "ІП-43 Repetukha Mykyta Volodymyrovych", "repetuxa.mykyta@edu.kpi.ua"),
        C,
    )
    assert (r.method, r.student_id) == (ClassroomLinkMethod.NAME, 1)
    assert r.score is not None and r.score >= 0.95


def test_group_with_z_prefix():
    """Group with 'з' prefix should be stripped."""
    result = name_tokens("ІА-з41 Harbier Marharita Oleksandrivna")
    assert result == ["harbier", "marharita", "oleksandrivna"]


def test_word_order_irrelevant():
    """Word order should not matter for similarity."""
    assert name_sim(name_tokens("Ivan Komin"), name_tokens("Комін Іван")) == 1.0


def test_typo_tolerated():
    """Small typos should still result in a match."""
    r = match(RosterEntry("u", "Repetuha Mykyta", None), C)
    assert r.method == ClassroomLinkMethod.NAME
    assert r.student_id == 1


def test_ambiguous_close_candidates_unmatched():
    """When top candidates are too close, should not match."""
    r = match(RosterEntry("u", "Komin I", None), C)
    assert r.method == ClassroomLinkMethod.NONE
    assert r.student_id is None


def test_not_enrolled_unmatched_with_suggestions():
    """Should provide suggestions even when not matched."""
    r = match(RosterEntry("u", "ІА-з41 Shnep Roman Antonovych", "shnep.roman@edu.kpi.ua"), C)
    assert r.method == ClassroomLinkMethod.NONE
    assert len(r.candidates) == 3


def test_email_local_part_signal():
    """Email local part should contribute to matching."""
    r = match(RosterEntry("u", "", "komin.ivan_ip44@edu.kpi.ua"), C[:2])
    assert r.student_id == 2


def test_apostrophes_and_yi():
    """Apostrophes should be stripped; 'ї' should transliterate correctly."""
    result = name_tokens("Мар'яна Їжак")
    assert result == ["mariana", "yizhak"]
