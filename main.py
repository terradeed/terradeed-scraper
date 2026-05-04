"""
TerraDeed Labs — x402 Web Scraping API (Phase 2: real scraping)

Wallet:    0x4E024e356bd01853654b7B5196F2B85F67Cc39EC  (Base Maine)
Price:     $0.01 USDC per call
Network:   Base Sepolia (eip155:8453)
Testnet facilitator: https://api.cdp.coinbase.com/platform/v2/x402
"""

from typing import Any

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

# ── Config ────────────────────────────────────────────────────────────────────

PAY_TO      = "0x4E024e356bd01853654b7B5196F2B85F67Cc39EC"
PRICE       = "$0.005"
NETWORK     = "eip155:84532"
FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="TerraDeed Scrape API",
    description="Pay-per-use web scraping. Returns clean LLM-ready markdown via x402 USDC micropayments.",
    version="0.2.0",
)

# ── x402 payment infrastructure ───────────────────────────────────────────────

import os
facilitator = HTTPFacilitatorClient(FacilitatorConfig(
    url=FACILITATOR,
    api_key_id=os.environ.get("CDP_API_KEY_ID"),
    api_key_secret=os.environ.get("CDP_API_KEY_SECRET"),
))
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
            f"Price: {PRICE} USDC per call on Base Sepolia."
        ),
        extensions={
            "bazaar": {
                "discoverable": True,
                "category": "search",
                "tags": ["scraping", "web-data", "markdown"],
            }
        },
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)

# ── Request / Response models ─────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str

class ScrapeResponse(BaseModel):
    content: str
    url: str
    status: str
    word_count: int
    title: str | None = None

# ── Real scraping logic ───────────────────────────────────────────────────────

def _scrape(url: str) -> dict[str, Any]:
    """
    Fetches the URL and extracts clean LLM-ready markdown using trafilatura.
    Strips boilerplate, ads, navigation, and returns only meaningful content.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        response.raise_for_status()

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Timeout fetching {url}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch {url}: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch {url}: {str(e)}")

    content = trafilatura.extract(
        response.text,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_tables=True,
        no_fallback=False,
    )

    meta = trafilatura.extract_metadata(response.text)
    title = meta.title if meta else None

    if not content:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract meaningful content from {url}. "
                   "The page may require JavaScript rendering."
        )

    return {
        "content": content,
        "url": url,
        "status": "success",
        "word_count": len(content.split()),
        "title": title,
    }

# ── Protected endpoint ────────────────────────────────────────────────────────

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(body: ScrapeRequest) -> dict[str, Any]:
    """
    Requires x402 payment ($0.005 USDC on Base Sepolia).
    Returns clean LLM-ready markdown extracted from the target URL.
    """
    return _scrape(body.url)

# ── Free meta endpoints ───────────────────────────────────────────────────────

@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "TerraDeed Scrape API",
        "version": "0.2.0",
        "endpoints": {
            "POST /scrape": {
                "protected": True,
                "price": PRICE,
                "network": NETWORK,
                "body": {"url": "string"},
            }
        },
        "payment": {"protocol": "x402", "facilitator": FACILITATOR},
        "docs": "/docs",
    }

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=4021)
