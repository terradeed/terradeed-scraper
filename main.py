"""
TerraDeed Labs - Web Scraping API
Dual Authentication: x402 USDC + API Keys
"""

import base64
import json
import os
import sqlite3
import secrets
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional
from contextlib import contextmanager

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Config
PAY_TO = "0x4E024e356bd01853654b7B5196F2B85F67Cc39EC"
SCRAPE_PRICE = "$0.01"
EXTRACT_PRICE = "$0.05"
SCRAPE_CREDITS = 1
EXTRACT_CREDITS = 5
NETWORK_CLIENT = "base"
BASE_URL = "https://api.terradeed.co.uk"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXTRACT_MODEL = "claude-sonnet-4-20250514"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "terradeed-admin-2026")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./terradeed.db")

DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite://") else "./terradeed.db"

# Database
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                credits_remaining INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                total_calls INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        conn.commit()

init_db()

# API Key Functions
def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def get_key_prefix(key: str) -> str:
    return key[:12] + "..." if len(key) > 12 else key

def validate_api_key(key: str) -> Optional[dict]:
    key_hash = hash_key(key)
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT key_prefix, credits_remaining, is_active FROM api_keys WHERE key_hash = ?",
            (key_hash,)
        )
        row = cursor.fetchone()
        
        if not row:
            return None
        if not row["is_active"]:
            return {"error": "API key revoked"}
        if row["credits_remaining"] <= 0:
            return {"error": "Insufficient credits"}
        
        return {
            "key_prefix": row["key_prefix"],
            "credits_remaining": row["credits_remaining"],
            "valid": True
        }

def deduct_credits(key: str, credits: int, endpoint: str) -> bool:
    key_hash = hash_key(key)
    timestamp = datetime.now(timezone.utc).isoformat()
    key_prefix = get_key_prefix(key)
    
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE api_keys 
               SET credits_remaining = credits_remaining - ?,
                   total_calls = total_calls + 1,
                   last_used_at = ?
               WHERE key_hash = ? AND credits_remaining >= ?""",
            (credits, timestamp, key_hash, credits)
        )
        if cursor.rowcount == 0:
            return False
        conn.commit()
    return True

def ensure_test_key():
    test_key = "td_sk_test_terradeed_2026"
    key_hash = hash_key(test_key)
    with get_db() as conn:
        cursor = conn.execute("SELECT 1 FROM api_keys WHERE key_hash = ?", (key_hash,))
        if not cursor.fetchone():
            conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at) VALUES (?, ?, ?, ?)",
                (key_hash, get_key_prefix(test_key), 1000, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()

ensure_test_key()

# x402 Config
USDC_EXTRA = {"name": "USD Coin", "version": "2"}

# CDP Facilitator Config (for Bazaar surfacing)
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402/facilitator"
XPAY_FACILITATOR_URL = "https://facilitator.xpay.sh"

# Facilitator endpoints for middleware
FACILITATOR_URLS = [XPAY_FACILITATOR_URL]
if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    FACILITATOR_URLS.append(CDP_FACILITATOR_URL)

# x402 accepts array - dual facilitator (xpay.sh primary, CDP for Bazaar)
SCRAPE_ACCEPTS = [
    {
        "scheme": "exact",
        "network": NETWORK_CLIENT,
        "asset": USDC_BASE,
        "amount": "10000",
        "payTo": PAY_TO,
        "resource": f"{BASE_URL}/scrape",
        "description": "Scrape any public URL - clean LLM-ready markdown",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": USDC_EXTRA,
        "facilitator": XPAY_FACILITATOR_URL,
    },
    {
        "scheme": "exact",
        "network": NETWORK_CLIENT,
        "asset": USDC_BASE,
        "amount": "10000",
        "payTo": PAY_TO,
        "resource": f"{BASE_URL}/scrape",
        "description": "Scrape any public URL - clean LLM-ready markdown (CDP)",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": USDC_EXTRA,
        "facilitator": CDP_FACILITATOR_URL,
    }
]

EXTRACT_ACCEPTS = [
    {
        "scheme": "exact",
        "network": NETWORK_CLIENT,
        "asset": USDC_BASE,
        "amount": "50000",
        "payTo": PAY_TO,
        "resource": f"{BASE_URL}/extract",
        "description": "Schema-driven structured JSON extraction",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": USDC_EXTRA,
        "facilitator": XPAY_FACILITATOR_URL,
    },
    {
        "scheme": "exact",
        "network": NETWORK_CLIENT,
        "asset": USDC_BASE,
        "amount": "50000",
        "payTo": PAY_TO,
        "resource": f"{BASE_URL}/extract",
        "description": "Schema-driven structured JSON extraction (CDP)",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": USDC_EXTRA,
        "facilitator": CDP_FACILITATOR_URL,
    }
]

# x402 Payment Required Response Helper
def payment_required_response(accepts: list, resource_url: str, resource_description: str, resource_mime_type: str, discovery_extension: dict) -> JSONResponse:
    """Return x402 v2 compliant 402 response with PAYMENT-REQUIRED header and resource object"""
    payload = {
        "x402Version": 2,
        "resource": {
            "url": resource_url,
            "description": resource_description,
            "mimeType": resource_mime_type
        },
        "accepts": accepts,
        "extensions": {
            "bazaar": discovery_extension
        }
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    return JSONResponse(
        status_code=402,
        headers={"PAYMENT-REQUIRED": payload_b64},
        content={"error": "Payment required"}
    )

def declare_discovery_extension_scrape():
    """Return bazaar discovery extension declaration for /scrape endpoint - CDP v2 compliant"""
    return {
        "info": {
            "title": "TerraDeed Scrape API - Scrape",
            "description": "Pay-per-use web scraping via x402 USDC or API keys. Returns clean LLM-ready markdown.",
            "version": "0.7.8",
            "contact": {
                "name": "TerraDeed Labs",
                "url": "https://terradeed.co.uk",
                "email": "contact@terradeed.co.uk"
            },
            "input": {
                "type": "http",
                "method": "POST",
                "description": "URL to scrape with optional JavaScript rendering",
                "example": {
                    "url": "https://example.com",
                    "js_render": False
                }
            },
            "output": {
                "type": "json",
                "description": "Scraped content in markdown format with metadata",
                "example": {
                    "content": "## Example Domain\n\nThis domain is for use in illustrative examples in documents.",
                    "url": "https://example.com",
                    "status": "success",
                    "word_count": 28,
                    "title": "Example Domain",
                    "js_rendered": False
                }
            }
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri", "description": "URL to scrape"},
                        "js_render": {"type": "boolean", "description": "Enable JavaScript rendering", "default": False}
                    },
                    "required": ["url"]
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Extracted markdown content"},
                        "url": {"type": "string", "format": "uri"},
                        "status": {"type": "string", "enum": ["success"]},
                        "word_count": {"type": "integer"},
                        "title": {"type": ["string", "null"]},
                        "js_rendered": {"type": "boolean"}
                    },
                    "required": ["content", "url", "status"]
                }
            },
            "required": ["input"]
        }
    }


def declare_discovery_extension_extract():
    """Return bazaar discovery extension declaration for /extract endpoint - CDP v2 compliant"""
    return {
        "info": {
            "title": "TerraDeed Scrape API - Extract",
            "description": "Schema-driven structured JSON extraction via x402 USDC or API keys. Extract specific fields from any URL.",
            "version": "0.7.8",
            "contact": {
                "name": "TerraDeed Labs",
                "url": "https://terradeed.co.uk",
                "email": "contact@terradeed.co.uk"
            },
            "input": {
                "type": "http",
                "method": "POST",
                "description": "URL to extract data from with list of fields to extract",
                "example": {
                    "url": "https://example.com/product",
                    "fields": ["product_name", "price", "description"],
                    "js_render": False
                }
            },
            "output": {
                "type": "json",
                "description": "Structured JSON extraction with metadata",
                "example": {
                    "url": "https://example.com/product",
                    "status": "success",
                    "data": {
                        "product_name": "Example Widget",
                        "price": "$29.99",
                        "description": "A high-quality example product for demonstration purposes."
                    },
                    "fields_requested": ["product_name", "price", "description"],
                    "fields_extracted": ["product_name", "price", "description"],
                    "js_rendered": False
                }
            }
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri", "description": "URL to extract data from"},
                        "fields": {"type": "array", "items": {"type": "string"}, "description": "List of field names to extract"},
                        "js_render": {"type": "boolean", "description": "Enable JavaScript rendering", "default": False}
                    },
                    "required": ["url", "fields"]
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri"},
                        "status": {"type": "string", "enum": ["success"]},
                        "data": {"type": "object", "description": "Extracted fields"},
                        "fields_requested": {"type": "array", "items": {"type": "string"}},
                        "fields_extracted": {"type": "array", "items": {"type": "string"}},
                        "js_rendered": {"type": "boolean"}
                    },
                    "required": ["url", "status", "data", "fields_requested", "fields_extracted"]
                }
            },
            "required": ["input"]
        }
    }

# FastAPI App
app = FastAPI(
    title="TerraDeed Scrape API",
    description="Pay-per-use web scraping via x402 USDC or API keys",
    version="0.7.8",
)

# Middleware: x402 auth check BEFORE Pydantic validation
@app.middleware("http")
async def x402_auth_middleware(request: Request, call_next):
    """
    Intercept POST /scrape and POST /extract to check auth before Pydantic validation.
    Returns 402 immediately if no valid auth present, avoiding 422 validation errors.
    """
    if request.method == "POST" and request.url.path in ["/scrape", "/extract"]:
        auth_method, api_key = await get_auth_method(request)
        
        # If no auth provided, return 402 before validation runs
        if auth_method == "none":
            if request.url.path == "/scrape":
                return payment_required_response(
                    SCRAPE_ACCEPTS,
                    f"{BASE_URL}/scrape",
                    "Scrape any public URL - clean LLM-ready markdown",
                    "application/json",
                    declare_discovery_extension_scrape()
                )
            else:
                return payment_required_response(
                    EXTRACT_ACCEPTS,
                    f"{BASE_URL}/extract",
                    "Schema-driven structured JSON extraction",
                    "application/json",
                    declare_discovery_extension_extract()
                )
    
    return await call_next(request)

# Models
class ScrapeRequest(BaseModel):
    url: str
    js_render: bool = False

class ExtractRequest(BaseModel):
    url: str
    fields: list[str]
    js_render: bool = False

class CreateKeyRequest(BaseModel):
    credits: int = 100
    rate_limit: int = 60
    admin_secret: str

# Scraping Functions
def _fetch_static(url: str) -> tuple[str, None]:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        response.raise_for_status()
        return response.text, None
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Timeout fetching {url}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch {url}: {str(e)}")

async def _fetch_with_playwright(url: str) -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        html = await page.content()
        await browser.close()
        return html

def _extract_content(html: str) -> tuple[str, str]:
    content = trafilatura.extract(html, output_format="markdown", include_links=False, include_images=False, include_tables=True)
    meta = trafilatura.extract_metadata(html)
    return content, (meta.title if meta else None)

async def _scrape(url: str, js_render: bool = False):
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
        except:
            pass

    if not content:
        raise HTTPException(status_code=422, detail=f"Could not extract content from {url}")

    return {"content": content, "url": url, "status": "success", "word_count": len(content.split()), "title": title, "js_rendered": js_rendered}

async def _extract_structured(markdown: str, url: str, fields: list[str], js_rendered: bool):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY missing")

    fields_str = ", ".join(f'"{f}"' for f in fields)
    prompt = f"""Extract these fields: [{fields_str}]

Return ONLY valid JSON. Set missing fields to null.

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
        raise HTTPException(status_code=502, detail=f"Anthropic error: {response.status_code} - {response.text}")

    try:
        data = json.loads(response.json()["content"][0]["text"].strip())
    except:
        raise HTTPException(status_code=502, detail="Malformed JSON from model")

    return {"url": url, "status": "success", "data": data, "fields_requested": fields, "fields_extracted": [k for k, v in data.items() if v is not None], "js_rendered": js_rendered, "model": EXTRACT_MODEL}

# Auth Helper
async def get_auth_method(request: Request) -> tuple[str, Optional[str]]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return ("api_key", auth_header[7:].strip())
    if request.headers.get("payment-signature"):
        return ("x402", None)
    return ("none", None)

# Endpoints
@app.post("/scrape")
async def scrape(body: ScrapeRequest, request: Request):
    auth_method, api_key = await get_auth_method(request)
    
    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info["error"])
        
        if key_info["credits_remaining"] < SCRAPE_CREDITS:
            raise HTTPException(status_code=402, detail={"error": "Insufficient credits", "credits_remaining": key_info["credits_remaining"], "credits_required": SCRAPE_CREDITS})
        
        result = await _scrape(body.url, body.js_render)
        deduct_credits(api_key, SCRAPE_CREDITS, "/scrape")
        result["auth_method"] = "api_key"
        result["credits_remaining"] = key_info["credits_remaining"] - SCRAPE_CREDITS
        return result
    
    return payment_required_response(
        SCRAPE_ACCEPTS,
        f"{BASE_URL}/scrape",
        "Scrape any public URL - clean LLM-ready markdown",
        "application/json",
        declare_discovery_extension_scrape()
    )

@app.post("/extract")
async def extract(body: ExtractRequest, request: Request):
    if not body.fields:
        raise HTTPException(status_code=422, detail="At least one field required")
    if len(body.fields) > 20:
        raise HTTPException(status_code=422, detail="Maximum 20 fields")
    
    auth_method, api_key = await get_auth_method(request)
    
    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info["error"])
        
        if key_info["credits_remaining"] < EXTRACT_CREDITS:
            raise HTTPException(status_code=402, detail={"error": "Insufficient credits", "credits_remaining": key_info["credits_remaining"], "credits_required": EXTRACT_CREDITS})
        
        scrape_result = await _scrape(body.url, body.js_render)
        result = await _extract_structured(scrape_result["content"], body.url, body.fields, scrape_result["js_rendered"])
        deduct_credits(api_key, EXTRACT_CREDITS, "/extract")
        result["auth_method"] = "api_key"
        result["credits_remaining"] = key_info["credits_remaining"] - EXTRACT_CREDITS
        return result
    
    return payment_required_response(
        EXTRACT_ACCEPTS,
        f"{BASE_URL}/extract",
        "Schema-driven structured JSON extraction",
        "application/json",
        declare_discovery_extension_extract()
    )

@app.get("/")
async def root():
    return JSONResponse(
        status_code=402,
        content={
            "name": "TerraDeed Scrape API",
            "version": "0.7.6",
            "authentication": {
                "x402": {"header": "Payment-Signature", "currency": "USDC"},
                "api_key": {"header": "Authorization: Bearer <key>", "credits": {"scrape": 1, "extract": 5}},
            },
            "endpoints": {
                "POST /scrape": {"price_usdc": SCRAPE_PRICE, "credits": SCRAPE_CREDITS},
                "POST /extract": {"price_usdc": EXTRACT_PRICE, "credits": EXTRACT_CREDITS},
            },
            "accepts": SCRAPE_ACCEPTS + EXTRACT_ACCEPTS,
        },
    )

@app.get("/health")
async def health():
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) as count FROM api_keys WHERE is_active = 1")
        active_keys = cursor.fetchone()["count"]
        cursor = conn.execute("SELECT SUM(total_calls) as total FROM api_keys")
        total_calls = cursor.fetchone()["total"] or 0
    
    return {
        "status": "ok",
        "version": "0.7.6",
        "anthropic": "configured" if ANTHROPIC_API_KEY else "missing",
        "auth_methods": ["x402", "api_key"],
        "api_keys": {"active": active_keys, "total_calls": total_calls}
    }

@app.get("/.well-known/x402")
async def well_known_x402():
    return {
        "version": 2,
        "name": "TerraDeed Scrape API",
        "resources": [
            {"url": f"{BASE_URL}/scrape", "method": "POST", "accepts": SCRAPE_ACCEPTS},
            {"url": f"{BASE_URL}/extract", "method": "POST", "accepts": EXTRACT_ACCEPTS},
        ],
    }

@app.post("/admin/keys")
async def create_key(request: CreateKeyRequest):
    if request.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    full_key = f"td_sk_{secrets.token_urlsafe(32)}"
    key_hash = hash_key(full_key)
    key_prefix = get_key_prefix(full_key)
    created_at = datetime.now(timezone.utc).isoformat()
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at) VALUES (?, ?, ?, ?)",
            (key_hash, key_prefix, request.credits, created_at)
        )
        conn.commit()
    
    return {"api_key": full_key, "credits": request.credits, "created_at": created_at, "message": "Store securely - will not be shown again"}

@app.get("/admin/keys")
async def list_keys(admin_secret: str):
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    with get_db() as conn:
        cursor = conn.execute("SELECT key_prefix, credits_remaining, total_calls, created_at, is_active FROM api_keys ORDER BY created_at DESC")
        keys = [dict(row) for row in cursor.fetchall()]
    
    return {"keys": keys, "count": len(keys)}

@app.get("/test-key")
async def test_key(api_key: str):
    info = validate_api_key(api_key)
    if not info:
        return {"valid": False, "error": "Invalid key"}
    if "error" in info:
        return {"valid": False, "error": info["error"]}
    return {"valid": True, **info}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
