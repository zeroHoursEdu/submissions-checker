## Context

Two independent things currently read `plugins_dir`:

1. **Startup registration** (`main.py:60-65`, `PluginLoader.load_all`): scans every subdirectory
   with a `config.yml`, upserts `Subject`/`SubjectsAssignment` rows with `owner_id = None`.
2. **Runtime code resolution** (`check_tasks.py:93-94`, `docker_sandbox.py`): every check run
   resolves `plugin_dir = Path(settings.host_plugins_dir or settings.plugins_dir) /
   subject_code` and bind-mounts it read-only into the sandbox at `/plugin`, regardless of which
   path created the `Subject` row.

`ConfigApplyService.apply()` already extracts the uploaded ZIP to a `tempfile.TemporaryDirectory`
twice (once to read `subjectCode`/hash, once to compute the diff/collect S3 files) and discards it
when the `with` block exits — it never persists the tree. So today, an API/UI-created subject has
a DB row and S3 assets but no `/plugin` mount target; only the manual-symlink path happens to also
put code where (2) expects it.

## Goals / Non-Goals

**Goals:**
- One creation path (API/UI upload) that is sufficient by itself to make a subject checkable —
  DB row, S3 assets, and on-disk checker code all come from the same ZIP.
- No behavior change to (2), the runtime mount-resolution logic — it already points at the right
  place once (1) also writes there.
- Re-uploading a changed ZIP for an existing subject fully replaces the on-disk tree, so stale
  files from a previous version (e.g. a removed assignment's `check.py`) don't linger.

**Non-Goals:**
- Reworking the sandbox mount mechanism itself (`docker_sandbox.py`, DinD host-path resolution) —
  unaffected; it already works from `plugins_dir`/`host_plugins_dir` regardless of who wrote there.
- Backfilling `owner_id` or migrating on-disk directories for subjects created by the old startup
  scan before this change ships (`docs/known_bugs.md` #1) — those keep working exactly as before
  (their `plugins/<code>/` directory is untouched, symlinks keep resolving) until someone
  re-uploads them via the API, at which point ownership fixes itself as a side effect of the
  normal apply flow.
- Any change to `SubjectPluginConfig.zip_data` storage — already stores the raw ZIP bytes;
  unrelated to whether the tree is also extracted to disk.

## Decisions

**Extract to disk as a new step in `_execute_plan`, after the DB transaction commits.** The
existing apply sequence is S3-upload → DB-transaction → S3-cleanup (each step already documented
in `openspec/specs/subject-config-apply/spec.md` as tolerating some risk — e.g. "orphan S3
objects may remain" if DB commit fails after S3 upload). Adding the disk-extraction step *after*
DB commit means: if extraction fails, the DB has already recorded the new version but the old
code is still live on disk — an operator can retry the upload. The alternative (extract-before-DB)
risks the opposite and strictly worse failure: new code live on disk for a subject whose DB row
still reflects the previous version, silently changing grading behavior for a config nobody
approved. Committing DB first, then swapping code, matches "DB is the source of truth, disk is a
derived cache" and is the safer failure direction.

**Atomic replace via extract-to-sibling-then-`os.replace`, not extract-in-place.** Extract the ZIP
to `plugins_dir/.tmp-<subjectCode>-<uuid-ish>/` (a sibling temp dir on the same filesystem), then
`os.replace()` it onto `plugins_dir/<subjectCode>/`. `os.replace` is atomic on POSIX for
directory-to-directory rename on the same filesystem, so a check run that resolves `plugin_dir`
mid-swap either sees the fully-old or fully-new tree, never a half-extracted one. Extracting
in-place (wiping then repopulating the live directory) would leave a window where an in-flight
check's bind-mount briefly sees a missing or partial `/plugin` tree.

**Reuse the existing `safe_extract()` (already used for the transient tmp-dir extraction) for the
persisted extraction too**, rather than a second extraction routine — it already guards against
zip-slip/symlink/size abuse, and the ZIP bytes are identical between the transient and persisted
extraction (the whole point is they should produce the same tree).

**`ConfigApplyService` takes `plugins_dir: Path` as an explicit constructor argument, not a
`get_settings()` call inside the service.** Keeps the service's dependencies explicit and testable
(unit tests pass a `tmp_path` instead of needing to monkeypatch global settings), matching how
`storage: StorageService | None` is already injected.

**Delete `PluginLoader` outright rather than leaving it unused.** No other caller exists once the
`main.py` startup call is removed; keeping dead code around invites someone reintroducing the
dual-path confusion this change is meant to remove.

**`_check_duplicate`'s hash short-circuit also checks that `plugins_dir/<subjectCode>/` exists on
disk, not just the DB hash — but self-heals by extracting in place rather than falling through to
the full apply pipeline.** The first version of this checked on-disk presence and returned `None`
(not-a-duplicate) on a miss, letting the caller run the entire plan/execute flow again — that
crashes on the `(subject_id, content_hash)` unique constraint, since the content (and therefore
the hash) hasn't changed and would try to insert a second `SubjectPluginConfig` row with the same
hash. The correct fix: `_check_duplicate` calls `_extract_plugin_tree` itself when the hash
matches but the directory is missing, then still returns the normal `unchanged` result — no new
DB row, tree re-extracted.

## Risks / Trade-offs

- [Risk] A subject's checker code briefly diverges from its DB-recorded version if the process
  crashes between DB commit and the disk swap. → Mitigation: narrow window; the strengthened
  duplicate check above means a retry of the same ZIP re-extracts instead of silently no-op'ing,
  so the divergence self-heals on the next apply of that same content.
- [Risk] Extracting untrusted ZIPs to a real, persistent, bind-mounted-into-Docker directory is a
  larger blast radius than the previous transient tmp-dir extraction. → Mitigation: `safe_extract`
  already rejects path traversal, symlinks, and oversized archives before any write; this change
  doesn't loosen that, it just makes the destination durable instead of a `TemporaryDirectory`.
- [Risk] E2e tests currently depend on the startup scan to register `e2e_test`; removing the scan
  breaks e2e setup unless replaced. → Mitigation: explicit task to seed `e2e_test` via a real ZIP
  upload through `apply-config` in test setup — this also gives the new full-tree-extraction path
  its own integration coverage.
