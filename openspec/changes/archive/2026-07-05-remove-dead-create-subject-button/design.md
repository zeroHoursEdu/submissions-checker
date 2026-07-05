## Context

Trivial cleanup — a dead link and its orphaned target template, both unreferenced by any working route. No architectural decision needed.

## Goals / Non-Goals

**Goals:** remove the dead UI element and orphaned template; document the intended single-path (upload-only) creation model so it doesn't regress.

**Non-Goals:** building a real manual create-subject form — explicitly not wanted (config upload is the intended sole path per the user's own instruction and the existing `subject-management` spec).

## Decisions

**Delete, don't comment out.** No caller, no route, no test references `teacher_subject_form.html` or `/teacher/subjects/create` — dead code with zero migration risk.

## Risks / Trade-offs

None — pure removal of unreachable code.
