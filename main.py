"""
TerraDeed Labs - Web Scraping API
Dual Authentication: x402 USDC + API Keys
Version 0.7.10 - CDP Bazaar Integration with x402 Resource Server
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

# x402 imports
from x402.server import x402ResourceServerSync
from x402.http import HTTPFacilitatorClient, FacilitatorConfig
from x402.http.facilitator_client_base import AuthProvider, AuthHeaders
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    declare_discovery_extension,
    OutputConfig,
)

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
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")

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

# CDP Auth Provider
class CDPAuthProvider(AuthProvider):
    """Auth provider for CDP facilitator"""
    def __init__(self, api_key_id: str, api_key_secret: str):
        self.api_key_id = api_key_id
        self.api_key_secret = api_key_secret
    
    def get_auth_headers(self) -> AuthHeaders:
        return {
            "CDP-API-KEY-ID": self.api_key_id,
            "CDP-API-KEY-SECRET": self.api_key_secret,
        }

# x402 Facilitator Configuration
XPAY_FACILITATOR = "https://facilitator.xpay.sh"
CDP_FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402/facilitator"

# Create facilitator clients
facilitator_clients = []

# xpay.sh facilitator (always included)
facilitator_clients.append(HTTPFacilitatorClient(FacilitatorConfig(url=XPAY_FACILITATOR)))

# CDP facilitator (if credentials available)
if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    cdp_auth = CDPAuthProvider(CDP_API_KEY_ID, CDP_API_KEY_SECRET)
    facilitator_clients.append(HTTPFacilitatorClient(FacilitatorConfig(
        url=CDP_FACILITATOR,
        auth_provider=cdp_auth
    )))

# Create x402 resource server with all facilitators (sync version)
x402_server = x402ResourceServerSync(facilitator_clients=facilitator_clients)

# Initialize the server (required before verify_payment)
x402_server.initialize()

# Register bazaar extension for discovery
x402_server.register_extension(bazaar_resource_server_extension)

# Resource configurations for x402
SCRAPE_RESOURCE = {
    "url": f"{BASE_URL}/scrape",
    "description": "Scrape any public URL - clean LLM-ready markdown",
    "mimeType": "application/json",
}

EXTRACT_RESOURCE = {
    "url": f"{BASE_URL}/extract",
    "description": "Schema-driven structured JSON extraction",
    "mimeType": "application/json",
}

# Payment requirements
SCRAPE_REQUIREMENTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": USDC_BASE,
    "amount": "10000",  # $0.01 in atomic units
    "payTo": PAY_TO,
    "maxTimeoutSeconds": 300,
}

EXTRACT_REQUIREMENTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": USDC_BASE,
    "amount": "50000",  # $0.05 in atomic units
    "payTo": PAY_TO,
    "maxTimeoutSeconds": 300,
}

# Bazaar discovery extensions using declare_discovery_extension
SCRAPE_BAZAAR_EXT = declare_discovery_extension(
    input={"url": "https://example.com", "js_render": False},
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "URL to scrape"},
            "js_render": {"type": "boolean", "description": "Enable JavaScript rendering", "default": False}
        },
        "required": ["url"]
    },
    body_type="json",
    output=OutputConfig(
        example={
            "content": "## Example Domain\n\nThis domain is for use in illustrative examples.",
            "url": "https://example.com",
            "status": "success",
            "word_count": 28,
            "title": "Example Domain",
            "js_rendered": False,
            "auth_method": "x402"
        },
        schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Extracted markdown content"},
                "url": {"type": "string", "format": "uri"},
                "status": {"type": "string", "enum": ["success"]},
                "word_count": {"type": "integer"},
                "title": {"type": ["string", "null"]},
                "js_rendered": {"type": "boolean"},
                "auth_method": {"type": "string", "enum": ["x402", "api_key"]}
            },
            "required": ["content", "url", "status"]
        }
    )
)

EXTRACT_BAZAAR_EXT = declare_discovery_extension(
    input={"url": "https://example.com/product", "fields": ["name", "price"], "js_render": False},
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "URL to extract data from"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "List of field names to extract"},
            "js_render": {"type": "boolean", "description": "Enable JavaScript rendering", "default": False}
        },
        "required": ["url", "fields"]
    },
    body_type="json",
    output=OutputConfig(
        example={
            "url": "https://example.com/product",
            "status": "success",
            "data": {"name": "Example Product", "price": "$29.99"},
            "fields_requested": ["name", "price"],
            "fields_extracted": ["name", "price"],
            "js_rendered": False,
            "model": EXTRACT_MODEL,
            "auth_method": "x402"
        },
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "status": {"type": "string", "enum": ["success"]},
                "data": {"type": "object", "description": "Extracted fields"},
                "fields_requested": {"type": "array", "items": {"type": "string"}},
                "fields_extracted": {"type": "array", "items": {"type": "string"}},
                "js_rendered": {"type": "boolean"},
                "model": {"type": "string"},
                "auth_method": {"type": "string", "enum": ["x402", "api_key"]}
            },
            "required": ["url", "status", "data", "fields_requested", "fields_extracted"]
        }
    )
)

# FastAPI App
app = FastAPI(
    title="TerraDeed Scrape API",
    description="Pay-per-use web scraping via x402 USDC or API keys",
    version="0.7.14",
)

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
    if request.headers.get("payment-signature") or request.headers.get("PAYMENT-SIGNATURE"):
        return ("x402", None)
    return ("none", None)

# x402 Payment Required Response Helper
def payment_required_response(requirements: dict, resource: dict, bazaar_ext: dict):
    """Return x402 v2 compliant 402 response with PAYMENT-REQUIRED header and bazaar extension"""
    # Build accepts array with both facilitators
    accepts = []
    
    # xpay.sh entry
    accepts.append({
        "scheme": requirements["scheme"],
        "network": requirements["network"],
        "asset": requirements["asset"],
        "amount": requirements["amount"],
        "payTo": requirements["payTo"],
        "maxTimeoutSeconds": requirements["maxTimeoutSeconds"],
        "facilitator": XPAY_FACILITATOR,
    })
    
    # CDP entry (if credentials available)
    if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
        accepts.append({
            "scheme": requirements["scheme"],
            "network": requirements["network"],
            "asset": requirements["asset"],
            "amount": requirements["amount"],
            "payTo": requirements["payTo"],
            "maxTimeoutSeconds": requirements["maxTimeoutSeconds"],
            "facilitator": CDP_FACILITATOR,
        })
    
    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": resource,
        "accepts": accepts,
        "extensions": bazaar_ext
    }
    
    return JSONResponse(
        status_code=402,
        headers={"PAYMENT-REQUIRED": base64.b64encode(json.dumps(payload).encode()).decode()},
        content={"error": "Payment required"}
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
                    SCRAPE_REQUIREMENTS,
                    SCRAPE_RESOURCE,
                    SCRAPE_BAZAAR_EXT
                )
            else:
                return payment_required_response(
                    EXTRACT_REQUIREMENTS,
                    EXTRACT_RESOURCE,
                    EXTRACT_BAZAAR_EXT
                )
        
        # If x402 payment signature present, verify it using x402ResourceServer
        if auth_method == "x402":
            payment_sig = request.headers.get("payment-signature") or request.headers.get("PAYMENT-SIGNATURE")
            if payment_sig:
                try:
                    import base64
                    from x402 import parse_payment_payload, PaymentRequirements
                    
                    # Decode base64 header, then parse into PaymentPayload
                    decoded_bytes = base64.b64decode(payment_sig)
                    payload = parse_payment_payload(decoded_bytes)
                    
                    # Build proper PaymentRequirements
                    req_dict = SCRAPE_REQUIREMENTS if request.url.path == "/scrape" else EXTRACT_REQUIREMENTS
                    requirements = PaymentRequirements(
                        scheme=req_dict["scheme"],
                        network=req_dict["network"],
                        asset=req_dict["asset"],
                        amount=req_dict["amount"],
                        pay_to=req_dict["payTo"],
                        max_timeout_seconds=req_dict["maxTimeoutSeconds"],
                        extra={}
                    )
                    
                    # Verify payment (sync server - no await needed)
                    result = x402_server.verify_payment(payload, requirements)
                    
                    if not result or not getattr(result, 'is_valid', False):
                        return JSONResponse(
                            status_code=402,
                            content={"error": "Payment verification failed"}
                        )
                    
                    # Payment valid - add marker to request state
                    request.state.x402_payment_valid = True
                    request.state.x402_payment_sig = payment_sig
                except Exception as e:
                    print(f"Payment verification error: {e}")
                    return JSONResponse(
                        status_code=402,
                        content={"error": f"Payment verification error: {str(e)}"}
                    )
    
    return await call_next(request)

# Endpoints
@app.post("/scrape")
async def scrape(body: ScrapeRequest, request: Request):
    # Check for API key auth
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
    
    # Check for x402 payment (verified in middleware)
    if getattr(request.state, "x402_payment_valid", False):
        result = await _scrape(body.url, body.js_render)
        result["auth_method"] = "x402"
        
        # Settle payment after successful service delivery
        try:
            payment_sig = getattr(request.state, "x402_payment_sig", None)
            if payment_sig:
                await x402_server.settle_payment(
                    payment_signature=payment_sig,
                    requirements={**SCRAPE_REQUIREMENTS, **SCRAPE_RESOURCE}
                )
        except Exception as e:
            print(f"Payment settlement warning: {e}")
        
        return result
    
    # No valid auth
    return payment_required_response(SCRAPE_REQUIREMENTS, SCRAPE_RESOURCE, SCRAPE_BAZAAR_EXT)

@app.post("/extract")
async def extract(body: ExtractRequest, request: Request):
    # Check for API key auth
    auth_method, api_key = await get_auth_method(request)
    
    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info["error"])
        if key_info["credits_remaining"] < EXTRACT_CREDITS:
            raise HTTPException(status_code=402, detail={"error": "Insufficient credits", "credits_remaining": key_info["credits_remaining"], "credits_required": EXTRACT_CREDITS})
        
        markdown = (await _scrape(body.url, body.js_render))["content"]
        result = await _extract_structured(markdown, body.url, body.fields, body.js_render)
        deduct_credits(api_key, EXTRACT_CREDITS, "/extract")
        result["auth_method"] = "api_key"
        result["credits_remaining"] = key_info["credits_remaining"] - EXTRACT_CREDITS
        return result
    
    # Check for x402 payment (verified in middleware)
    if getattr(request.state, "x402_payment_valid", False):
        markdown = (await _scrape(body.url, body.js_render))["content"]
        result = await _extract_structured(markdown, body.url, body.fields, body.js_render)
        result["auth_method"] = "x402"
        
        # Settle payment after successful service delivery
        try:
            payment_sig = getattr(request.state, "x402_payment_sig", None)
            if payment_sig:
                await x402_server.settle_payment(
                    payment_signature=payment_sig,
                    requirements={**EXTRACT_REQUIREMENTS, **EXTRACT_RESOURCE}
                )
        except Exception as e:
            print(f"Payment settlement warning: {e}")
        
        return result
    
    # No valid auth
    return payment_required_response(EXTRACT_REQUIREMENTS, EXTRACT_RESOURCE, EXTRACT_BAZAAR_EXT)

@app.get("/health")
async def health():
    """Health check endpoint"""
    facilitators = [XPAY_FACILITATOR]
    if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
        facilitators.append(CDP_FACILITATOR)
    
    return {
        "status": "ok",
        "version": "0.7.14",
        "facilitators": facilitators,
        "auth_methods": ["x402", "api_key"]
    }

@app.get("/")
async def root():
    """Root endpoint - redirects to docs"""
    return {
        "service": "TerraDeed Scrape API",
        "version": "0.7.14",
        "documentation": "https://terradeed.co.uk/docs",
        "endpoints": {
            "scrape": {"path": "/scrape", "method": "POST", "price": SCRAPE_PRICE, "auth": ["x402", "api_key"]},
            "extract": {"path": "/extract", "method": "POST", "price": EXTRACT_PRICE, "auth": ["x402", "api_key"]},
            "health": {"path": "/health", "method": "GET"}
        }
    }

@app.post("/admin/keys")
async def create_key(request: CreateKeyRequest):
    """Create a new API key (admin only)"""
    if request.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    # Generate new key
    key = f"td_sk_{secrets.token_urlsafe(32)}"
    key_hash = hash_key(key)
    key_prefix = get_key_prefix(key)
    timestamp = datetime.now(timezone.utc).isoformat()
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, credits_remaining, created_at) VALUES (?, ?, ?, ?)",
            (key_hash, key_prefix, request.credits, timestamp)
        )
        conn.commit()
    
    return {"api_key": key, "credits": request.credits, "created_at": timestamp}

@app.get("/admin/keys/{key_prefix}")
async def get_key_status(key_prefix: str, request: Request):
    """Get API key status (admin only)"""
    admin_secret = request.headers.get("x-admin-secret", "")
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT key_prefix, credits_remaining, total_calls, created_at, last_used_at, is_active FROM api_keys WHERE key_prefix = ?",
            (key_prefix,)
        )
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")
        
        return {
            "key_prefix": row["key_prefix"],
            "credits_remaining": row["credits_remaining"],
            "total_calls": row["total_calls"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "is_active": bool(row["is_active"])
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
