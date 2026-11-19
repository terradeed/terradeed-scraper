"""Discovery documents: OpenAPI, /.well-known/x402, and glama.json.

These tests check metadata only. Prices and payment behaviour stay as they are.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import main

ROOT = Path(__file__).resolve().parents[1]
PAID = [
    ("post", "/scrape", main.SCRAPE_PRICE, main.SCRAPE_REQUIREMENTS, "0.010000"),
    ("post", "/extract", main.EXTRACT_PRICE, main.EXTRACT_REQUIREMENTS, "0.050000"),
    ("post", "/extract/property", main.PROPERTY_PRICE, main.PROPERTY_REQUIREMENTS, "0.100000"),
    ("get", "/v1/uk/tenders", main.TENDER_PRICE, main.TENDER_REQUIREMENTS, "0.020000"),
]
DASHES = ("\u2014", "\u2013")


def _schema():
    main.app.openapi_schema = None
    return main.app.openapi()


def test_prices_are_unchanged():
    assert [(method, path) for method, path in main.PAID_ROUTES] or main.PAID_ROUTES
    assert main.PAID_ROUTES == {
        ("POST", "/scrape"),
        ("POST", "/extract"),
        ("POST", "/extract/property"),
        ("GET", "/v1/uk/tenders"),
    }
    for _method, _path, label, requirements, decimal in PAID:
        assert label in {"$0.01", "$0.05", "$0.10", "$0.02"}
        assert main.usd_amount_from_atomic(requirements["amount"]) == decimal
        assert requirements["asset"] == main.USDC_BASE
        assert requirements["payTo"] == main.PAY_TO


def test_openapi_discovery_metadata():
    schema = _schema()
    info = schema["info"]
    assert schema["openapi"] == "3.1.0"
    assert info["title"] == "Terradeed Scrape API"
    assert "AI Automation for Business" not in json.dumps(info)
    assert info["version"] == main.APP_VERSION
    assert info["x-guidance"]
    assert info["contact"]["email"] == "contact@terradeed.co.uk"
    for dash in DASHES:
        assert dash not in info["title"]
        assert dash not in info["description"]
        assert dash not in info["x-guidance"]
    for label, _requirements, _decimal in [(row[2], row[3], row[4]) for row in PAID]:
        assert label in info["description"]
        assert label in info["x-guidance"]

    schemes = schema["components"]["securitySchemes"]
    assert schemes["x402"]["name"] == "PAYMENT-SIGNATURE"
    assert schemes["apiKey"]["scheme"] == "bearer"

    for method, path, label, requirements, decimal in PAID:
        operation = schema["paths"][path][method]
        payment = operation["x-payment-info"]
        assert payment["protocols"] == [{"x402": {}}]
        assert payment["price"] == {"mode": "fixed", "currency": "USD", "amount": decimal}
        assert operation["responses"]["402"]["description"] == "Payment Required"
        assert operation["security"] == [{"x402": []}, {"apiKey": []}]
        assert label in operation["description"]
        output = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert output["type"] == "object"
        assert output["properties"]
        for dash in DASHES:
            assert dash not in json.dumps(operation["description"])
        assert requirements["amount"] == str(int(decimal.replace(".", "")))


def test_non_paid_openapi_routes_are_marked_free():
    schema = _schema()
    paid = {(method, path) for method, path, *_rest in PAID}
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in main._OPENAPI_METHODS or not isinstance(operation, dict):
                continue
            if (method, path) in paid:
                assert "x-payment-info" in operation
                continue
            assert operation["security"] == []
            assert "x-payment-info" not in operation
            assert "402" not in operation.get("responses", {})


def test_well_known_x402_matches_the_catalog():
    response = TestClient(main.app).get("/.well-known/x402")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "max-age=300" in response.headers["cache-control"]
    document = response.json()
    assert document["x402Version"] == 2
    assert document["kind"] == "resource-server"
    assert document["name"] == "Terradeed Scrape API"
    assert document["openapi"] == f"{main.BASE_URL}/openapi.json"
    assert document["contact"] == "contact@terradeed.co.uk"
    for dash in DASHES:
        assert dash not in json.dumps(document)

    by_path = {item["path"]: item for item in document["resources"]}
    assert set(by_path) == {path for _method, path, *_rest in PAID}
    schema = _schema()
    for method, path, _label, requirements, decimal in PAID:
        resource = by_path[path]
        assert resource["method"] == method.upper()
        assert resource["url"] == f"{main.BASE_URL}{path}"
        assert resource["price"]["amount"] == decimal
        assert resource["price"] == schema["paths"][path][method]["x-payment-info"]["price"]
        accepts = resource["accepts"][0]
        assert accepts["amount"] == requirements["amount"]
        assert accepts["asset"] == requirements["asset"]
        assert accepts["payTo"] == requirements["payTo"]
        assert accepts["scheme"] == requirements["scheme"]
        assert accepts["network"] == requirements["network"]


def test_discovery_routes_do_not_change_payment_behaviour():
    client = TestClient(main.app)
    manifest = client.get("/.well-known/x402")
    assert manifest.status_code == 200
    missing = client.get("/bazaar.json")
    assert missing.status_code == 404
    unpaid = client.post("/scrape", json={"url": "https://example.com"})
    assert unpaid.status_code == 402
    assert unpaid.json()["error"] == "Payment required"


def test_readme_promises_only_served_discovery_documents():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/openapi.json" in readme
    assert "/.well-known/x402" in readme
    assert "bazaar.json" not in readme
    assert "58838" not in readme
    assert "search_endpoints" not in readme
    assert "Agentic Market" not in readme
    assert "Coinbase CDP x402 Bazaar" in readme
    assert "https://www.x402scan.com" in readme
    for dash in DASHES:
        assert dash not in readme


def test_glama_metadata_matches_current_routes():
    document = json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))
    assert document["$schema"] == "https://glama.ai/mcp/schemas/server.json"
    assert document["maintainers"] == ["terradeed"]
    assert document["name"] == "Terradeed Scrape API"
    description = document["description"]
    for dash in DASHES:
        assert dash not in description
        assert dash not in document["name"]
    for _method, path, label, *_rest in PAID:
        assert path in description
        assert label in description
    assert "AI Automation for Business" not in description
