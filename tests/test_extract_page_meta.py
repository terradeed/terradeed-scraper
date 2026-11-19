"""Title and meta description reach the extractor prompt.

Trafilatura's markdown drops ``<title>`` and meta descriptions. These
tests use saved HTML and a mocked model call. They make no network calls.
"""

import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE_META = (FIXTURES / "extract_page_meta.html").read_text(encoding="utf-8")
EXAMPLE = (FIXTURES / "example.html").read_text(encoding="utf-8")
URL = "https://example.com/"

_dumps = json.dumps

META_LINES = (
    "Page title: Hexagon Care | Supported living",
    "Meta description: Supported living and residential care across the United Kingdom.",
    "Open Graph title: Hexagon Care",
    "Open Graph description: Care services for adults in Manchester.",
)


def _insert_key(credits: int = 100) -> str:
    key = f"td_sk_test_{secrets.token_hex(8)}"
    with main.get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (main.hash_key(key), main.get_key_prefix(key), credits, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    return key


def _install(monkeypatch, html: str):
    prompts = []

    def fetch_static(url):
        assert url == URL
        return html, None

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            prompt = json["messages"][0]["content"]
            prompts.append(prompt)
            if "Extract property listing data" in prompt:
                payload = {
                    "listing_type": "sale",
                    "property": {"address": "Welcome"},
                    "vendor": {},
                    "source": "other",
                    "confidence": {},
                }
            else:
                payload = {
                    "title": "Hexagon Care | Supported living",
                    "description": "from the model",
                }
            text = _dumps(payload)

            def body():
                return {"content": [{"type": "text", "text": text}]}

            return SimpleNamespace(status_code=200, text=text, json=body)

    async def enrich(**kwargs):
        return {"enrichment_status": "skipped", "data_complete": False, "failed_sources": []}

    monkeypatch.setattr(main, "_fetch_static", fetch_static)
    monkeypatch.setattr(main.httpx, "AsyncClient", RecordingClient)
    monkeypatch.setattr(main, "enrich_property", enrich)
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    return prompts


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/extract", {"url": URL, "fields": ["title", "description"], "js_render": False}),
        ("/extract/property", {"url": URL, "js_render": False}),
    ],
)
def test_title_and_meta_description_reach_the_prompt(monkeypatch, path, payload):
    prompts = _install(monkeypatch, PAGE_META)
    response = TestClient(main.app).post(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {_insert_key()}"},
    )

    assert response.status_code == 200, response.text
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "Page metadata (not always repeated in the body text):" in prompt
    for line in META_LINES:
        assert line in prompt
    content_at = prompt.index("Page content:")
    metadata_at = prompt.index("Page title:")
    assert metadata_at < content_at
    assert "Families can ask the office for a brochure." in prompt
    assert "page_context" not in response.json()


def test_title_without_social_tags_is_labelled_and_og_lines_are_omitted(monkeypatch):
    prompts = _install(monkeypatch, EXAMPLE)
    response = TestClient(main.app).post(
        "/extract",
        json={"url": URL, "fields": ["title", "description"], "js_render": False},
        headers={"Authorization": f"Bearer {_insert_key()}"},
    )

    assert response.status_code == 200, response.text
    prompt = prompts[0]
    assert "Page title: Example Domain" in prompt
    assert "Meta description:" not in prompt
    assert "Open Graph title:" not in prompt
    assert "Open Graph description:" not in prompt


def test_scrape_response_does_not_include_extractor_page_context(monkeypatch):
    _install(monkeypatch, PAGE_META)
    response = TestClient(main.app).post(
        "/scrape",
        json={"url": URL, "js_render": False},
        headers={"Authorization": f"Bearer {_insert_key()}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "page_context" not in body
    assert "Families can ask the office for a brochure." in body["content"]
    assert "Page title:" not in body["content"]
