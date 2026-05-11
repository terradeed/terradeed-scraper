"""
TerraDeed Labs — x402 Web Scraping API (Phase 2: real scraping + CDP mainnet)

Wallet:    0x4E024e356bd01853654b7B5196F2B85F67Cc39EC  (Base mainnet)
Price:     $0.01 USDC per call
Network:   Base mainnet (eip155:8453)
Facilitator: CDP (https://api.cdp.coinbase.com/platform/v2/x402)

Set these env vars in Railway:
    CDP_API_KEY_ID=your-key-id
    CDP_API_KEY_SECRET=your-secret
"""

import os
from typing import Any

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
FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"

CDP_API_KEY_ID     = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")

# ── CDP JWT Auth Provider ─────────────────────────────────────────────────────

def _build_cdp_auth_provider() -> CreateHeadersAuthProvider | None:
    """
    Build a CDP auth provider using the cdp-sdk JWT generator.
    Returns None if CDP keys are not set (falls back to unauthenticated,
    which works with the testnet facilitator but not CDP mainnet).
    """
    if not CDP_API_KEY_ID or not CDP_API_KEY_SECRET:
        return None

    try:
        from cdp.auth import GetAuthHeadersOptions, get_auth_headers

        CDP_HOST      = "api.cdp.coinbase.com"
        CDP_BASE_PATH = "/platform/v2/x402"

        def create_headers() -> dict[str, dict[str, str]]:
            """
            Called fresh on every request by CreateHeadersAuthProvider.
            Generates a new JWT for each endpoint path so tokens are never stale.
            """
            def _auth(method: str, path: str) -> dict[str, str]:
                opts = GetAuthHeadersOptions(
                    api_key_id=CDP_API_KEY_ID,
                    api_key_secret=CDP_API_KEY_SECRET,
                    request_method=method,
                    request_host=CDP_HOST,
                    request_path=path,
                )
                headers = get_auth_headers(opts)
                # Only pass Authorization — Content-Type is added by the client
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
    description="Pay-per-use web scraping. Returns clean LLM-ready markdown via x402 USDC micropayments.",
    version="0.3.0",
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
            f"Price: {PRICE} USDC per call on Base mainnet."
        ),
        extensions={
            "bazaar": {
                "discoverable": True,
                "category": "search",
                "tags": ["scraping", "web-data", "markdown", "llm", "ai-agent"],
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
    """Fetch URL and extract clean LLM-ready markdown using trafilatura."""
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
    Requires x402 payment ($0.01 USDC on Base mainnet).
    Returns clean LLM-ready markdown extracted from the target URL.
    """
    return _scrape(body.url)

# ── Free meta endpoints ───────────────────────────────────────────────────────

@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "TerraDeed Scrape API",
        "version": "0.3.0",
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
    cdp_configured = bool(CDP_API_KEY_ID and CDP_API_KEY_SECRET)
    return {
        "status": "ok",
        "cdp_auth": "configured" if cdp_configured else "missing",
        "network": NETWORK,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=4021)
