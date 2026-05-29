"""
TerraDeed Labs — x402 Web Scraping API

Wallet:    0x4E024e356bd01853654b7B5196F2B85F67Cc39EC  (Base mainnet)
Endpoints:
    POST /scrape   — $0.01 USDC — clean LLM-ready markdown from any URL
    POST /extract  — $0.05 USDC — schema-driven structured JSON extraction
Network:   Base mainnet (eip155:8453 internally / base for JS clients)
Facilitator: xpay (https://facilitator.xpay.sh)

Set these env vars in Railway:
    ANTHROPIC_API_KEY=your-anthropic-key  (required for /extract)
    CDP_API_KEY_ID=your-key-id            (optional)
    CDP_API_KEY_SECRET=your-secret        (optional)
"""

import base64
import json
import os
from typing import Any, Optional

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from x402.http import (
    CreateHeadersAuthProvider,
    FacilitatorConfig,
    HTTPFacilitatorClient,
    PaymentOption,
)
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

# ── Config ────────────────────────────────────────────────────────────────────

PAY_TO           = "0x4E024e356bd01853654b7B5196F2B85F67Cc39EC"
SCRAPE_PRICE     = "$0.01"
EXTRACT_PRICE    = "$0.05"

NETWORK_INTERNAL = "eip155:8453"
NETWORK_CLIENT   = "base"

FACILITATOR      = "https://facilitator.xpay.sh"
BASE_URL         = "https://api.terradeed.co.uk"
USDC_BASE        = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXTRACT_MODEL    = "claude-sonnet-4-20250514"

CDP_API_KEY_ID     = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

# ── x402 accepts arrays ───────────────────────────────────────────────────────
# extra.name and extra.version are critical for EIP-712 domain alignment.
#
# The x402 JS SDK signAuthorization() uses extra?.name and extra?.version
# when building the EIP-712 domain for signing. Without these, name/version
# are undefined and excluded from the domain hash. The server's verify()
# falls back to config["8453"].usdcName ("USD Coin") and getVersion() ("2"),
# producing a different domain and an invalid signature recovery.
#
# Providing extra.name and extra.version explicitly forces both signing and
# verification to use the same EIP-712 domain — making the signature valid.

USDC_EXTRA = {"name": "USD Coin", "version": "2"}

SCRAPE_ACCEPTS = [
    {
        "scheme": "exact",
        "network": NETWORK_CLIENT,
        "asset": USDC_BASE,
        "maxAmountRequired": "10000",
        "payTo": PAY_TO,
        "resource": f"{BASE_URL}/scrape",
        "description": "Scrape any public URL — clean LLM-ready markdown",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": USDC_EXTRA,
    }
]

EXTRACT_ACCEPTS = [
    {
        "scheme": "exact",
        "network": NETWORK_CLIENT,
        "asset": USDC_BASE,
        "maxAmountRequired": "50000",
        "payTo": PAY_TO,
        "resource": f"{BASE_URL}/extract",
        "description": "Schema-driven structured JSON extraction from any URL",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": USDC_EXTRA,
    }
]

ROUTE_ACCEPTS = {
    "POST /scrape":  SCRAPE_ACCEPTS,
    "POST /extract": EXTRACT_ACCEPTS,
}

# ── Network normalisation middleware ──────────────────────────────────────────

class NetworkNormalisationMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = list(scope.get("headers", []))
            new_headers = []
            for name, value in headers:
                if name.lower() == b"x-payment":
                    try:
                        decoded = json.loads(base64.b64decode(value).decode("utf-8"))
                        if decoded.get("network") == NETWORK_CLIENT:
                            decoded["network"] = NETWORK_INTERNAL
                            value = base64.b64encode(json.dumps(decoded).encode("utf-8"))
                    except Exception:
                        pass
                new_headers.append((name, value))
            scope = {**scope, "headers": new_headers}
        await self.app(scope, receive, send)


# ── ASGI middleware: injects accepts array into 402 response body ─────────────

class X402ResponseBodyMiddleware:
    def __init__(self, app: Any, route_accepts: dict[str, list]) -> None:
        self.app = app
        self.route_accepts = route_accepts

    def _build_body(self, method: str, path: str) -> bytes:
        route_key = f"{method} {path}"
        accepts = self.route_accepts.get(route_key, [])
        return json.dumps({"x402Version": 2, "accepts": accepts, "error": "Payment required"}).encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        status_code: Optional[int] = None
        body = self._build_body(method, path)

        async def send_wrapper(message: Any) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if status_code == 402:
                    headers = {k: v for k, v in message.get("headers", [])}
                    headers[b"content-type"] = b"application/json"
                    headers[b"content-length"] = str(len(body)).encode()
                    message = {"type": "http.response.start", "status": 402, "headers": list(headers.items())}
            elif message["type"] == "http.response.body" and status_code == 402:
                message = {"type": "http.response.body", "body": body, "more_body": False}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ── CDP JWT Auth Provider ─────────────────────────────────────────────────────

def _build_cdp_auth_provider() -> CreateHeadersAuthProvider | None:
    if not CDP_API_KEY_ID or not CDP_API_KEY_SECRET:
        return None
    try:
        from cdp.auth import GetAuthHeadersOptions, get_auth_headers
        CDP_HOST      = "api.cdp.coinbase.com"
        CDP_BASE_PATH = "/platform/v2/x402"

        def create_headers() -> dict[str, dict[str, str]]:
            def _auth(method: str, path: str) -> dict[str, str]:
                opts = GetAuthHeadersOptions(
                    api_key_id=CDP_API_KEY_ID, api_key_secret=CDP_API_KEY_SECRET,
                    request_method=method, request_host=CDP_HOST, request_path=path,
                )
                return {"Authorization": get_auth_headers(opts)["Authorization"]}
            return {
                "verify":    _auth("POST", f"{CDP_BASE_PATH}/verify"),
                "settle":    _auth("POST", f"{CDP_BASE_PATH}/settle"),
                "supported": _auth("GET",  f"{CDP_BASE_PATH}/supported"),
            }
        return CreateHeadersAuthProvider(create_headers)
    except ImportError:
        print("WARNING: cdp-sdk not installed.")
        return None


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="TerraDeed Scrape API",
    description="Pay-per-use web scraping and structured data extraction via x402 USDC micropayments.",
    version="0.6.0",
)

auth_provider = _build_cdp_auth_provider()
facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR, auth_provider=auth_provider))
server = x402ResourceServer(facilitator)
server.register(NETWORK_INTERNAL, ExactEvmServerScheme())

routes: dict[str, RouteConfig] = {
    "POST /scrape": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=PAY_TO, price=SCRAPE_PRICE, network=NETWORK_INTERNAL)],
        mime_type="application/json",
        description="Scrape any public URL — clean LLM-ready markdown. $0.01 USDC on Base.",
    ),
    "POST /extract": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=PAY_TO, price=EXTRACT_PRICE, network=NETWORK_INTERNAL)],
        mime_type="application/json",
        description="Schema-driven structured JSON extraction. $0.05 USDC on Base.",
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
app.add_middleware(X402ResponseBodyMiddleware, route_accepts=ROUTE_ACCEPTS)
app.add_middleware(NetworkNormalisationMiddleware)

# ── Models ────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    js_render: bool = False

class ScrapeResponse(BaseModel):
    content: str
    url: str
    status: str
    word_count: int
    title: Optional[str] = None
    js_rendered: bool = False

class ExtractRequest(BaseModel):
    url: str
    fields: list[str]
    js_render: bool = False

class ExtractResponse(BaseModel):
    url: str
    status: str
    data: dict[str, Any]
    fields_requested: list[str]
    fields_extracted: list[str]
    js_rendered: bool = False
    model: str = EXTRACT_MODEL

# ── Static scraping ───────────────────────────────────────────────────────────

def _fetch_static(url: str) -> tuple[str, str | None]:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        response.raise_for_status()
        return response.text, None
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Timeout fetching {url}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch {url}: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch {url}: {str(e)}")

# ── JS rendering ──────────────────────────────────────────────────────────────

async def _fetch_with_playwright(url: str) -> str:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Playwright failed to render {url}: {str(e)}")

# ── Content extraction ────────────────────────────────────────────────────────

def _extract_content(html: str) -> tuple[str | None, str | None]:
    content = trafilatura.extract(html, output_format="markdown", include_links=False, include_images=False, include_tables=True, no_fallback=False)
    meta = trafilatura.extract_metadata(html)
    return content, (meta.title if meta else None)

# ── Core scrape logic ─────────────────────────────────────────────────────────

async def _scrape(url: str, js_render: bool = False) -> dict[str, Any]:
    js_rendered = False
    if js_render:
        html = await _fetch_with_playwright(url)
        js_rendered = True
    else:
        html, _ = _fetch_static(url)

    content, title = _extract_content(html)

    if not content and not js_render:
        try:
            html = await _fetch_with_playwright(url)
            content, title = _extract_content(html)
            js_rendered = True
        except Exception:
            pass

    if not content:
        raise HTTPException(status_code=422, detail=f"Could not extract meaningful content from {url}.")

    return {"content": content, "url": url, "status": "success", "word_count": len(content.split()), "title": title, "js_rendered": js_rendered}

# ── Structured extraction ─────────────────────────────────────────────────────

async def _extract_structured(markdown: str, url: str, fields: list[str], js_rendered: bool) -> dict[str, Any]:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY missing.")

    fields_str = ", ".join(f'"{f}"' for f in fields)
    prompt = f"""Extract the following fields from the page content below.

Fields: [{fields_str}]

Return ONLY a valid JSON object. Set missing fields to null. No explanation, no markdown.

URL: {url}

Content:
{markdown[:8000]}"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": EXTRACT_MODEL, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Extraction model error: {response.status_code}")

    try:
        data = json.loads(response.json()["content"][0]["text"].strip())
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Extraction model returned malformed JSON.")

    return {"url": url, "status": "success", "data": data, "fields_requested": fields, "fields_extracted": [k for k, v in data.items() if v is not None], "js_rendered": js_rendered, "model": EXTRACT_MODEL}

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(body: ScrapeRequest) -> dict[str, Any]:
    return await _scrape(body.url, body.js_render)


@app.post("/extract", response_model=ExtractResponse)
async def extract(body: ExtractRequest) -> dict[str, Any]:
    if not body.fields:
        raise HTTPException(status_code=422, detail="At least one field must be specified.")
    if len(body.fields) > 20:
        raise HTTPException(status_code=422, detail="Maximum 20 fields per request.")
    scrape_result = await _scrape(body.url, body.js_render)
    return await _extract_structured(scrape_result["content"], body.url, body.fields, scrape_result["js_rendered"])


@app.get("/")
async def root() -> dict[str, Any]:
    return {"name": "TerraDeed Scrape API", "version": "0.6.0", "endpoints": {"POST /scrape": {"price": SCRAPE_PRICE}, "POST /extract": {"price": EXTRACT_PRICE}}, "payment": {"protocol": "x402", "network": NETWORK_CLIENT, "facilitator": FACILITATOR}, "docs": "/docs"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.6.0", "cdp_auth": "configured" if CDP_API_KEY_ID else "missing", "anthropic": "configured" if ANTHROPIC_API_KEY else "missing", "network": NETWORK_CLIENT, "capabilities": "static+js-rendering+structured-extraction"}


@app.get("/bazaar.json")
async def bazaar_manifest() -> dict[str, Any]:
    return {
        "resources": [
            {"url": f"{BASE_URL}/scrape", "method": "POST", "name": "TerraDeed Web Scraper", "description": "Pay-per-use web scraping. Clean LLM-ready markdown from any URL, including JS-rendered SPAs.", "category": "search", "tags": ["scraping", "web-data", "markdown", "llm", "ai-agent", "playwright"], "input": {"url": "https://example.com", "js_render": False}, "pricing": {"amount": "0.01", "currency": "USDC", "network": NETWORK_CLIENT}},
            {"url": f"{BASE_URL}/extract", "method": "POST", "name": "TerraDeed Structured Extractor", "description": "Schema-driven structured JSON extraction. Pass fields, receive typed JSON.", "category": "search", "tags": ["extraction", "structured-data", "json", "llm", "ai-agent"], "input": {"url": "https://example.com/product", "fields": ["price", "title", "availability"]}, "pricing": {"amount": "0.05", "currency": "USDC", "network": NETWORK_CLIENT}},
        ]
    }


@app.get("/.well-known/x402")
async def well_known_x402() -> dict[str, Any]:
    return {
        "version": 2,
        "resources": [
            {"url": f"{BASE_URL}/scrape", "method": "POST", "description": "LLM-ready markdown from any URL.", "accepts": SCRAPE_ACCEPTS, "info": {"name": "TerraDeed Web Scraper", "category": "search", "tags": ["scraping", "web-data", "markdown", "llm"]}},
            {"url": f"{BASE_URL}/extract", "method": "POST", "description": "Schema-driven structured JSON extraction.", "accepts": EXTRACT_ACCEPTS, "info": {"name": "TerraDeed Structured Extractor", "category": "search", "tags": ["extraction", "structured-data", "json", "llm"]}},
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
