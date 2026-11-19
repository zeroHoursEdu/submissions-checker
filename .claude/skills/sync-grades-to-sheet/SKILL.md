---
name: sync-grades-to-sheet
description: Copy the lab grades from the production DB into the teacher's Google Sheet "DistributedSoftwareC++ 2026 Schedule" (sheet «Студенти») — use when the teacher says "перенеси оцінки в таблицю", "онови таблицю".
---

# Sync grades DB → Google Sheet

Sheet: `1FWa63lgt20UGdx26V0scDpuxeuk2_9eKTOxhVXu_Sq0` (https://docs.google.com/spreadsheets/d/1FWa63lgt20UGdx26V0scDpuxeuk2_9eKTOxhVXu_Sq0).
Tabs: «Студенти» (sheetId 0), «шкала оцінок» (grading scale, don't touch).
Load the `anthropic-skills:google-workspace` skill and its `references/sheets.md` first; tools are deferred, load
`get_values`, `update_values`, `get_spreadsheet` with ToolSearch in one call.

## Layout of «Студенти» (verify with a read before writing; it may change)
Row 1 headers, students in rows 2..41 (alphabetical by group, not by name). Columns:
`A №, B Група, C ПІБ ("Last First Patronymic"), D Бригада, E–G dates, H ЛБ№1, I А-1, J–L dates, M ЛБ№2, N ЛБ№3,
O ЛБ№4, P ЛБ№5, Q А-2, R–S dates, T ЛБ№6, U ЛБ№7, V Залік`.
- **Залік (V) and А-1/А-2 are formulas: never write them.** Залік = sum of labs and recalculates itself.
- Lab columns are not contiguous: write three ranges per sync: `H2:H41`, `M2:P41`, `T2:U41`.
- Send numbers as numbers; `null` leaves a cell untouched (use it for students without a grade), `""` would clear it.
- The sheet has existing values from earlier syncs; DB is the source of truth, but report any cell where DB
  differs from what the sheet had before overwriting (read first, diff, then write).

## Procedure
1. Pull grades (`prod-grades-queries`, first query): per student `g1,…,g7`.
2. Read `'Студенти'!A1:V45` (rows in sheet order, names in C).
3. Map DB "First Last" → sheet "Last First …" by surname + first name (normalise `'` vs `` ` `` in e.g. Дар'я/Дар`я).
   Print unmatched names; there should be none.
4. Build arrays in sheet row order, write the three ranges.
5. Re-read `C1:V41`, check each student sits in the right row and Залік recalculated; tell the teacher which
   students stay empty (no DB grade). Link the sheet by title in the reply.

A helper that builds the payload lives only in the scratchpad of past sessions; it is a 30-line dict mapping, easy to redo.
