"""Test process defaults so importing the app does not require a production env."""

import os
import tempfile

_db_dir = tempfile.mkdtemp(prefix="terradeed-tests-")
os.environ.setdefault(
    "ADMIN_SECRET",
    "test-admin-secret-must-be-32-characters-long",
)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_dir}/terradeed.db")
