# pythonBasics — Task Text Proposals & Automation Notes

Compiled 2026-07-06 while building full automated grading (checks + quizzes) for all 9
`pythonBasics` labs × 10 variants each, cross-checked against the source PDFs
(`ЛР_1.pdf`…`ЛР_9.pdf`). Every lab's variants are now covered by automated checks or, where
that's genuinely not possible without changing the task, routed to manual teacher review —
see `pythonBasicSubject/assignments/labN/AMENDMENTS.md` for the full per-variant reasoning and
`/home/vampir/petProjects/testinSubmissionChecker/labN/` for verified valid/invalid solutions
and self-test results.

Per the instruction to prefer not touching the original tasks: everywhere below, "pinned
contract" means a concrete input/output convention the checker needs that the PDF leaves
implicit (e.g. exact file names, which line holds which input) — these are additive
clarifications, not task redesigns, and in every case except lab8 and lab9-variant-7 the
underlying task itself is unchanged.

---

## Summary by lab

| Lab | Automated variants | Manual variants | review_mode | Notes |
|---|---|---|---|---|
| 1 | 10/10 | 0 | `tests_then_quiz` | Already complete; only fixed a stale word in variant 10's description and added 3 quiz questions. |
| 2 | 10/10 | 0 | `tests_then_quiz` (was `tests_only`) | New quiz added. |
| 3 | 10/10 | 0 | `tests_then_quiz` (was `tests_then_teacher`) | Variants 3/8/10 newly automated. |
| 4 | 10/10 | 0 | `tests_then_quiz` (was `tests_only`) | New quiz added; see function-definition gap below. |
| 5 | 10/10 | 0 | `tests_then_quiz` (was `tests_then_teacher`) | Variants 5/6/7/10 newly automated. |
| 6 | 10/10 | 0 | `tests_then_quiz` (was `tests_then_teacher`) | Variants 4/5/8/10 newly automated (v8 uses an invented, needs-sign-off course list). |
| 7 | 10/10 | 0 | `tests_then_quiz` (was `tests_then_teacher`) | Variants 3/5/6/8/9 newly automated (behavior-based, not message-text). |
| 8 | 5/10 | 5 (1,4,5,7,9) | `tests_then_teacher` (unchanged) | See dedicated section below — needs task-text amendments to automate further. |
| 9 | 10/10 | 0 | `tests_then_quiz` (was `tests_then_teacher`) | Variant 7 replaced (was a verbatim duplicate of variant 2); variants 5/6 newly automated. |

All `review_mode` switches to `tests_then_quiz` were made **only** for labs where zero variants
remain manual — the quiz is now the sole post-test checkpoint, replacing a teacher-review step
that had become vestigial once every variant got a real automated check. This is a judgment
call, not a technical requirement; see "Platform gap" below for why it was an all-or-nothing
choice.

---

## Lab 8 (ООП) — the one lab needing real task-text changes

Every other lab's ambiguity was resolved by picking the most natural reading of the existing
PDF wording. Lab 8 is different: the PDF describes *class modeling* with no input/output
contract at all (the worked example hardcodes constructor arguments and never calls `input()`),
so nothing can be graded automatically without adding an explicit contract. Proposed additions
below are meant to be inserted into section 8.8 of the task text for variants 2, 3, 6, 8, 10;
variants 1, 4, 5, 7, 9 are recommended to stay manual (pure data storage or open-ended CRUD —
formalizing a command protocol for them would change what the exercise is teaching).

**Variant 2 (середній бал):**
> Програма зчитує з клавіатури один рядок з оцінками студента, розділеними пробілом
> (наприклад: `5 4 3 5`). Створити об'єкт класу, обчислити середній бал за допомогою методу
> класу та вивести його останнім рядком виводу (без додаткового тексту після числа).

**Variant 3 (вік за роком народження):**
> Програма зчитує з клавіатури два цілих числа, кожне на окремому рядку: спочатку **поточний
> рік**, потім **рік народження студента**. Створити об'єкт класу, обчислити вік як різницю
> цих чисел і вивести його останнім рядком виводу. **Використання `datetime.now()`
> заборонено** — поточний рік має надходити з вводу, а не визначатися автоматично (це
> унеможливлює детерміновану перевірку).

**Variant 6 (вартість покупки):**
> Програма зчитує з клавіатури рядки виду `назва ціна кількість` (через пробіл), по одному
> товару на рядок; порожній рядок означає кінець вводу. Створити об'єкт(и) класу для кожного
> товару, обчислити загальну вартість покупки та вивести її останнім рядком виводу.

**Variant 8 (площа і периметр прямокутника):**
> Програма зчитує з клавіатури два числа, кожне на окремому рядку: спочатку **ширину**, потім
> **висоту** прямокутника. Створити об'єкт класу, обчислити площу та периметр за допомогою
> методів класу і вивести обидва значення — **спочатку площу, потім периметр** — кожне на
> окремому рядку.

**Variant 10 (середнє значення групи оцінок):**
> Програма зчитує з клавіатури один рядок з оцінками групи, розділеними пробілом. Створити
> об'єкт класу, обчислити середнє значення групи оцінок методом класу та вивести його
> останнім рядком виводу.

Why 1, 4, 5, 7, 9 stay manual: variants 1 (Student name/surname) and 7 (employee data) are
pure storage with nothing to compute or verify — an automated check could only confirm "didn't
crash," which doesn't test OOP understanding. Variant 4 (accumulating grades into a list) and
variants 5/9 (library and calendar CRUD) would need an invented command protocol substantial
enough to change the exercise from "model a class" into "parse a command language," which
seems like the wrong trade-off for this lab's learning goal.

The lab8 quiz (OOP concepts — `class`/`__init__`/`self`, instance vs. class attributes,
encapsulation) was still written and is recommended for all students regardless of which
variant they're assigned, since the concepts it tests are variant-independent. See "Platform
gap" immediately below for how it actually reaches students under `tests_then_teacher`.

---

## Platform gap surfaced by lab8: no combined "teacher review + quiz" mode

`_advance_after_tests` (`src/submissions_checker/workers/tasks/check_tasks.py`) treats
`review_mode` as one mutually-exclusive dispatch. There is no mode that both routes to a human
teacher **and** auto-sends a quiz — `tests_then_teacher` never looks at a `quiz:` block at all.

For lab8 specifically, this means: with `review_mode: tests_then_teacher` (needed to preserve
manual grading for variants 1/4/5/7/9), the quiz **does not auto-fire** after tests pass. It
only becomes reachable via the existing `POST /teacher/submissions/{id}/review` → `action=approve`
path, which checks the assignment's config for a quiz and transitions to `QUIZ_SENT` instead of
`COMPLETED` if one exists (`teacher_send_quiz`, confirmed working end-to-end during testing —
see the "teacher approves → QUIZ_SENT" step in the verification results). So the quiz is
reachable today, just teacher-gated rather than automatic, for every lab8 submission regardless
of variant.

**Suggested follow-up for `submissions-checker` itself** (added to
`docs/missing_features.md` too): a `tests_then_teacher_then_quiz` review mode (or a
`send_quiz_after_teacher_approval: true` flag alongside `tests_then_teacher`) would let a lab
keep mandatory human review for some variants while still auto-prompting the quiz on approval,
instead of relying on the teacher to remember it's there.

---

## Other flagged gaps (not fixed, out of scope for this pass)

**Lab 4 — function-definition isn't structurally enforced.** The PDF is explicit that
students must implement the answer as a callable function and invoke it for several values,
but the checker only inspects stdout. A submission with zero `def` statements that computes the
right answer inline would score 100%. Fixing this would need an AST-based structural check
(e.g. confirm at least one `ast.FunctionDef` with a `return`) layered on top of the existing
`last_number` output check — proposed as a `checklib` backlog item, not implemented here.

**Lab 6 variant 8 — invented course list needs teacher sign-off.** The PDF's variant 8 ("create
a JSON of faculty courses") has no worked example to pin against, unlike variant 2's new-student
record. A plausible 4-course list for faculty ФІТ was invented to make it testable
(`courses.json` = `["Алгоритми та структури даних", "Бази даних", "Об'єктно-орієнтоване
програмування", "Комп'ютерні мережі"]`) — this should be reviewed against the real curriculum
before shipping to students, or swapped for whatever list a teacher already uses.

**Lab 9 variant 7 replaced.** Variants 2 and 7 were verbatim duplicates in the PDF (both "find
email addresses in text" — confirmed by reading section 9.8, not a config-flattening artifact).
Variant 7 was changed to hashtag extraction (`#\w+`) instead, per the existing amendment's own
suggestion. New variant 7 description: *«Написати програму для пошуку всіх хеш-тегів (слів, що
починаються з символу '#') у тексті. Ввід: текст.»*

**Lab 3/5/6/7/9 — several pinned I/O contracts invented for automation** (exact file names like
`data.txt`/`input.txt`, which stdin line holds which value, a fixed dict for lab7 variant 6,
etc.) are documented in full in each lab's `AMENDMENTS.md` and are already folded into
`config.yml`'s variant descriptions so students see them, not just the checker. Nothing left
open in these labs — see the per-lab `AMENDMENTS.md` for the exact wording if the official PDF
task sheets are ever revised to match.
