## ADDED Requirements

### Requirement: ZIP upload extracts the full plugin tree to disk

A successful config apply SHALL extract the entire uploaded ZIP archive (not only `config.yml`
and the images/content files already referenced by the diff plan) to `plugins_dir/<subjectCode>/`
on disk, atomically replacing any previously extracted tree for that subject. This makes the
`config-apply` endpoint sufficient on its own to make a subject checkable — the extracted tree is
exactly what `check_tasks.py` mounts read-only into the sandbox at check-execution time.

The extraction SHALL happen after the DB transaction in the apply sequence commits successfully,
and SHALL be atomic from the perspective of any concurrently running check (a check reading
`plugins_dir/<subjectCode>/` at any point during the swap SHALL see either the complete previous
tree or the complete new tree, never a partial one).

The duplicate-ZIP short-circuit (matching `content_hash`) SHALL also verify that
`plugins_dir/<subjectCode>/` exists on disk before skipping extraction; if the hash matches but
the directory is missing (e.g. a prior extraction failed after its DB commit), the system SHALL
still perform the extraction rather than treating the apply as a no-op.

#### Scenario: New subject's checker code is extracted

- **WHEN** a teacher uploads a ZIP creating a new subject, and the DB transaction commits
- **THEN** the full ZIP contents (checker scripts, fixtures, `config.yml`, everything) are
  extracted to `plugins_dir/<subjectCode>/`, and a check run against that subject can resolve its
  `/plugin` mount without any manual file placement

#### Scenario: Re-upload replaces stale files

- **WHEN** a teacher re-uploads a ZIP for an existing subject that removes an assignment's
  `check.py` present in the previous version
- **THEN** after the apply completes, `plugins_dir/<subjectCode>/` no longer contains that
  removed file — the on-disk tree matches the new ZIP exactly, not a merge of old and new

#### Scenario: Concurrent check sees a consistent tree

- **WHEN** a check task resolves `plugin_dir` for a subject at the same moment a new apply is
  swapping that subject's extracted tree
- **THEN** the check task SHALL see either the fully-old or fully-new tree, never a directory
  missing files or containing a mix of both versions

#### Scenario: Retry after a failed extraction re-extracts instead of no-op'ing

- **WHEN** an apply's DB transaction commits but the subsequent disk extraction fails or is
  interrupted, and the same ZIP is uploaded again
- **THEN** the system SHALL detect that `plugins_dir/<subjectCode>/` is missing despite the
  matching `content_hash`, and SHALL perform the extraction rather than skipping it as unchanged
