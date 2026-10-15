"""
Tests for admin route security behaviour.
Covers: missing ADMIN_SECRET (soft start, 503 routes), wrong secret (401),
correct secret (200), and backward-compatible body field with deprecation warning.
"""

import os
import sys
import secrets
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# We must reload main.py for each ADMIN_SECRET variant, so import it dynamically.


@pytest.fixture
def client_with_secret(tmp_path):
    """Return a TestClient with a valid ADMIN_SECRET configured."""
    db_path = tmp_path / "test.db"
    secret = "x" * 32

    env = {
        "ADMIN_SECRET": secret,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "ANTHROPIC_API_KEY": "",
        "CDP_API_KEY_ID": "",
        "CDP_API_KEY_SECRET": "",
    }

    with patch.dict(os.environ, env, clear=False):
        # Remove cached main module so re-import picks up new env
        modules_to_remove = [k for k in sys.modules if k in ("main", "enrichment")]
        for m in modules_to_remove:
            del sys.modules[m]

        from main import app

        yield TestClient(app), secret


@pytest.fixture
def client_no_secret(tmp_path):
    """Return a TestClient with ADMIN_SECRET missing."""
    db_path = tmp_path / "test.db"

    env = {
        "ADMIN_SECRET": "",
        "DATABASE_URL": f"sqlite:///{db_path}",
        "ANTHROPIC_API_KEY": "",
        "CDP_API_KEY_ID": "",
        "CDP_API_KEY_SECRET": "",
    }

    with patch.dict(os.environ, env, clear=False):
        modules_to_remove = [k for k in sys.modules if k in ("main", "enrichment")]
        for m in modules_to_remove:
            del sys.modules[m]

        from main import app

        yield TestClient(app)


@pytest.fixture
def client_short_secret(tmp_path):
    """Return a TestClient with ADMIN_SECRET under 32 chars."""
    db_path = tmp_path / "test.db"

    env = {
        "ADMIN_SECRET": "short",
        "DATABASE_URL": f"sqlite:///{db_path}",
        "ANTHROPIC_API_KEY": "",
        "CDP_API_KEY_ID": "",
        "CDP_API_KEY_SECRET": "",
    }

    with patch.dict(os.environ, env, clear=False):
        modules_to_remove = [k for k in sys.modules if k in ("main", "enrichment")]
        for m in modules_to_remove:
            del sys.modules[m]

        from main import app

        yield TestClient(app)


# ---------------------------------------------------------------------------
# App starts even when ADMIN_SECRET is missing or short
# ---------------------------------------------------------------------------

def test_app_starts_without_admin_secret(client_no_secret):
    """The FastAPI app must import and boot when ADMIN_SECRET is empty."""
    r = client_no_secret.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_app_starts_with_short_admin_secret(client_short_secret):
    """The FastAPI app must import and boot when ADMIN_SECRET is too short."""
    r = client_short_secret.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Admin routes return 503 when ADMIN_SECRET is not configured
# ---------------------------------------------------------------------------

def test_create_key_503_when_no_secret(client_no_secret):
    r = client_no_secret.post("/admin/keys", json={"credits": 10}, headers={})
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()


def test_get_key_status_503_when_no_secret(client_no_secret):
    r = client_no_secret.get("/admin/keys/whatever", headers={})
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()


def test_create_key_503_when_short_secret(client_short_secret):
    r = client_short_secret.post("/admin/keys", json={"credits": 10}, headers={})
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Wrong secret returns 401
# ---------------------------------------------------------------------------

def test_create_key_401_with_wrong_secret(client_with_secret):
    client, _ = client_with_secret
    r = client.post(
        "/admin/keys",
        json={"credits": 10},
        headers={"x-admin-secret": "wrong-secret-thirty-two-chars-long"},
    )
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


def test_get_key_status_401_with_wrong_secret(client_with_secret):
    client, _ = client_with_secret
    r = client.get(
        "/admin/keys/prefix",
        headers={"x-admin-secret": "wrong-secret-thirty-two-chars-long"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Correct secret works
# ---------------------------------------------------------------------------

def test_create_key_200_with_correct_secret(client_with_secret):
    client, secret = client_with_secret
    r = client.post(
        "/admin/keys",
        json={"credits": 10},
        headers={"x-admin-secret": secret},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["credits"] == 10
    assert data["api_key"].startswith("td_sk_")


def test_get_key_status_200_with_correct_secret(client_with_secret):
    client, secret = client_with_secret
    # create a key first
    create_r = client.post(
        "/admin/keys",
        json={"credits": 10},
        headers={"x-admin-secret": secret},
    )
    prefix = create_r.json()["api_key"][:12] + "..."

    r = client.get(
        f"/admin/keys/{prefix}",
        headers={"x-admin-secret": secret},
    )
    assert r.status_code == 200
    assert r.json()["credits_remaining"] == 10


# ---------------------------------------------------------------------------
# Backward-compatible body field with deprecation warning
# ---------------------------------------------------------------------------

def test_create_key_accepts_deprecated_body_field(client_with_secret):
    client, secret = client_with_secret
    r = client.post(
        "/admin/keys",
        json={"credits": 10, "admin_secret": secret},
    )
    assert r.status_code == 200
    assert "warnings" in r.json()
    assert "deprecated" in r.json()["warnings"][0].lower()
