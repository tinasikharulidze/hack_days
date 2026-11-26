"""Shared test setup.

The tests need a bank to talk to, so they use the mock bank, which lives in the
sibling folder ../mockbank. We add it to the import path here. To keep the mock
somewhere else, set the environment variable MOCKBANK_PATH.
"""

import os
import sys
from pathlib import Path

import pytest

MOCKBANK = Path(os.environ.get("MOCKBANK_PATH", Path(__file__).resolve().parents[2] / "mockbank"))
if not (MOCKBANK / "app").is_dir():
    raise RuntimeError(f"Mock bank not found at {MOCKBANK}. Set MOCKBANK_PATH to the mockbank folder.")
sys.path.insert(0, str(MOCKBANK))

from app import store                  # noqa: E402  (these come from the mock bank)
from app.main import create_app        # noqa: E402


@pytest.fixture()
def app():
    """A fresh mock bank for every test, with a clean state."""
    store.reset_all()
    return create_app()
