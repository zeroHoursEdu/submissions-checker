## MODIFIED Requirements

### Requirement: Subject-per-repo packaging with custom image

A subject SHALL be packaged as a self-contained git repository containing its `config.yml`, per-assignment check scripts, fixtures, assignment documents, the `checklib` source, and a `Dockerfile` that builds the subject's runtime image. The `config.yml` `image:` field SHALL reference that image. The repository SHALL be loadable by the engine without engine code changes, by zipping the repository and uploading it through the `POST /teacher/subjects/apply-config` endpoint, which extracts it to `plugins_dir/<subjectCode>/`. The repository SHALL include documentation for building the image and running checks locally.

#### Scenario: Subject repo builds a runnable image

- **WHEN** a maintainer runs `docker build` against the subject repo's `Dockerfile`
- **THEN** an image is produced containing the interpreter, the subject's third-party dependencies (e.g. `pandas`, `openpyxl`), and `checklib` installed into site-packages

#### Scenario: Engine loads the repo via upload, not a startup scan

- **WHEN** a teacher zips the subject repo and uploads it through `POST /teacher/subjects/apply-config`
- **THEN** the config-apply service upserts the subject and its assignments from the ZIP's `config.yml`, extracts the full repo tree to `plugins_dir/<subjectCode>/`, and checks run using the image named in `image:` — no engine startup scan is involved at any point
