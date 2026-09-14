"""Test-environment defaults, applied before any application module is imported.

`core.templates` calls `get_settings()` at import time, and `Settings` requires
`SECRET_KEY` and `DATABASE_URL`. Any test module that imports a route therefore
fails *at collection* when those are unset — which is every checkout that has no
local `.env`, including CI. The suite used to pass only because a developer's
`.env` happened to be sitting in the working directory.

This file lives at the repository root rather than in `tests/` because pytest
loads the rootdir `conftest.py` before anything under `testpaths`, so the values
are in place before the first application import.

`setdefault`, not assignment: a real environment wins. These are placeholders to
make imports succeed, not test configuration. Tests that touch a database get a
real URL from the `test_settings` fixture, which builds `Settings` explicitly
from the testcontainer.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-minimum-32-chars-long")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
