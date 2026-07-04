## Context

`docker_sandbox.py:50-54` already documents and fixes this exact class of bug for `/output`:

```python
with tempfile.TemporaryDirectory(prefix="sandbox_output_") as output_dir:
    # Subject images drop to a non-root user (e.g. uid 10001), so the bind-mounted
    # /output (a host temp dir, created 0700) must be writable by that user for the
    # check to emit result.json. Widen perms on this ephemeral dir only.
    os.chmod(output_dir, 0o777)
```

`check_tasks.py`'s submission-extraction directory (`tempfile.TemporaryDirectory(prefix=
"submission_")`, populated by `safe_extract()`, then mounted read-only at `/submission`) never
got the equivalent treatment. Unlike `/output` (a single empty directory needing only its own
mode widened before anything is written into it), `/submission` already contains an extracted
file tree by the time it's mounted — widening only the top-level directory's mode would let the
non-root user traverse into it but not necessarily read every file or traverse subdirectories,
since `zipfile.extractall()` preserves whatever Unix mode bits (if any) were stored in the
archive, which may be arbitrarily restrictive.

## Goals / Non-Goals

**Goals:**
- The sandbox's non-root user can traverse every directory and read every file under
  `/submission`, regardless of what permission bits the original ZIP entries carried.

**Non-Goals:**
- Changing `safe_extract()`'s safety checks (path traversal, symlink rejection, size limits) —
  unrelated and already correct.
- Touching the `/output` or `/plugin` mounts — already handled correctly.

## Decisions

**Recursively widen permissions on the whole extracted tree, not just the top-level directory.**
Because file-level mode bits from the archive aren't trustworthy (could be `0600` if the student
zipped on a strict umask, or anything else), only chmod-ing the top directory is insufficient —
an unreadable file deeper in the tree reproduces the same error one level down. Walk the tree
after extraction and set directories to `0o755` (traversable+readable+listable) and files to
`0o644` (readable). Read-only mount means no write bit is needed for the sandbox user.

**Do it in `check_tasks.py` right after `safe_extract()`, not inside `safe_extract()` itself.**
`safe_extract()` is also used by `config_apply.py` for teacher ZIP uploads, which have no
non-root-sandbox-mount requirement — keeping the permission widening local to the
check-execution call site avoids changing behavior for that unrelated caller.

## Risks / Trade-offs

- [Risk] Walking every file in a large submission tree before each check adds minor overhead. →
  Mitigation: submissions are already size-capped by `safe_extract()`'s `MAX_TOTAL_UNCOMPRESSED_BYTES`/`MAX_ENTRY_COUNT`, so the walk is bounded by the same limits.
- [Risk] `0o644`/`0o755` strips any executable bit a script in the submission may have needed. →
  Mitigation: the sandbox always invokes scripts via `tool /plugin/{script_path} /submission`
  (e.g. `python3 ...`), per `docker_sandbox.py:69` — the submission's own files are data read by
  the check script, never executed directly, so the executable bit is not needed.
