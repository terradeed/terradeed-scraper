"""Paid calls settle or deduct only after a usable success.

x402 is verified before the handler. Settlement, and API-key credit
deduction, run only when the handler returns 2xx with usable content.
Upstream errors, timeouts, 4xx/5xx, and empty extractions are not charged.
No network and no facilitator: fetchers and x402 verify/settle are mocked.
"""

import base64
import json
import secrets
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from uk_tenders import TenderQueryError, TenderUpstreamError

PAGE = "https://example.com/page"
SCRAPE_CONTENT = "Hexagon Care provides supported living in Manchester."

ROUTES = {
    "scrape": {
        "method": "post",
        "path": "/scrape",
        "json": {"url": PAGE, "js_render": False},
        "credits": main.SCRAPE_CREDITS,
        "amount": main.SCRAPE_REQUIREMENTS["amount"],
        "failure_status": 504,
        "failure_text": "Timeout fetching",
    },
    "extract": {
        "method": "post",
        "path": "/extract",
        "json": {"url": PAGE, "fields": ["title"], "js_render": False},
        "credits": main.EXTRACT_CREDITS,
        "amount": main.EXTRACT_REQUIREMENTS["amount"],
        "failure_status": 502,
        "failure_text": "Anthropic error: 529",
    },
    "property": {
        "method": "post",
        "path": "/extract/property",
        "json": {"url": PAGE, "js_render": False},
        "credits": main.PROPERTY_CREDITS,
        "amount": main.PROPERTY_REQUIREMENTS["amount"],
        "failure_status": 502,
        "failure_text": "Anthropic error: 500",
    },
    "tenders": {
        "method": "get",
        "path": "/v1/uk/tenders",
        "params": {"q": "training", "limit": 1},
        "credits": main.TENDER_CREDITS,
        "amount": main.TENDER_REQUIREMENTS["amount"],
        "failure_status": 502,
        "failure_text": "both unavailable",
    },
}


class Recorder:
    def __init__(self):
        self.verified = []
        self.settled = []
        self.payload = {"kind": "test-payload"}

    def install(self, monkeypatch, *, valid=True, settle_raises=False):
        monkeypatch.setattr("x402.parse_payment_payload", lambda raw: self.payload)

        async def verify(payload, requirements, *args, **kwargs):
            self.verified.append({"payload": payload, "requirements": requirements})
            return SimpleNamespace(
                is_valid=valid,
                invalid_reason=None if valid else "invalid_signature",
                invalid_message=None if valid else "signature mismatch",
            )

        async def settle(*args, **kwargs):
            self.settled.append((args, kwargs))
            if settle_raises:
                raise RuntimeError("facilitator down")
            return {"success": True, "transaction": "0xabc"}

        monkeypatch.setattr(main.x402_server, "verify_payment", verify)
        monkeypatch.setattr(main.x402_server, "settle_payment", settle)


def _x402_headers():
    token = base64.b64encode(b'{"x402Version":2}').decode()
    return {"payment-signature": token}


def _insert_key(credits=100):
    key = f"td_sk_test_{secrets.token_hex(8)}"
    with main.get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (main.hash_key(key), main.get_key_prefix(key), credits, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    return key


def _balance(key):
    with main.get_db() as conn:
        row = conn.execute(
            "SELECT credits_remaining, total_calls FROM api_keys WHERE key_hash = ?",
            (main.hash_key(key),),
        ).fetchone()
    return row["credits_remaining"], row["total_calls"] or 0


def _request(client, route, headers):
    spec = ROUTES[route]
    kwargs = {"headers": headers}
    if "json" in spec:
        kwargs["json"] = spec["json"]
    if "params" in spec:
        kwargs["params"] = spec["params"]
    return getattr(client, spec["method"])(spec["path"], **kwargs)


def _install_backends(monkeypatch, route, mode):
    async def scrape_ok(url, js_render=False):
        content = "   " if mode == "empty" and route == "scrape" else SCRAPE_CONTENT
        return {
            "content": content,
            "url": url,
            "status": "success",
            "word_count": len(content.split()),
            "title": "Hexagon Care" if content.strip() else None,
            "js_rendered": False,
            "js_render_fallback": False,
        }

    async def scrape_fail(url, js_render=False):
        raise HTTPException(status_code=504, detail=f"Timeout fetching {url}")

    if route == "scrape":
        monkeypatch.setattr(main, "_scrape", scrape_fail if mode == "failure" else scrape_ok)
        return

    monkeypatch.setattr(main, "_scrape", scrape_ok)

    async def extract_ok(markdown, url, fields, js_rendered, **kwargs):
        if mode == "empty":
            data = {field: None for field in fields}
        else:
            data = {field: "Hexagon Care" for field in fields}
        return {
            "url": url,
            "status": "success",
            "data": data,
            "fields_requested": list(fields),
            "fields_extracted": [key for key, value in data.items() if value is not None],
            "js_rendered": js_rendered,
            "model": "test",
        }

    async def extract_fail(markdown, url, fields, js_rendered, **kwargs):
        raise HTTPException(status_code=502, detail="Anthropic error: 529 - overloaded")

    async def property_ok(markdown, url, js_rendered, **kwargs):
        if mode == "empty":
            prop = {"address": None, "asking_price": None}
            listing = None
        else:
            prop = {"address": "1 High Street, Manchester", "asking_price": 850000}
            listing = "sale"
        return {
            "url": url,
            "status": "success",
            "listing_type": listing,
            "property": prop,
            "vendor": {},
            "source": "other",
            "confidence": {},
            "js_rendered": js_rendered,
            "model": "test",
        }

    async def property_fail(markdown, url, js_rendered, **kwargs):
        raise HTTPException(status_code=502, detail="Anthropic error: 500 - upstream")

    async def tenders_ok(**kwargs):
        results = [] if mode == "empty" else [{"ocid": "ocds-test", "title": "AI training"}]
        return {
            "licence": "OGL-UK-3.0",
            "attribution": "Contains public sector information licensed under the Open Government Licence v3.0.",
            "sources": ["find-tender"],
            "results": results,
            "next_cursor": None,
        }

    async def tenders_fail(**kwargs):
        raise TenderUpstreamError("Find a Tender and Contracts Finder are both unavailable")

    if route == "extract":
        monkeypatch.setattr(
            main, "_extract_structured", extract_fail if mode == "failure" else extract_ok
        )
    elif route == "property":
        monkeypatch.setattr(
            main, "_extract_property", property_fail if mode == "failure" else property_ok
        )
    elif route == "tenders":
        monkeypatch.setattr(
            main.tender_service, "search", tenders_fail if mode == "failure" else tenders_ok
        )


def _assert_requirements(requirements, route):
    assert requirements.scheme == "exact"
    assert requirements.network == "eip155:8453"
    assert requirements.asset == main.USDC_BASE
    assert requirements.amount == ROUTES[route]["amount"]
    assert requirements.pay_to == main.PAY_TO
    assert requirements.max_timeout_seconds == 300
    assert requirements.extra == {"name": "USD Coin", "version": "2"}


def _assert_success_body(route, body):
    if route == "scrape":
        assert body["status"] == "success"
        assert "Hexagon Care" in body["content"]
    elif route == "extract":
        assert body["status"] == "success"
        assert body["data"]["title"] == "Hexagon Care"
    elif route == "property":
        assert body["status"] == "success"
        assert body["property"]["address"] == "1 High Street, Manchester"
    elif route == "tenders":
        assert body["licence"] == "OGL-UK-3.0"
        assert body["results"][0]["title"] == "AI training"


def _assert_not_settled(response, recorder):
    assert recorder.settled == []
    assert "payment-response" not in {key.lower() for key in response.headers}


@pytest.mark.parametrize("route", list(ROUTES))
def test_x402_success_settles(monkeypatch, route):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, route, "success")

    response = _request(TestClient(main.app), route, _x402_headers())

    assert response.status_code == 200, response.text
    assert len(recorder.verified) == 1
    assert len(recorder.settled) == 1
    assert recorder.verified[0]["payload"] is recorder.payload
    _assert_requirements(recorder.verified[0]["requirements"], route)
    assert recorder.settled[0][1]["payload"] is recorder.payload
    assert recorder.settled[0][1]["requirements"] is recorder.verified[0]["requirements"]
    receipt = json.loads(base64.b64decode(response.headers["PAYMENT-RESPONSE"]))
    assert receipt == {"success": True, "transaction": "0xabc"}
    body = response.json()
    assert body["auth_method"] == "x402"
    _assert_success_body(route, body)


@pytest.mark.parametrize("route", list(ROUTES))
def test_x402_failure_does_not_settle(monkeypatch, route):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, route, "failure")

    response = _request(TestClient(main.app), route, _x402_headers())

    spec = ROUTES[route]
    assert response.status_code == spec["failure_status"], response.text
    assert spec["failure_text"] in response.text
    assert len(recorder.verified) == 1
    _assert_requirements(recorder.verified[0]["requirements"], route)
    _assert_not_settled(response, recorder)


@pytest.mark.parametrize("route", list(ROUTES))
def test_api_key_success_deducts(monkeypatch, route):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, route, "success")
    key = _insert_key(100)

    response = _request(
        TestClient(main.app), route, {"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_method"] == "api_key"
    assert body["credits_remaining"] == 100 - ROUTES[route]["credits"]
    _assert_success_body(route, body)
    assert _balance(key) == (100 - ROUTES[route]["credits"], 1)
    assert recorder.verified == []
    _assert_not_settled(response, recorder)


@pytest.mark.parametrize("route", list(ROUTES))
def test_api_key_failure_does_not_deduct(monkeypatch, route):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, route, "failure")
    key = _insert_key(100)

    response = _request(
        TestClient(main.app), route, {"Authorization": f"Bearer {key}"}
    )

    spec = ROUTES[route]
    assert response.status_code == spec["failure_status"], response.text
    assert spec["failure_text"] in response.text
    assert _balance(key) == (100, 0)
    assert recorder.verified == []
    _assert_not_settled(response, recorder)


@pytest.mark.parametrize("route", ["scrape", "extract", "property"])
@pytest.mark.parametrize("auth", ["x402", "api_key"])
def test_empty_extraction_is_not_charged(monkeypatch, route, auth):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, route, "empty")
    key = _insert_key(100)
    headers = _x402_headers() if auth == "x402" else {"Authorization": f"Bearer {key}"}

    response = _request(TestClient(main.app), route, headers)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == main.EMPTY_EXTRACTION_DETAIL
    _assert_not_settled(response, recorder)
    assert _balance(key) == (100, 0)


@pytest.mark.parametrize("auth", ["x402", "api_key"])
def test_tender_search_with_no_matches_is_charged(monkeypatch, auth):
    """An empty notice list is a successful search, not an empty extraction."""
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, "tenders", "empty")
    key = _insert_key(100)
    headers = _x402_headers() if auth == "x402" else {"Authorization": f"Bearer {key}"}

    response = _request(TestClient(main.app), "tenders", headers)

    assert response.status_code == 200, response.text
    assert response.json()["results"] == []
    assert response.json()["licence"] == "OGL-UK-3.0"
    if auth == "x402":
        assert len(recorder.settled) == 1
        assert "PAYMENT-RESPONSE" in response.headers
        assert _balance(key) == (100, 0)
    else:
        assert recorder.settled == []
        assert response.json()["credits_remaining"] == 100 - main.TENDER_CREDITS
        assert _balance(key) == (100 - main.TENDER_CREDITS, 1)


def test_blank_page_scrape_returns_422_and_does_not_settle(monkeypatch):
    recorder = Recorder()
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        main,
        "_fetch_static",
        lambda url: ("<html><head><title>Empty</title></head><body></body></html>", None),
    )

    async def no_browser(url):
        raise RuntimeError("browser disabled in test")

    monkeypatch.setattr(main, "_fetch_with_playwright", no_browser)
    key = _insert_key(100)

    response = _request(TestClient(main.app), "scrape", _x402_headers())

    assert response.status_code == 422, response.text
    assert "Could not extract content" in response.json()["detail"]
    assert len(recorder.verified) == 1
    _assert_not_settled(response, recorder)
    assert _balance(key) == (100, 0)

    response = _request(
        TestClient(main.app), "scrape", {"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 422, response.text
    assert _balance(key) == (100, 0)
    _assert_not_settled(response, recorder)


def test_tender_query_error_does_not_settle_or_deduct(monkeypatch):
    recorder = Recorder()
    recorder.install(monkeypatch)

    async def bad_query(**kwargs):
        raise TenderQueryError("limit must be between 1 and 25")

    monkeypatch.setattr(main.tender_service, "search", bad_query)
    key = _insert_key(100)
    client = TestClient(main.app)

    x402 = client.get("/v1/uk/tenders", headers=_x402_headers(), params={"limit": 1})
    assert x402.status_code == 400
    assert x402.json()["detail"] == "limit must be between 1 and 25"
    _assert_not_settled(x402, recorder)

    keyed = client.get(
        "/v1/uk/tenders",
        headers={"Authorization": f"Bearer {key}"},
        params={"limit": 1},
    )
    assert keyed.status_code == 400
    assert _balance(key) == (100, 0)


def test_invalid_payment_does_not_settle_or_run_handler(monkeypatch):
    recorder = Recorder()
    recorder.install(monkeypatch, valid=False)
    calls = {"n": 0}

    async def scrape(url, js_render=False):
        calls["n"] += 1
        raise AssertionError("handler should not run")

    monkeypatch.setattr(main, "_scrape", scrape)
    response = _request(TestClient(main.app), "scrape", _x402_headers())

    assert response.status_code == 402
    assert "signature mismatch" in response.json()["error"]
    assert calls["n"] == 0
    assert len(recorder.verified) == 1
    _assert_not_settled(response, recorder)


def test_missing_auth_is_402_and_does_not_settle(monkeypatch):
    recorder = Recorder()
    recorder.install(monkeypatch)
    _install_backends(monkeypatch, "extract", "success")

    response = _request(TestClient(main.app), "extract", {})

    assert response.status_code == 402
    assert response.json()["error"] == "Payment required"
    assert recorder.verified == []
    _assert_not_settled(response, recorder)


def test_settle_exception_still_returns_content_without_receipt(monkeypatch):
    recorder = Recorder()
    recorder.install(monkeypatch, settle_raises=True)
    _install_backends(monkeypatch, "scrape", "success")

    response = _request(TestClient(main.app), "scrape", _x402_headers())

    assert response.status_code == 200, response.text
    assert "Hexagon Care" in response.json()["content"]
    assert len(recorder.settled) == 1
    assert "payment-response" not in {key.lower() for key in response.headers}


def test_credit_race_after_success_returns_402_without_deducting(monkeypatch):
    _install_backends(monkeypatch, "scrape", "success")
    key = _insert_key(100)

    def deny(api_key, credits, endpoint):
        return False

    monkeypatch.setattr(main, "deduct_credits", deny)
    response = _request(
        TestClient(main.app), "scrape", {"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "Insufficient credits"
    assert "content" not in response.json()
    assert _balance(key) == (100, 0)
