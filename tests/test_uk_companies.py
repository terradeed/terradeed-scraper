"""UK company lookup and search: Companies House HTML in, company-level JSON out.

No network. Pages come from saved fixtures through httpx.MockTransport, and
x402 verify/settle are mocked the same way as test_payment_settlement.
"""

import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from tests.test_payment_settlement import Recorder, _balance, _insert_key, _x402_headers
from uk_companies import (
    CH_BASE,
    LICENCE,
    CompanyNotFound,
    CompanyQueryError,
    CompanyService,
    CompanyUpstreamError,
    normalise_company_number,
    parse_company_overview,
    parse_filing_history,
    parse_search_results,
)

FIXTURES = Path(__file__).parent / "fixtures" / "companies_house"
OFFICER_NAME = "Wendy Mira Becker"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _routes():
    return {
        "/company/00445790": (200, _page("overview_00445790.html")),
        "/company/00445790/filing-history": (200, _page("filings_00445790.html")),
        "/company/SC540749": (200, _page("overview_SC540749.html")),
        "/company/SC540749/filing-history": (200, "<html></html>"),
        "/company/08079123": (200, _page("overview_08079123.html")),
        "/company/99999999": (404, _page("not_found.html")),
        "/search/companies": (200, _page("search_widget.html")),
    }


def _service(calls=None, override=None):
    routes = _routes()
    if override:
        routes.update(override)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        assert request.url.host == "find-and-update.company-information.service.gov.uk"
        assert "TerradeedLabs-UKCompanies" in request.headers["user-agent"]
        status, body = routes.get(request.url.path, (404, "not found"))
        return httpx.Response(status, text=body)

    return CompanyService(transport=httpx.MockTransport(handler), min_interval=0)


# --- parsing ---------------------------------------------------------------

def test_overview_parses_company_level_fields():
    company = parse_company_overview(_page("overview_00445790.html"))
    assert company["company_number"] == "00445790"
    assert company["name"] == "TESCO PLC"
    assert company["status"] == "active"
    assert company["type"] == "Public limited Company"
    assert company["date_of_creation"] == "1947-11-27"
    assert company["registered_office_address"].endswith("AL7 1GA")
    assert company["sic_codes"][0]["code"] == "47110"
    assert company["previous_names"][0]["from"] == "1981-12-14"
    assert company["accounts"] == {
        "next_made_up_to": "2027-02-26",
        "next_due": "2027-08-26",
        "last_made_up_to": "2026-02-28",
        "overdue": False,
    }
    assert company["confirmation_statement"]["next_due"] == "2027-07-02"
    assert company["confirmation_statement"]["overdue"] is False


def test_overview_flags_overdue_and_strike_off():
    company = parse_company_overview(_page("overview_SC540749.html"))
    assert company["company_number"] == "SC540749"
    assert company["status_detail"] == "Active proposal to strike off"
    assert company["confirmation_statement"]["overdue"] is True
    assert company["accounts"]["overdue"] is False


def test_overview_dissolved_company():
    company = parse_company_overview(_page("overview_08079123.html"))
    assert company["status"] == "dissolved"
    assert company["date_of_cessation"] == "2016-04-26"
    assert company["accounts"]["last_made_up_to"] == "2013-05-31"
    assert company["confirmation_statement"]["last_made_up_to"] == "2015-05-22"


def test_not_found_page_is_not_a_company():
    assert parse_company_overview(_page("not_found.html")) is None


def test_filings_omit_officer_names_and_documents():
    filings = parse_filing_history(_page("filings_00445790.html"), 25)
    assert filings
    assert OFFICER_NAME not in json.dumps(filings)
    officer = [f for f in filings if f["type"] == "AP01"]
    assert officer
    assert officer[0]["category"] == "officers"
    assert officer[0]["description"] == "Officer filing (name omitted)"
    assert officer[0]["document_url"] is None
    capital = [f for f in filings if f["type"] == "SH06"][0]
    assert capital["category"] == "capital"
    assert capital["document_url"].startswith(f"{CH_BASE}/company/00445790/filing-history/")
    assert capital["date"] == "2026-11-24"


def test_filings_respect_limit():
    assert len(parse_filing_history(_page("filings_00445790.html"), 3)) == 3


def test_search_results_skip_template_and_parse_dates():
    results = parse_search_results(_page("search_widget.html"), 20)
    assert len(results) == 20
    first = results[0]
    assert first["company_number"] == "08079123"
    assert first["dissolved"] is True
    assert first["date_of_cessation"] == "2016-04-26"
    assert results[1]["date_of_creation"] == "2023-09-28"
    assert all("{{" not in json.dumps(item) for item in results)
    previous = [r for r in results if r["matched_previous_name"]]
    assert previous and previous[0]["matched_previous_name"] == "SALARY WIDGET LTD"


@pytest.mark.parametrize(
    "raw, expected",
    [("00445790", "00445790"), ("445790", "00445790"), (" sc540749 ", "SC540749"), ("SC54074", "SC054074"), ("oc 123456", "OC123456")],
)
def test_company_number_normalisation(raw, expected):
    assert normalise_company_number(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "ABC", "123456789", "TESCO PLC", "00/445790"])
def test_company_number_rejects_bad_input(raw):
    with pytest.raises(CompanyQueryError):
        normalise_company_number(raw)


# --- service ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_returns_profile_and_filings_and_caches():
    calls = []
    service = _service(calls)
    result = await service.lookup(number="445790", filings=5)
    assert result["licence"] == LICENCE
    assert result["company"]["name"] == "TESCO PLC"
    assert len(result["filings"]) == 5
    assert result["filings_status"] == "ok"
    assert result["source_url"] == f"{CH_BASE}/company/00445790"
    assert OFFICER_NAME not in json.dumps(result)
    await service.lookup(number="00445790", filings=5)
    assert len(calls) == 2  # overview + filings, second lookup cached


@pytest.mark.asyncio
async def test_lookup_without_filings_skips_history():
    calls = []
    result = await _service(calls).lookup(number="SC540749", filings=0)
    assert result["filings"] == []
    assert result["filings_status"] == "skipped"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lookup_unknown_number_raises_not_found():
    with pytest.raises(CompanyNotFound):
        await _service().lookup(number="99999999")


@pytest.mark.asyncio
async def test_lookup_rate_limit_opens_cooldown():
    calls = []
    service = _service(calls, {"/company/00445790": (429, "slow down")})
    with pytest.raises(CompanyUpstreamError):
        await service.lookup(number="00445790")
    with pytest.raises(CompanyUpstreamError):
        await service.lookup(number="00445790")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lookup_filings_outage_still_returns_profile():
    service = _service(override={"/company/00445790/filing-history": (500, "boom")})
    result = await service.lookup(number="00445790")
    assert result["company"]["company_number"] == "00445790"
    assert result["filings_status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("filings", [-1, 26, "x"])
async def test_lookup_rejects_bad_filings(filings):
    with pytest.raises(CompanyQueryError):
        await _service().lookup(number="00445790", filings=filings)


@pytest.mark.asyncio
async def test_search_returns_results():
    result = await _service().search(q="widget  solutions", limit=3)
    assert result["query"] == {"q": "widget solutions", "limit": 3}
    assert len(result["results"]) == 3
    assert result["licence"] == LICENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("q, limit", [("", 10), ("a", 10), ("ok query", 0), ("ok query", 21), ("x" * 161, 10)])
async def test_search_rejects_bad_input(q, limit):
    with pytest.raises(CompanyQueryError):
        await _service().search(q=q, limit=limit)


# --- HTTP routes and payment -----------------------------------------------

@pytest.fixture
def fixture_service(monkeypatch):
    monkeypatch.setattr(main, "company_service", _service())


@pytest.mark.parametrize(
    "path, params, amount",
    [
        ("/v1/uk/company", {"number": "00445790"}, "10000"),
        ("/v1/uk/company/search", {"q": "tesco"}, "10000"),
    ],
)
def test_unpaid_call_returns_402_with_requirements(path, params, amount):
    response = TestClient(main.app).get(path, params=params)
    assert response.status_code == 402
    decoded = json.loads(base64.b64decode(response.headers["payment-required"]))
    assert decoded["x402Version"] == 2
    accepts = decoded["accepts"][0]
    assert accepts["amount"] == amount
    assert accepts["payTo"] == main.PAY_TO
    assert accepts["asset"] == main.USDC_BASE
    assert accepts["network"] == "eip155:8453"
    assert decoded["resource"]["url"] == f"{main.BASE_URL}{path}"
    assert "bazaar" in decoded["extensions"]


def test_x402_lookup_settles_once(monkeypatch, fixture_service):
    recorder = Recorder()
    recorder.install(monkeypatch)
    response = TestClient(main.app).get(
        "/v1/uk/company", params={"number": "00445790", "filings": 3}, headers=_x402_headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_method"] == "x402"
    assert body["company"]["name"] == "TESCO PLC"
    assert len(recorder.settled) == 1
    assert recorder.verified[0]["requirements"].amount == "10000"
    assert "payment-response" in {key.lower() for key in response.headers}


def test_x402_unknown_company_is_not_settled(monkeypatch, fixture_service):
    recorder = Recorder()
    recorder.install(monkeypatch)
    response = TestClient(main.app).get(
        "/v1/uk/company", params={"number": "99999999"}, headers=_x402_headers()
    )
    assert response.status_code == 404
    assert recorder.settled == []


def test_x402_bad_number_is_not_settled(monkeypatch, fixture_service):
    recorder = Recorder()
    recorder.install(monkeypatch)
    response = TestClient(main.app).get(
        "/v1/uk/company", params={"number": "not-a-number"}, headers=_x402_headers()
    )
    assert response.status_code == 400
    assert recorder.settled == []


def test_x402_search_settles(monkeypatch, fixture_service):
    recorder = Recorder()
    recorder.install(monkeypatch)
    response = TestClient(main.app).get(
        "/v1/uk/company/search", params={"q": "widget solutions", "limit": 2}, headers=_x402_headers()
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 2
    assert len(recorder.settled) == 1


def test_api_key_lookup_deducts_one_credit(fixture_service):
    key = _insert_key(credits=5)
    response = TestClient(main.app).get(
        "/v1/uk/company", params={"number": "SC540749"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_method"] == "api_key"
    assert body["credits_remaining"] == 4
    assert _balance(key)[0] == 4


def test_api_key_upstream_failure_is_not_deducted(monkeypatch):
    monkeypatch.setattr(
        main, "company_service", _service(override={"/company/00445790": (500, "down")})
    )
    key = _insert_key(credits=5)
    response = TestClient(main.app).get(
        "/v1/uk/company", params={"number": "00445790"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 502
    assert _balance(key)[0] == 5


def test_llms_txt_and_root_list_company_routes():
    client = TestClient(main.app)
    text = client.get("/llms.txt").text
    assert "/v1/uk/company" in text
    assert "/v1/uk/company/search" in text
    root = client.get("/").json()
    assert root["endpoints"]["uk_company"]["price"] == main.COMPANY_PRICE
    assert root["endpoints"]["uk_company_search"]["path"] == "/v1/uk/company/search"


def test_new_company_first_accounts_and_statement_dates():
    company = parse_company_overview(_page("overview_16941632.html"))
    assert company["name"] == "TERRADEED LTD"
    assert company["accounts"]["next_made_up_to"] == "2027-01-31"
    assert company["accounts"]["next_due"] == "2027-10-05"
    assert company["confirmation_statement"]["next_made_up_to"] == "2027-01-04"
    assert company["confirmation_statement"]["next_due"] == "2027-01-18"
