"""JS render success, retry, and static fallback.

A renderer crash used to escape POST /extract and come back as HTTP 500
with an empty body behind the platform proxy. These tests lock the
contract: render, or degrade to a static scrape and say so.
"""

import asyncio
import secrets

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main

SAMPLE_HTML = """<!DOCTYPE html><html><head><title>Hexagon Care</title></head><body>
<article>
<h1>Hexagon Care</h1>
<p>Hexagon Care provides supported living and residential care across the United Kingdom. The registered director is Jane Example and the office is in Manchester.</p>
<p>Families can contact the team for referrals, assessments, and day services. The organisation has supported adults with learning disabilities since 1998.</p>
</article>
</body></html>"""

URL = "https://www.hexagoncare.com"


def _insert_key(credits: int = 100) -> str:
    key = f"td_sk_test_{secrets.token_hex(8)}"
    with main.get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (main.hash_key(key), main.get_key_prefix(key), credits, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    return key


async def _fake_extract(markdown, url, fields, js_rendered):
    return {
        "url": url,
        "status": "success",
        "data": {field: "Jane Example" for field in fields},
        "fields_requested": fields,
        "fields_extracted": list(fields),
        "js_rendered": js_rendered,
        "model": "test",
    }


def _post_extract(monkeypatch, render_page):
    monkeypatch.setattr(main, "_render_page", render_page)
    monkeypatch.setattr(main, "_fetch_static", lambda url: (SAMPLE_HTML, None))
    monkeypatch.setattr(main, "_extract_structured", _fake_extract)
    client = TestClient(main.app, raise_server_exceptions=False)
    return client.post(
        "/extract",
        json={"url": URL, "fields": ["director_name"], "js_render": True},
        headers={"Authorization": f"Bearer {_insert_key()}"},
    )


@pytest.mark.asyncio
async def test_render_success_path(monkeypatch):
    calls = {"n": 0}

    async def render(url):
        calls["n"] += 1
        assert url == URL
        return SAMPLE_HTML

    monkeypatch.setattr(main, "_render_page", render)
    result = await main._scrape(URL, js_render=True)

    assert calls["n"] == 1
    assert result["status"] == "success"
    assert result["js_rendered"] is True
    assert result["js_render_fallback"] is False
    assert "fallback_reason" not in result
    assert "Jane Example" in result["content"]
    assert result["title"] == "Hexagon Care"


@pytest.mark.asyncio
async def test_render_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def render(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("navigation timed out")
        return SAMPLE_HTML

    monkeypatch.setattr(main, "_render_page", render)
    html = await main._fetch_with_playwright(URL)

    assert html == SAMPLE_HTML
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_render_timeout_fails_after_one_retry(monkeypatch):
    calls = {"n": 0}

    async def render(url):
        calls["n"] += 1
        await asyncio.sleep(5)
        return SAMPLE_HTML

    monkeypatch.setattr(main, "_render_page", render)
    monkeypatch.setattr(main, "RENDER_ATTEMPT_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(main.RenderError, match="after 2 attempts"):
        await main._fetch_with_playwright(URL)

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_render_failure_falls_back_to_static_scrape(monkeypatch):
    calls = {"n": 0}

    async def render(url):
        calls["n"] += 1
        raise RuntimeError("Target closed: browser crashed")

    monkeypatch.setattr(main, "_render_page", render)
    monkeypatch.setattr(main, "_fetch_static", lambda url: (SAMPLE_HTML, None))

    result = await main._scrape(URL, js_render=True)

    assert calls["n"] == main.RENDER_ATTEMPTS
    assert result["js_rendered"] is False
    assert result["js_render_fallback"] is True
    assert "RuntimeError" in result["fallback_reason"]
    assert "after 2 attempts" in result["fallback_reason"]
    assert "Jane Example" in result["content"]


def test_extract_render_success_returns_rendered_result(monkeypatch):
    async def render(url):
        return SAMPLE_HTML

    response = _post_extract(monkeypatch, render)

    assert response.content
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["js_rendered"] is True
    assert body["js_render_fallback"] is False
    assert "fallback_reason" not in body
    assert body["data"]["director_name"] == "Jane Example"


def test_extract_render_crash_falls_back_instead_of_empty_500(monkeypatch):
    """The reported failure: renderer crash on a real URL, empty HTTP 500."""

    async def render(url):
        raise RuntimeError("page crashed")

    response = _post_extract(monkeypatch, render)

    assert response.content, "crash path must not return an empty body"
    assert response.status_code != 500
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["js_render_fallback"] is True
    assert body["js_rendered"] is False
    assert body["fallback_reason"]
    assert "page crashed" in body["fallback_reason"]
    assert body["data"]["director_name"] == "Jane Example"
    assert body["status"] == "success"


def test_extract_render_and_static_failure_returns_json_body(monkeypatch):
    async def render(url):
        raise RuntimeError("page crashed")

    def static_down(url):
        raise HTTPException(status_code=502, detail=f"Failed to fetch {url}: connection refused")

    monkeypatch.setattr(main, "_render_page", render)
    monkeypatch.setattr(main, "_fetch_static", static_down)
    monkeypatch.setattr(main, "_extract_structured", _fake_extract)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/extract",
        json={"url": URL, "fields": ["director_name"], "js_render": True},
        headers={"Authorization": f"Bearer {_insert_key()}"},
    )

    assert response.content, "total failure must not return an empty body"
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    detail = response.json()["detail"]
    assert "connection refused" in detail
    assert "JS render failed" in detail
    assert "page crashed" in detail
