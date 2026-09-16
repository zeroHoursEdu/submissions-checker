# javaProgramming2 — Task Text Proposals & Automation Notes

Compiled 2026-07-07 while building automated grading (checks, no quizzes this pass) for a
brand-new Java subject: 7 labs (`lab0`-`lab6`) × 70 total variants, cross-checked against the
source PDFs (`Лабораторна робота №0-6....pdf`). This subject didn't exist before this pass —
config, `javalib` (a Java-flavored port of pythonBasics' `checklib`), and every check.py were
built from scratch. Plugin repo root:
`/home/vampir/petProjects/testinSubmissionChecker/Лабораторні_Програмування_Частина_2/`.
Verified solutions live in
`/home/vampir/petProjects/testinSubmissionChecker/javaProgramming2/labN/`.

## Scope confirmed with the project owner before building

1. **Only the 7 Ukrainian-titled labs (0-6)** — a separate 8-file "Software Engineering"
   series (`se_s3_l2.pdf`...`se_s3_l9.pdf`, covering UML diagrams via ArgoUML/Umbrello,
   JavaDoc generation, and ANT builds) was explicitly excluded. Those tasks ask for
   diagrams, generated documentation, and a build-tool target — none of which is verifiable
   by running code and inspecting stdout. **Recommendation if this track is ever wanted:**
   treat it as 100% manual/portfolio review (rubric-based), not an automated-checker subject;
   at most, a checker could confirm a JavaDoc HTML output directory exists and is non-empty,
   which is a very weak signal.
2. **"Додаткова лабораторна робота №1"** (bonus: Stream API/lambdas, redo lab3 or lab6 using
   `Collection.stream()`) was also out of scope for this pass — not built. It would need a
   structural check (confirm `.stream(...)` pipeline usage, ≥3 chained operations) that
   output-only checking can't provide, similar to the lab4 gap below.
3. **Variant model**: each lab's `C_n = record-book-number % n` derivation was replaced with
   **one variant per distinct value of each modulus that changes task logic**, not the full
   cross product (which would have been 385 raw combinations for lab1 alone). Moduli that only
   pick a Java primitive type (byte/short/int/.../double) were fixed to a single representative
   type (almost always `int`) throughout, since they don't affect correctness. Full derivation
   per lab is in each `assignments/labN/AMENDMENTS.md`.

## Summary by lab

| Lab | Topic | Variants (automated/total) | review_mode | Notes |
|---|---|---|---|---|
| 0 | Оператори (operators) | 5/5 | `tests_only` | Numeric formula + exception handling. |
| 1 | Масиви (arrays/matrices) | 11/11 | `tests_only` | Two independent op-axes covered representatively. |
| 2 | Рядки (strings) | 17/17 | `tests_only` | All 17 distinct text algorithms, none reducible. |
| 3 | Класи (classes/OOP) | 11/11 | `tests_then_teacher` | Pinned contract per domain (see below) — task text needs this. |
| 4 | Композиція класів (composition) | 7/17 (representative) | `tests_only` | Reuses lab2's algorithms via composed classes; structural use not verified. |
| 5 | Наслідування/поліморфізм (inheritance) | 12/13 | `tests_then_teacher` | Variant 12 (toy room, budget-allocation) stays manual — no single correct answer. |
| 6 | Колекції (custom collections) | 6/6 | `tests_only` | Pinned command protocol + minimal method subset, not full `List`/`Set` compliance. |

`review_mode: tests_then_teacher` was kept (rather than switched to `tests_only`) for lab3
and lab5 as a conservative default for this first pass of a brand-new subject — a human
still spot-checks Java Code Conventions/Google Java Style Guide compliance and the "pushed to
GitHub" requirement, neither of which any checker here verifies. This is a judgment call, not
a technical requirement; `tests_only` would work equally well technically for lab3 (all 11
variants are automated) if the team prefers faster feedback over the manual style check.

## Lab 3 (Classes) — the task text needs a real I/O contract added

Like pythonBasics' lab8, this lab describes OOP modeling with **no I/O contract at all** — not
even a worked example beyond the tie-break footnote. Proposed addition for section 3.3 (one
general template, reused across all 11 domains):

> Програма зчитує з клавіатури: перший рядок — кількість об'єктів N; далі N рядків — по
> одному об'єкту на рядок, поля через кому; останній рядок — цільовий об'єкт для пошуку в
> тому самому форматі. Вивід: N рядків відсортованого масиву (той самий CSV-формат), потім
> "Знайдено на позиції K" (рахуючи з 1, у відсортованому масиві) або "Не знайдено".

Full field lists for all 11 domains (car, plane, boat, clothing, cosmetics, sports equipment,
furniture, student, NPC, institution, building block) plus 3 fully worked input/output
examples are in `assignments/lab3/AMENDMENTS.md` and this repo's proposal doc equivalent —
ready to paste into the official task sheet if adopted.

## Lab 5 (Inheritance/Polymorphism) — same gap, 13 domains

Same situation as lab3 but with real inheritance hierarchies (base class + ≥3 subclasses per
domain, polymorphic total calculation). A per-domain pinned contract (input format, which
field sorts/searches, subclass-specific fields) was drafted for all 13 domains — see
`assignments/lab5/AMENDMENTS.md`. **Domain 12 (kids' room, fixed toy count within a budget)
was deliberately left unpinned and routes to manual review** — it's an allocation/selection
problem with many equally-valid solutions, and any pinned "correct" answer would be an
invented algorithm not present in the source material at all (as opposed to domains 0-11,
where the pinned contract just fills in an unspecified but singular I/O shape).

## Lab 4 (Composition) & Lab 2 (Strings) — structural-enforcement gap

Same class of gap as pythonBasics' lab4 (Python "must define a function"): lab4's entire
point is that letters/words/sentences/text must be modeled as **separate composed classes**,
but `check.py` only inspects compiled-program stdout — a submission that solves the same
algorithm with a single flat `Main` class (i.e., resubmits a correct lab2 solution) would score
100% despite missing the lab's actual learning objective entirely.

**Proposal (not implemented):** a source-inspection validate step — e.g. a regex/simple-parser
scan for `class\s+\w+` declarations — confirming at least 4 distinct classes exist besides
`Main`, as a soft/partial-credit signal layered on top of the behavioral check. This is the
same backlog item flagged for pythonBasics' lab4 (AST-based "was a function actually defined"
check) generalized to "were separate classes actually defined." Neither is implemented in
either subject; both are genuine, repeatable gaps in output-only automated grading of
structural/design requirements, worth addressing once for both subjects if this becomes a
priority.

## Recurring platform-adjacent gotcha: common + variant `check_command` set to the same script

While merging lab2's config fragment into the master `config.yml`, its `common.sandbox` block
had `check_command: assignments/lab2/check.py` set **in addition to** every variant's own
identical `check_command`. Since `check_core.run_check` runs `common_check` and `variant_check`
as two separate sandbox invocations and merges their test lists, this would have silently
run the exact same script twice per submission and doubled the score denominator (100/100
would have reported as 200/200) — numerically still 100%, so it wouldn't have surfaced as an
obviously wrong grade, just wasted compute and corrupted the score's meaning for any future
partial-credit scenario. **This is the second time this exact mistake has occurred** (also
found and fixed in pythonBasics' lab6 during the previous pass) — see
`docs/known_bugs.md` #13, which already proposes a platform-level guard: `resolve_check_plan`
(`services/check_core.py`) could warn or reject when `common_check == variant_check` (identical
resolved script paths), since running the same script twice is never intentional. Given this
has now happened twice independently across two different subjects, this platform guard is
worth prioritizing over documentation alone.

## What was NOT built (explicitly out of scope, not silently dropped)

- The `se_s3_l2`-`se_s3_l9` "Software Engineering" track (UML/JavaDoc/ANT) — see scope note above.
- "Додаткова лабораторна робота №1" (Stream API bonus lab).
- Quizzes for any of the 7 labs (unlike pythonBasics, which got a knowledge-check quiz per
  lab) — not requested for this pass; the same `javalib`/config pattern from pythonBasics
  would carry over directly if quizzes are wanted later (same `quiz:` YAML block, same
  `anti_cheat` settings, just Java-appropriate conceptual questions per lab topic).
- Full `java.util.List`/`Set` interface compliance testing in lab6 (only a pinned minimal
  method subset is graded — see `assignments/lab6/AMENDMENTS.md`).
- Code style (Javadoc, Java Code Conventions/Google Java Style Guide) and "pushed to GitHub"
  requirements, present in nearly every lab's task text — inherently outside what a behavioral
  checker can verify; flagged per-lab for manual spot-check.
