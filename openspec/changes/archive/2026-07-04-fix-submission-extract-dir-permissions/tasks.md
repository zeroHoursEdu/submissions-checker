## 1. Permission widening

- [x] 1.1 In `src/submissions_checker/workers/tasks/check_tasks.py`, after `safe_extract(zf, extract_path)`, walk `extract_path` and `os.chmod` every directory to `0o755` and every file to `0o644`, mirroring the `output_dir` precedent in `docker_sandbox.py`.

## 2. Verification (user-performed)

- [x] 2.1 Ask the user to resubmit work as the test student for the previously-failing `pythonBasics` assignment and report whether the submission now reaches `TESTING`/graded instead of failing with `PermissionError`.
