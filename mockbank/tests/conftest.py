"""Shared test setup."""

import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import create_app


@pytest.fixture()
def app():
    """A fresh mock bank for every test, with a clean state."""
    store.reset_all()
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client
