"""
TerraDeed Labs — x402 Web Scraping API

Wallet:    0x4E024e356bd01853654b7B5196F2B85F67Cc39EC  (Base mainnet)
Price:     $0.01 USDC per call
Network:   Base mainnet (eip155:8453)
Facilitator: xpay (https://facilitator.xpay.sh)

Set these env vars in Railway:
    CDP_API_KEY_ID=your-key-id
    CDP_API_KEY_SECRET=your-secret
"""

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

PAY_TO      = "0x4E024e356bd01853654b7B5196F2B85F67Cc39EC"
PRICE       = "$0.01"
NETWORK     = "eip155:8453"          # Base mainnet
FACILITATOR = "https://facilitator.xpay.sh"
BASE_URL    = "https://api.terradeed.co.uk"

CDP_API_KEY_ID     = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")

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
                    api_key_id=CDP_API_KEY_ID,
                    api_key_secret=CDP_API_KEY_SECRET,
                    request_method=method,
                    request_host=CDP_HOST,
                    request_path=path,
                )
                headers = get_auth_headers(opts)
                return {"Authorization": headers["Authorization"]}

            return {
                "verify":    _auth("POST", f"{CDP_BASE_PATH}/verify"),
                "settle":    _auth("POST", f"{CDP_BASE_PATH}/settle"),
                "supported": _auth("GET",  f"{CDP_BASE_PATH}/supported"),
            }

        return CreateHeadersAuthProvider(create_headers)

    except ImportError:
        print("WARNING: cdp-sdk not installed. Install it to use CDP facilitator.")
        return None


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="TerraDeed Scrape API",
    description="Pay-per-use web scraping. Returns clean LLM-ready markdown via x402 USDC micropayments. Supports JS rendering via Playwright.",
    version="0.4.0",
)

# ── x402 payment infrastructure ───────────────────────────────────────────────

auth_provider = _build_cdp_auth_provider()

facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(
        url=FACILITATOR,
        auth_provider=auth_provider,
    )
)

server = x402ResourceServer(facilitator)
server.register(NETWORK, ExactEvmServerScheme())

routes: dict[str, RouteConfig] = {
    "POST /scrape": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=PAY_TO,
                price=PRICE,
                network=NETWORK,
            ),
        ],
        mime_type="application/json",
        description=(
            "Scrape any public URL and receive clean LLM-ready markdown. "
            f"Price: {PRICE} USDC per call on Base mainnet. "
            "Supports JS rendering for SPAs and dynamic sites."
        ),
        extensions={
            "bazaar": {
                "discoverable": True,
                "category": "search",
                "tags": ["scraping", "web-data", "markdown", "llm", "ai-agent", "playwright", "js-rendering"],
                "info": {
                    "name": "TerraDeed Web Scraper",
                    "description": "Pay-per-use web scraping API. Extracts clean LLM-ready markdown from any URL including JS-rendered SPAs. Returns title, word count, and content.",
                    "input": {
                        "url": "https://example.com",
                        "js_render": False,
                    },
                    "output": {
                        "description": "Clean LLM-ready markdown extracted from the target URL, with title, word count, and render method used.",
                        "content_type": "application/json",
                        "example": {
                            "content": "## Example Domain\n\nThis domain is for use in illustrative examples.",
                            "url": "https://example.com",
                            "status": "success",
                            "word_count": 14,
                            "title": "Example Domain",
                            "js_rendered": False,
                        },
                    },
                },
                "schema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Clean LLM-ready markdown extracted from the URL"},
                        "url": {"type": "string", "description": "The URL that was scraped"},
                        "status": {"type": "string", "description": "success or error"},
                        "word_count": {"type": "integer", "description": "Number of words in extracted content"},
                        "title": {"type": "string", "description": "Page title"},
                        "js_rendered": {"type": "boolean", "description": "Whether Playwright JS rendering was used"},
                    },
                    "required": ["content", "url", "status"],
                },
            }
        },
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)

# ── Request / Response models ─────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    js_render: bool = False  # Set True to use Playwright for JS-heavy sites

class ScrapeResponse(BaseModel):
    content: str
    url: str
    status: str
    word_count: int
    title: Optional[str] = None
    js_rendered: bool = False

# ── Static scraping (httpx + trafilatura) ─────────────────────────────────────

def _fetch_static(url: str) -> tuple[str, str | None]:
    """Fetch URL with httpx. Returns (html, title) or raises HTTPException."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
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

# ── JS rendering (Playwright) ─────────────────────────────────────────────────

async def _fetch_with_playwright(url: str) -> str:
    """Fetch URL using Playwright for JS-rendered content. Returns HTML."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Playwright failed to render {url}: {str(e)}")

# ── Content extraction ────────────────────────────────────────────────────────

def _extract_content(html: str) -> tuple[str | None, str | None]:
    """Extract markdown content and title from HTML using trafilatura."""
    content = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_tables=True,
        no_fallback=False,
    )
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta else None
    return content, title

# ── Main scrape logic ─────────────────────────────────────────────────────────

async def _scrape(url: str, js_render: bool = False) -> dict[str, Any]:
    """
    Scrape URL and return clean LLM-ready markdown.
    If js_render=True, uses Playwright.
    If js_render=False, tries httpx first, falls back to Playwright if content is empty.
    """
    js_rendered = False

    if js_render:
        # Explicit JS rendering requested
        html = await _fetch_with_playwright(url)
        js_rendered = True
    else:
        # Try static first
        html, _ = _fetch_static(url)

    content, title = _extract_content(html)

    # Auto-fallback to Playwright if static fetch returned empty content
    if not content and not js_render:
        try:
            html = await _fetch_with_playwright(url)
            content, title = _extract_content(html)
            js_rendered = True
        except Exception:
            pass  # Playwright fallback failed, handle below

    if not content:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract meaningful content from {url}. "
                   "The page may be behind authentication or blocking automated access."
        )

    return {
        "content": content,
        "url": url,
        "status": "success",
        "word_count": len(content.split()),
        "title": title,
        "js_rendered": js_rendered,
    }

# ── Protected endpoint ────────────────────────────────────────────────────────

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(body: ScrapeRequest) -> dict[str, Any]:
    """
    Requires x402 payment ($0.01 USDC on Base mainnet).
    Returns clean LLM-ready markdown extracted from the target URL.
    Set js_render=true for JS-heavy SPAs and dynamic sites.
    """
    return await _scrape(body.url, body.js_render)

# ── Free meta endpoints ───────────────────────────────────────────────────────

@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "TerraDeed Scrape API",
        "version": "0.4.0",
        "capabilities": ["static-scraping", "js-rendering"],
        "endpoints": {
            "POST /scrape": {
                "protected": True,
                "price": PRICE,
                "network": NETWORK,
                "body": {
                    "url": "string (required)",
                    "js_render": "boolean (optional, default false)",
                },
            }
        },
        "payment": {"protocol": "x402", "facilitator": FACILITATOR},
        "docs": "/docs",
    }

@app.get("/health")
async def health() -> dict[str, str]:
    cdp_configured = bool(CDP_API_KEY_ID and CDP_API_KEY_SECRET)
    return {
        "status": "ok",
        "version": "0.4.0",
        "cdp_auth": "configured" if cdp_configured else "missing",
        "network": NETWORK,
        "capabilities": "static+js-rendering",
    }

@app.get("/bazaar.json")
async def bazaar_manifest() -> dict[str, Any]:
    """Static Bazaar discovery manifest."""
    return {
        "resources": [
            {
                "url": f"{BASE_URL}/scrape",
                "method": "POST",
                "name": "TerraDeed Web Scraper",
                "description": "Pay-per-use web scraping API. Extracts clean LLM-ready markdown from any URL including JS-rendered SPAs. Returns title, word count, and content.",
                "category": "search",
                "tags": ["scraping", "web-data", "markdown", "llm", "ai-agent", "playwright", "js-rendering"],
                "input": {"url": "https://example.com", "js_render": False},
                "output": {
                    "description": "Clean LLM-ready markdown extracted from the target URL, with title, word count, and render method.",
                    "content_type": "application/json",
                    "example": {
                        "content": "## Example Domain\n\nThis domain is for use in illustrative examples.",
                        "url": "https://example.com",
                        "status": "success",
                        "word_count": 14,
                        "title": "Example Domain",
                        "js_rendered": False,
                    },
                    "schema": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Clean LLM-ready markdown extracted from the URL"},
                            "url": {"type": "string", "description": "The URL that was scraped"},
                            "status": {"type": "string", "description": "success or error"},
                            "word_count": {"type": "integer", "description": "Number of words in extracted content"},
                            "title": {"type": "string", "description": "Page title"},
                            "js_rendered": {"type": "boolean", "description": "Whether Playwright JS rendering was used"},
                        },
                        "required": ["content", "url", "status"],
                    }
                },
                "pricing": {
                    "amount": "0.01",
                    "currency": "USDC",
                    "network": "eip155:8453",
                }
            }
        ]
    }

@app.get("/.well-known/x402")
async def well_known_x402() -> dict[str, Any]:
    """Standard x402 discovery endpoint for crawlers and indexers."""
    return {
        "version": 2,
        "resources": [
            {
                "url": f"{BASE_URL}/scrape",
                "method": "POST",
                "description": "Pay-per-use web scraping API. Extracts clean LLM-ready markdown from any URL. Supports JS rendering for SPAs.",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "eip155:8453",
                        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "10000",
                        "payTo": PAY_TO,
                    }
                ],
                "info": {
                    "name": "TerraDeed Web Scraper",
                    "category": "search",
                    "tags": ["scraping", "web-data", "markdown", "llm", "ai-agent", "playwright", "js-rendering"],
                    "input": {"url": "https://example.com", "js_render": False},
                    "output": {
                        "description": "Clean LLM-ready markdown with title, word count, and render method.",
                        "content_type": "application/json",
                    }
                }
            }
        ]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
