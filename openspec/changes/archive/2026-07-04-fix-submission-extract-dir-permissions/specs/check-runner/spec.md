## ADDED Requirements

### Requirement: Extracted submission tree is readable by the non-root sandbox user

When the production check task extracts a submitted ZIP archive before running it through the
sandbox, every directory and file in the extracted tree SHALL be traversable and readable by the
non-root user the sandbox container runs as, regardless of the permission bits stored in the
original archive entries.

#### Scenario: Submission file is readable inside the sandbox

- **WHEN** a student's submitted ZIP is extracted and bind-mounted read-only at `/submission`
  inside the sandbox container, and the sandbox image runs the check as a non-root user
- **THEN** that user SHALL be able to stat and read every extracted file without a
  `PermissionError`, regardless of the mode bits the ZIP entries originally carried

#### Scenario: Nested submission directories are traversable

- **WHEN** the submission ZIP contains files inside nested subdirectories
- **THEN** the non-root sandbox user SHALL be able to traverse every intermediate directory to
  reach those files
