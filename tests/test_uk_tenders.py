"""UK tenders feed: official OCDS payloads in, public cards out. No network."""

import json

import httpx
import pytest

from uk_tenders import (
    ATTRIBUTION,
    LICENCE,
    TenderQueryError,
    TenderService,
    TenderUpstreamError,
    _matches_region,
    grants_stub,
    normalise_release,
    redact_text,
)

CONTACT_EMAIL = "procurement.officer@example.test"
CONTACT_NAME = "Jane Example"
CONTACT_PHONE = "+44 7700 900123"
SECRET_DESCRIPTION = "Ask the named officer for the full specification pack."


def _fts_release(**overrides):
    release = {
        "ocid": "ocds-h6vhtk-0000aa",
        "id": "000111-2026",
        "date": "2026-11-03T12:00:00Z",
        "tag": ["tender"],
        "tender": {
            "title": "AI training for library staff",
            "description": (
                f"Contact {CONTACT_NAME} at {CONTACT_EMAIL} or {CONTACT_PHONE}. "
                f"{SECRET_DESCRIPTION}"
            ),
            "value": {"amount": 120000, "currency": "GBP"},
            "tenderPeriod": {"endDate": "2026-12-01T17:00:00Z"},
            "items": [{"deliveryAddresses": [{"region": "UKD3", "countryName": "United Kingdom"}]}],
        },
        "buyer": {"id": "GB-FTS-1", "name": "Manchester City Council"},
        "parties": [
            {
                "id": "GB-FTS-1",
                "name": "Manchester City Council",
                "identifier": {"legalName": "Manchester City Council"},
                "roles": ["buyer"],
                "address": {
                    "streetAddress": "Town Hall",
                    "locality": "Manchester",
                    "postalCode": "M60 2LA",
                    "countryName": "United Kingdom",
                },
                "contactPoint": {
                    "name": CONTACT_NAME,
                    "email": CONTACT_EMAIL,
                    "telephone": CONTACT_PHONE,
                },
            }
        ],
    }
    release.update(overrides)
    return release


def _cf_release(**overrides):
    release = {
        "ocid": "ocds-b5fd17-11111111-2222-3333-4444-555555555555",
        "id": "11111111-2222-3333-4444-555555555555-9",
        "date": "2026-11-04T09:00:00Z",
        "tag": ["tender"],
        "tender": {
            "title": "School catering contract",
            "description": "Hot meals for primary schools.",
            "value": {"amount": 50000, "currency": "GBP"},
            "tenderPeriod": {"endDate": "2026-11-20T12:00:00Z"},
            "items": [
                {
                    "deliveryAddresses": [
                        {"region": "North West", "countryName": "United Kingdom"}
                    ]
                }
            ],
        },
        "buyer": {"id": "GB-SRS-1", "name": "Example Borough Council"},
        "parties": [
            {
                "id": "GB-SRS-1",
                "name": "Example Borough Council",
                "identifier": {"legalName": "Example Borough Council"},
                "roles": ["buyer"],
                "contactPoint": {
                    "name": "Sam Contact",
                    "email": "sam.contact@example.test",
                },
            }
        ],
    }
    release.update(overrides)
    return release


def _package(releases, next_url=None):
    body = {
        "license": "http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
        "releases": releases,
    }
    if next_url:
        body["links"] = {"next": next_url}
    return body


class _Router:
    def __init__(self, fts_pages, cf_pages):
        self.fts_pages = list(fts_pages)
        self.cf_pages = list(cf_pages)
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if "find-tender" in url:
            payload = self.fts_pages.pop(0) if self.fts_pages else _package([])
        elif "contractsfinder" in url:
            payload = self.cf_pages.pop(0) if self.cf_pages else _package([])
        else:
            return httpx.Response(404, json={"error": "unexpected host"})
        return httpx.Response(200, json=payload)


def _service(router, **kwargs):
    return TenderService(
        transport=httpx.MockTransport(router),
        min_interval=0,
        **kwargs,
    )


def _assert_no_personal_data(payload):
    encoded = json.dumps(payload)
    assert CONTACT_EMAIL not in encoded
    assert CONTACT_NAME not in encoded
    assert CONTACT_PHONE not in encoded
    assert "sam.contact@example.test" not in encoded
    assert "Sam Contact" not in encoded
    assert SECRET_DESCRIPTION not in encoded

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in {
                    "contactpoint",
                    "email",
                    "telephone",
                    "faxnumber",
                    "phone",
                }
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)


@pytest.mark.asyncio
async def test_happy_path_merges_sources_and_strips_contacts():
    router = _Router(
        fts_pages=[_package([_fts_release()])],
        cf_pages=[_package([_cf_release()])],
    )
    service = _service(router)
    result = await service.search(q="training", region="north-west", limit=25)
    cached = json.dumps(service._http_cache)
    assert CONTACT_EMAIL not in cached
    assert CONTACT_NAME not in cached
    assert CONTACT_PHONE not in cached

    assert result["licence"] == LICENCE
    assert result["attribution"] == ATTRIBUTION
    assert result["sources"] == ["find-tender", "contracts-finder"]
    assert [item["ocid"] for item in result["results"]] == ["ocds-h6vhtk-0000aa"]
    card = result["results"][0]
    assert card["title"] == "AI training for library staff"
    assert card["buyer"] == "Manchester City Council"
    assert card["value_gbp"] == 120000
    assert card["published"] == "2026-11-03T12:00:00Z"
    assert card["deadline"] == "2026-12-01T17:00:00Z"
    assert card["stage"] == "tender"
    assert card["region_hints"] == ["north-west"]
    assert card["url"] == "https://www.find-tender.service.gov.uk/Notice/000111-2026"
    assert card["source"] == "find-tender"
    assert set(card) == {
        "ocid",
        "title",
        "buyer",
        "value_gbp",
        "published",
        "deadline",
        "stage",
        "region_hints",
        "url",
        "source",
    }
    _assert_no_personal_data(result)
    assert len(router.calls) == 2


@pytest.mark.asyncio
async def test_empty_results_keep_attribution():
    router = _Router(
        fts_pages=[_package([_fts_release()])],
        cf_pages=[_package([_cf_release()])],
    )
    result = await _service(router).search(q="no-such-procurement-xyz")

    assert result["results"] == []
    assert result["next_cursor"] is None
    assert result["licence"] == LICENCE
    assert result["attribution"] == ATTRIBUTION
    assert "Open Government Licence v3.0" in result["attribution"]
    assert result["licence_url"].endswith("/version/3/")
    assert result["sources"] == ["find-tender", "contracts-finder"]
    _assert_no_personal_data(result)


@pytest.mark.asyncio
async def test_attribution_present_on_mixed_results():
    router = _Router(
        fts_pages=[_package([_fts_release()])],
        cf_pages=[_package([_cf_release()])],
    )
    result = await _service(router).search(region="uk")

    assert result["attribution"]
    assert result["licence"] == "OGL-UK-3.0"
    assert [item["source"] for item in result["results"]] == [
        "contracts-finder",
        "find-tender",
    ]
    assert result["results"][0]["url"].endswith(
        "/Notice/11111111-2222-3333-4444-555555555555"
    )
    _assert_no_personal_data(result)


@pytest.mark.asyncio
async def test_keyword_matches_description_without_returning_it():
    release = _fts_release()
    release["tender"]["title"] = "Library support services"
    release["tender"]["description"] = "BridgeAI deployment for front-line teams."
    router = _Router(fts_pages=[_package([release])], cf_pages=[_package([])])
    result = await _service(router).search(q="BridgeAI")

    assert len(result["results"]) == 1
    assert "BridgeAI" not in json.dumps(result["results"])
    assert "front-line" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cursor_pages_cached_results_without_another_fetch():
    releases = []
    for index in range(30):
        releases.append(
            _fts_release(
                ocid=f"ocds-h6vhtk-{index:04d}",
                id=f"{index:06d}-2026",
                date=f"2026-11-{(index % 27) + 1:02d}T00:00:00Z",
            )
        )
    router = _Router(fts_pages=[_package(releases)], cf_pages=[_package([])])
    service = _service(router)
    first = await service.search(limit=25)
    assert len(first["results"]) == 25
    assert first["next_cursor"]

    second = await service.search(limit=25, cursor=first["next_cursor"])
    assert len(second["results"]) == 5
    assert second["next_cursor"] is None
    assert second["attribution"] == ATTRIBUTION
    assert len(router.calls) == 2


@pytest.mark.asyncio
async def test_filtered_search_reads_a_second_page_and_stops():
    noise = _fts_release(
        ocid="ocds-h6vhtk-noise",
        id="000001-2026",
        date="2026-11-01T00:00:00Z",
    )
    noise["tender"]["title"] = "Unrelated stationery"
    noise["tender"]["description"] = "Paper"
    match = _fts_release(ocid="ocds-h6vhtk-match", id="000002-2026")
    next_url = (
        "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?"
        "cursor=page2"
    )
    router = _Router(
        fts_pages=[_package([noise], next_url=next_url), _package([match])],
        cf_pages=[_package([])],
    )
    result = await _service(router).search(q="library staff")

    assert [item["ocid"] for item in result["results"]] == ["ocds-h6vhtk-match"]
    assert sum("find-tender" in url for url in router.calls) == 2
    assert any("cursor=page2" in url for url in router.calls)
    assert result["coverage"]["truncated"] is False


@pytest.mark.asyncio
async def test_broad_search_does_not_walk_past_the_first_page():
    next_url = (
        "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?"
        "cursor=page2"
    )
    router = _Router(
        fts_pages=[_package([_fts_release()], next_url=next_url), _package([])],
        cf_pages=[_package([])],
    )
    result = await _service(router).search()

    assert len(result["results"]) == 1
    assert result["coverage"]["truncated"] is True
    assert result["coverage"]["pages_fetched"]["find-tender"] == 1
    assert sum("find-tender" in url for url in router.calls) == 1


@pytest.mark.asyncio
async def test_repeat_search_uses_the_http_cache():
    router = _Router(
        fts_pages=[_package([_fts_release()])],
        cf_pages=[_package([_cf_release()])],
    )
    service = _service(router)
    await service.search(q="training")
    await service.search(q="catering")
    assert len(router.calls) == 2


@pytest.mark.asyncio
async def test_rate_limit_does_not_retry_and_returns_the_other_source():
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(str(request.url))
        if "contractsfinder" in str(request.url):
            return httpx.Response(403, text="rate limit")
        return httpx.Response(200, json=_package([_fts_release()]))

    handler.calls = []
    service = TenderService(transport=httpx.MockTransport(handler), min_interval=0)
    result = await service.search(q="training")
    assert result["results"][0]["source"] == "find-tender"
    assert result["sources"] == ["find-tender"]
    assert any("contracts-finder rate limited" in warning for warning in result["warnings"])
    assert result["attribution"] == ATTRIBUTION

    await service.search(q="library")
    cf_calls = [url for url in handler.calls if "contractsfinder" in url]
    assert len(cf_calls) == 1


@pytest.mark.asyncio
async def test_both_sources_down_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "11"}, text="unavailable")

    service = TenderService(transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(TenderUpstreamError):
        await service.search()


@pytest.mark.asyncio
async def test_invalid_query_parameters():
    service = TenderService(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(TenderQueryError):
        await service.search(region="mars")
    with pytest.raises(TenderQueryError):
        await service.search(stage="implementation")
    with pytest.raises(TenderQueryError):
        await service.search(limit=26)
    with pytest.raises(TenderQueryError):
        await service.search(updated_from="11/01/2026")


def test_redaction_keeps_contract_years_and_drops_contacts():
    text = redact_text(
        f"Framework 2024-2026. Email {CONTACT_EMAIL} or {CONTACT_PHONE}. Budget 120000."
    )
    assert "2024-2026" in text
    assert "120000" in text
    assert CONTACT_EMAIL not in text
    assert CONTACT_PHONE not in text
    assert "[redacted]" in text


def test_postcode_fallback_and_award_value():
    release = _fts_release()
    release["tag"] = ["award"]
    release["tender"]["items"] = [
        {"deliveryAddresses": [{"postalCode": "M1 1AE", "countryName": "United Kingdom"}]}
    ]
    release["tender"]["value"] = {"amount": 10, "currency": "EUR"}
    release["awards"] = [{"value": {"amount": 88000, "currency": "GBP"}}]
    card = normalise_release("find-tender", release)
    assert card["stage"] == "award"
    assert card["region_hints"] == ["north-west"]
    assert card["value_gbp"] == 88000


def test_national_notice_matches_every_region_filter():
    release = _fts_release()
    release["tender"]["items"] = [
        {"deliveryAddresses": [{"region": "UK", "countryName": "United Kingdom"}]}
    ]
    card = normalise_release("find-tender", release)
    assert "uk" in card["region_hints"]
    assert _matches_region(card["region_hints"], "north-west")
    assert _matches_region(card["region_hints"], "scotland")
    assert _matches_region(card["region_hints"], "england")


def test_grants_stub_republishes_nothing():
    payload = grants_stub()
    assert payload["available"] is False
    assert payload["results"] == []
    assert payload["sources"] == []
    assert "contact Innovate UK" in payload["note"]
    assert "The Chest" in payload["note"]
    assert "BridgeAI" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_cursor_expires_with_the_cache():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    releases = [
        _fts_release(ocid=f"ocds-h6vhtk-{index:04d}", id=f"{index:06d}-2026")
        for index in range(30)
    ]
    router = _Router(fts_pages=[_package(releases)], cf_pages=[_package([])])
    service = _service(router, cache_ttl=10, mono=clock)
    first = await service.search(limit=10)
    clock.t = 11
    with pytest.raises(TenderQueryError, match="expired"):
        await service.search(limit=10, cursor=first["next_cursor"])
