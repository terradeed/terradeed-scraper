"""Admin revoke endpoint: one key only, admin secret required, revoked keys refused.

No network: the paid-route check uses /scrape with the fetcher mocked.
"""

import logging
import secrets

from fastapi.testclient import TestClient

import main

ADMIN = {"x-admin-secret": main.ADMIN_SECRET}


def _insert_key(key, credits=100):
    with main.get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (main.hash_key(key), main.get_key_prefix(key), credits, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    return key


def _new_key(credits=100):
    return _insert_key(f"td_sk_{secrets.token_hex(16)}", credits)


def _row(key):
    with main.get_db() as conn:
        return conn.execute(
            "SELECT is_active, credits_remaining, total_calls FROM api_keys WHERE key_hash = ?",
            (main.hash_key(key),),
        ).fetchone()


def _revoke(client, prefix, headers=ADMIN):
    return client.post(f"/admin/keys/{prefix}/revoke", headers=headers)


def test_revoke_requires_admin_secret():
    client = TestClient(main.app)
    key = _new_key()
    prefix = main.get_key_prefix(key)

    assert _revoke(client, prefix, headers={}).status_code == 401
    wrong = {"x-admin-secret": "x" * len(main.ADMIN_SECRET)}
    assert _revoke(client, prefix, headers=wrong).status_code == 401
    assert _row(key)["is_active"] == 1


def test_revoke_success_with_or_without_dots(caplog):
    client = TestClient(main.app)
    key = _new_key(credits=7)
    prefix = main.get_key_prefix(key)
    assert prefix.endswith("...")

    with caplog.at_level(logging.WARNING):
        response = _revoke(client, prefix[:-3])
    assert response.status_code == 200, response.text
    assert response.json() == {"key_prefix": prefix, "is_active": False}
    row = _row(key)
    assert row["is_active"] == 0
    assert row["credits_remaining"] == 7
    assert row["total_calls"] == 0
    assert any(prefix in r.getMessage() and "revoked" in r.getMessage() for r in caplog.records)
    assert not any(key in r.getMessage() for r in caplog.records)
    assert not any(main.ADMIN_SECRET in r.getMessage() for r in caplog.records)

    # Idempotent: revoking again keeps it inactive and still returns 200.
    again = _revoke(client, prefix)
    assert again.status_code == 200
    assert again.json() == {"key_prefix": prefix, "is_active": False}

    status = client.get(f"/admin/keys/{prefix}", headers=ADMIN)
    assert status.json()["is_active"] is False


def test_revoked_key_gets_401_on_paid_route(monkeypatch):
    called = []

    async def scrape_ok(url, js_render=False):
        called.append(url)
        return {"content": "ok", "url": url, "status": "success", "word_count": 1}

    monkeypatch.setattr(main, "_scrape", scrape_ok)
    client = TestClient(main.app)
    key = _new_key(credits=10)

    assert _revoke(client, main.get_key_prefix(key)).status_code == 200

    response = client.post(
        "/scrape",
        json={"url": "https://example.com/page", "js_render": False},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 401, response.text
    assert "API key revoked" in response.text
    assert called == []
    assert _row(key)["credits_remaining"] == 10


def test_ambiguous_prefix_refused_and_nothing_changes(caplog):
    client = TestClient(main.app)
    shared = f"td_sk_{secrets.token_hex(3)}"  # 12 characters
    assert len(shared) == 12
    key_a = _insert_key(shared + secrets.token_hex(16))
    key_b = _insert_key(shared + secrets.token_hex(16))
    prefix = main.get_key_prefix(key_a)
    assert prefix == main.get_key_prefix(key_b)

    with caplog.at_level(logging.WARNING):
        response = _revoke(client, prefix)
    assert response.status_code == 409, response.text
    assert _row(key_a)["is_active"] == 1
    assert _row(key_b)["is_active"] == 1
    assert any(prefix in r.getMessage() and "refused" in r.getMessage() for r in caplog.records)


def test_unknown_prefix_404():
    client = TestClient(main.app)
    response = _revoke(client, "td_sk_zzzzzz...")
    assert response.status_code == 404
