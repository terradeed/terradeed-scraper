"""Anthropic replies are read by content-block type.

Claude Sonnet 5.5 runs adaptive thinking when the request omits
``thinking``, so the first block is often a thinking block and has no
``text`` key. ``content[0]["text"]`` raised KeyError (HTTP 500 on
``/extract/property``). These tests make no network calls.
"""

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from tests.test_payment_settlement import (
    Recorder,
    _assert_not_settled,
    _assert_requirements,
    _balance,
    _insert_key,
    _x402_headers,
)

URL = "https://example.com/page"
PAGE = "A commercial unit at 1 High Street, Manchester."

EXTRACT_DATA = {"title": "Hexagon Care"}
PROPERTY_DATA = {
    "listing_type": "sale",
    "property": {"address": "1 High Street, Manchester", "asking_price": 850000},
    "vendor": {"agent_name": "Example Agent"},
    "source": "other",
    "confidence": {"address": 0.95},
}

THINKING = {
    "type": "thinking",
    "thinking": json.dumps({"title": "from-thinking", "address": "from-thinking"}),
    "signature": "sig",
}
REDACTED = {"type": "redacted_thinking", "data": "abc"}


def _reply(content, *, json_raises=False):
    def body():
        if json_raises:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": content,
        }

    return SimpleNamespace(status_code=200, text="{}", json=body)


def _patch_anthropic(monkeypatch, content, *, json_raises=False):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            assert url == "https://api.anthropic.com/v1/messages"
            assert json["model"] == main.EXTRACT_MODEL
            assert "thinking" not in json
            return _reply(content, json_raises=json_raises)

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")


async def _skipped_enrichment(**kwargs):
    return {
        "enrichment_status": "skipped",
        "data_complete": False,
        "failed_sources": [],
    }


def _payload_for(kind):
    return EXTRACT_DATA if kind == "extract" else PROPERTY_DATA


def _text_block(kind):
    return {"type": "text", "text": json.dumps(_payload_for(kind))}


async def _invoke(kind):
    if kind == "extract":
        return await main._extract_structured(PAGE, URL, ["title"], False)
    return await main._extract_property(PAGE, URL, False)


async def _call(kind):
    result = await _invoke(kind)
    assert result["status"] == "success"
    assert result["model"] == main.EXTRACT_MODEL
    if kind == "extract":
        assert result["data"]["title"] == "Hexagon Care"
    else:
        assert result["property"]["address"] == "1 High Street, Manchester"
        assert result["listing_type"] == "sale"
    return result


@pytest.fixture
def quiet_enrichment(monkeypatch):
    monkeypatch.setattr(main, "enrich_property", _skipped_enrichment)


def test_message_text_skips_a_leading_non_text_block():
    raw = main._anthropic_message_text(
        _reply([THINKING, {"type": "text", "text": json.dumps(EXTRACT_DATA)}])
    )
    assert json.loads(raw) == EXTRACT_DATA


def test_message_text_reads_a_normal_text_block():
    typed = main._anthropic_message_text(_reply([{"type": "text", "text": '{"title": "Hexagon Care"}'}]))
    untyped = main._anthropic_message_text(_reply([{"text": '{"title": "Hexagon Care"}'}]))
    assert json.loads(typed) == EXTRACT_DATA
    assert json.loads(untyped) == EXTRACT_DATA


def test_message_text_joins_text_blocks_and_ignores_thinking():
    raw = main._anthropic_message_text(
        _reply(
            [
                THINKING,
                {"type": "text", "text": '{"title":'},
                {"type": "text", "text": '"Hexagon Care"}'},
            ]
        )
    )
    assert json.loads(raw) == EXTRACT_DATA


@pytest.mark.parametrize(
    "content",
    [
        [THINKING],
        [REDACTED],
        [],
        [{"type": "text", "text": "  \n\t"}],
        [THINKING, {"type": "text", "text": "   "}],
        [{"type": "thinking", "signature": "sig"}],
    ],
)
def test_message_text_rejects_a_reply_with_no_text(content):
    with pytest.raises(HTTPException) as caught:
        main._anthropic_message_text(_reply(content))
    assert caught.value.status_code == 502
    assert caught.value.detail == main.MODEL_NO_TEXT_DETAIL


def test_message_text_rejects_a_non_json_body():
    with pytest.raises(HTTPException) as caught:
        main._anthropic_message_text(_reply([], json_raises=True))
    assert caught.value.status_code == 502
    assert caught.value.detail == main.MODEL_NO_TEXT_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["extract", "property"])
@pytest.mark.parametrize("leading", [THINKING, REDACTED])
async def test_extractors_use_text_when_the_first_block_is_not_text(
    monkeypatch, quiet_enrichment, kind, leading
):
    _patch_anthropic(monkeypatch, [leading, _text_block(kind)])
    await _call(kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["extract", "property"])
@pytest.mark.parametrize(
    "block",
    [
        {"type": "text"},
        {},
    ],
)
async def test_extractors_read_a_normal_text_reply(monkeypatch, quiet_enrichment, kind, block):
    text_block = {**block, "text": json.dumps(_payload_for(kind))}
    _patch_anthropic(monkeypatch, [text_block])
    await _call(kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["extract", "property"])
@pytest.mark.parametrize(
    "content_of",
    ["thinking", "empty", "whitespace"],
)
async def test_extractors_return_502_when_the_reply_has_no_text(
    monkeypatch, quiet_enrichment, kind, content_of
):
    content = {
        "thinking": [THINKING],
        "empty": [],
        "whitespace": [{"type": "text", "text": " \n "}],
    }[content_of]
    _patch_anthropic(monkeypatch, content)
    with pytest.raises(HTTPException) as caught:
        await _invoke(kind)
    assert caught.value.status_code == 502
    assert caught.value.detail == main.MODEL_NO_TEXT_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["extract", "property"])
async def test_malformed_model_json_stays_502(monkeypatch, quiet_enrichment, kind):
    _patch_anthropic(
        monkeypatch,
        [THINKING, {"type": "text", "text": "not json"}],
    )
    with pytest.raises(HTTPException) as caught:
        await _invoke(kind)
    assert caught.value.status_code == 502
    assert caught.value.detail == "Malformed JSON from model"


def _install_route(monkeypatch, content):
    async def scrape_ok(url, js_render=False):
        return {
            "content": PAGE,
            "url": url,
            "status": "success",
            "word_count": len(PAGE.split()),
            "title": "Listing",
            "js_rendered": False,
            "js_render_fallback": False,
        }

    monkeypatch.setattr(main, "_scrape", scrape_ok)
    monkeypatch.setattr(main, "enrich_property", _skipped_enrichment)
    _patch_anthropic(monkeypatch, content)


def _post(route, headers):
    spec = {
        "extract": {
            "path": "/extract",
            "json": {"url": URL, "fields": ["title"], "js_render": False},
        },
        "property": {
            "path": "/extract/property",
            "json": {"url": URL, "js_render": False},
        },
    }[route]
    return TestClient(main.app).post(spec["path"], json=spec["json"], headers=headers)


@pytest.mark.parametrize("route", ["extract", "property"])
def test_paid_call_with_a_leading_thinking_block_settles(monkeypatch, route):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_route(monkeypatch, [THINKING, _text_block(route)])

    response = _post(route, _x402_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_method"] == "x402"
    if route == "extract":
        assert body["data"]["title"] == "Hexagon Care"
    else:
        assert body["property"]["address"] == "1 High Street, Manchester"
    assert len(recorder.verified) == 1
    assert len(recorder.settled) == 1
    _assert_requirements(recorder.verified[0]["requirements"], route)
    receipt = json.loads(base64.b64decode(response.headers["PAYMENT-RESPONSE"]))
    assert receipt == {"success": True, "transaction": "0xabc"}


@pytest.mark.parametrize("route", ["extract", "property"])
@pytest.mark.parametrize("auth", ["x402", "api_key"])
def test_paid_call_with_no_text_is_502_and_not_charged(monkeypatch, route, auth):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_route(monkeypatch, [THINKING])
    key = _insert_key(100)
    headers = _x402_headers() if auth == "x402" else {"Authorization": f"Bearer {key}"}

    response = _post(route, headers)

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == main.MODEL_NO_TEXT_DETAIL
    _assert_not_settled(response, recorder)
    assert _balance(key) == (100, 0)
    if auth == "x402":
        assert len(recorder.verified) == 1
        _assert_requirements(recorder.verified[0]["requirements"], route)
