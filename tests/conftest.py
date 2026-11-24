"""Shared pytest fixtures for the Flask application."""

import os

os.environ.setdefault("FLASK_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.pop("VERCEL", None)

import pytest
from website.application import create_app


@pytest.fixture()
def app():
    application = create_app()
    application.config.update(
        TESTING=True,
        SECRET_KEY="pytest-session-secret",
        SESSION_COOKIE_SECURE=False,
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()
