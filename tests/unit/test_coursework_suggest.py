"""Guessing which Classroom coursework belongs to which lab."""

from submissions_checker.services.google.coursework_match import suggest

LABS = [
    (1, "ЛР №1 — Отримання та відображення системної інформації"),
    (2, "ЛР №2 — Графічний інтерфейс користувача (GDI)"),
    (4, "ЛР №4 — Клієнт-сервер: комунікація через сокети"),
]
WORKS = [
    ("w4", "4. Клієнт-серверна архітектура додатку. Метод комунікації: сокети"),
    ("w1", "1. Отримання та відображення системної інформації"),
    ("w2", "2. Графічний інтерфейс користувача"),
    ("x", "Оголошення про захист"),
]


def test_matches_by_lab_number():
    assert suggest(LABS, WORKS) == {1: "w1", 2: "w2", 4: "w4"}


def test_number_written_differently():
    works = [("a", "Лабораторна робота 1"), ("b", "Lab 2: GUI")]
    assert suggest(LABS[:2], works) == {1: "a", 2: "b"}


def test_two_courseworks_with_the_same_number_fall_back_to_title():
    works = [
        ("a", "1. Отримання та відображення системної інформації"),
        ("b", "1. Додаткове завдання (не оцінюється)"),
    ]
    assert suggest(LABS[:1], works) == {1: "a"}


def test_no_number_uses_title_similarity():
    labs = [(9, "Курсова робота")]
    works = [("a", "Курсова робота: звіт"), ("b", "Оголошення")]
    assert suggest(labs, works) == {9: "a"}


def test_nothing_similar_is_left_unlinked():
    assert suggest([(9, "Курсова робота")], [("b", "Оголошення про захист")]) == {}


def test_a_coursework_is_suggested_once():
    labs = [(1, "ЛР №1 — Звіт"), (11, "ЛР №1 — Звіт (повторно)")]
    assert list(suggest(labs, [("a", "1. Звіт")]).values()) == ["a"]


def test_taken_courseworks_are_skipped():
    assert suggest(LABS[:1], WORKS, taken={"w1"}) == {}
