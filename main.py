"""
TerraDeed Labs — x402 Web Scraping API + API Key Tier

Wallet:    0x4E024e356bd01853654b7B5196F2B85F67Cc39EC  (Base mainnet)
Endpoints:
    POST /scrape   — $0.01 USDC or 1 credit — clean LLM-ready markdown from any URL
    POST /extract  — $0.05 USDC or 5 credits — schema-driven structured JSON extraction
Network:   Base mainnet (eip155:8453 internally / base for JS clients)
Facilitator: xpay (https://facilitator.xpay.sh)

Authentication Methods (parallel):
    1. x402: Payment-Signature header with USDC micropayment
    2. API Key: Authorization: Bearer <key> with prepaid credits

Set these env vars in Railway:
    ANTHROPIC_API_KEY=your-anthropic-key  (required for /extract)
    CDP_API_KEY_ID=your-key-id            (optional)
    CDP_API_KEY_SECRET=your-secret        (optional)
    ADMIN_SECRET=your-admin-secret        (required for /admin/keys endpoint)
    DATABASE_URL=sqlite:///./terradeed.db (optional, defaults to SQLite on volume)
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
from functools import wraps

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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
SCRAPE_CREDITS   = 1
EXTRACT_CREDITS  = 5

NETWORK_INTERNAL = "eip155:8453"
NETWORK_CLIENT   = "base"

FACILITATOR      = "https://facilitator.xpay.sh"
BASE_URL         = "https://api.terradeed.co.uk"
USDC_BASE        = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXTRACT_MODEL    = "claude-sonnet-4-20250514"

CDP_API_KEY_ID     = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_SECRET       = os.environ.get("ADMIN_SECRET", "terradeed-admin-2026")
DATABASE_URL       = os.environ.get("DATABASE_URL", "sqlite:///./terradeed.db")

# Parse SQLite path from DATABASE_URL
DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite://") else "./terradeed.db"

# ── Database Setup ────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Initialize the database with required tables."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                credits_remaining INTEGER NOT NULL DEFAULT 0,
                rate_limit_per_minute INTEGER DEFAULT 60,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                total_calls INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                metadata TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_prefix TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                credits_used INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                success BOOLEAN DEFAULT 1,
                error_message TEXT
            )
        """)
        conn.commit()
        print(f"✓ Database initialized at {DB_PATH}")

# Initialize DB on module load
init_db()

# ── API Key Management ────────────────────────────────────────────────────────

def generate_api_key() -> tuple[str, str]:
    """Generate a new API key. Returns (full_key, key_hash)."""
    random_part = secrets.token_urlsafe(32)
    full_key = f"td_sk_{random_part}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, key_hash

def hash_key(key: str) -> str:
    """Hash an API key for lookup."""
    return hashlib.sha256(key.encode()).hexdigest()

def get_key_prefix(key: str) -> str:
    """Get the first 12 chars of key for display/logging."""
    return key[:12] + "..." if len(key) > 12 else key

def create_api_key(credits: int = 100, rate_limit: int = 60) -> dict[str, Any]:
    """Create a new API key with initial credits."""
    full_key, key_hash = generate_api_key()
    key_prefix = get_key_prefix(full_key)
    created_at = datetime.now(timezone.utc).isoformat()
    
    with get_db() as conn:
        conn.execute(
            """INSERT INTO api_keys 
               (key_hash, key_prefix, credits_remaining, rate_limit_per_minute, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key_hash, key_prefix, credits, rate_limit, created_at, json.dumps({"source": "admin_api"}))
        )
        conn.commit()
    
    return {
        "api_key": full_key,
        "credits": credits,
        "rate_limit": rate_limit,
        "created_at": created_at,
        "message": "Store this key securely. It will not be shown again."
    }

def validate_api_key(key: str) -> Optional[dict[str, Any]]:
    """Validate an API key and return its details if valid."""
    key_hash = hash_key(key)
    
    with get_db() as conn:
        cursor = conn.execute(
            """SELECT key_prefix, credits_remaining, rate_limit_per_minute, is_active 
               FROM api_keys WHERE key_hash = ?""",
            (key_hash,)
        )
        row = cursor.fetchone()
        
        if not row:
            return None
        
        if not row["is_active"]:
            return {"error": "API key has been revoked"}
        
        if row["credits_remaining"] <= 0:
            return {"error": "Insufficient credits", "credits_remaining": 0}
        
        return {
            "key_prefix": row["key_prefix"],
            "credits_remaining": row["credits_remaining"],
            "rate_limit": row["rate_limit_per_minute"],
            "valid": True
        }

def deduct_credits(key: str, credits: int, endpoint: str, success: bool = True, error_message: str = None) -> bool:
    """Deduct credits from an API key and log usage."""
    key_hash = hash_key(key)
    timestamp = datetime.now(timezone.utc).isoformat()
    key_prefix = get_key_prefix(key)
    
    with get_db() as conn:
        # Deduct credits
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
        
        # Log usage
        conn.execute(
            """INSERT INTO usage_logs (key_prefix, endpoint, credits_used, timestamp, success, error_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key_prefix, endpoint, credits, timestamp, success, error_message)
        )
        conn.commit()
    
    return True

def get_key_stats(key_prefix: str = None) -> list[dict[str, Any]]:
    """Get usage stats for API keys."""
    with get_db() as conn:
        if key_prefix:
            cursor = conn.execute(
                """SELECT key_prefix, credits_remaining, total_calls, created_at, last_used_at, is_active
                   FROM api_keys WHERE key_prefix LIKE ?""",
                (f"%{key_prefix}%",)
            )
        else:
            cursor = conn.execute(
                """SELECT key_prefix, credits_remaining, total_calls, created_at, last_used_at, is_active
                   FROM api_keys ORDER BY created_at DESC LIMIT 100"""
            )
        return [dict(row) for row in cursor.fetchall()]

# ── Hardcoded test key for immediate testing ───────────────────────────────────

def ensure_test_key():
    """Ensure a test key exists for immediate testing."""
    test_key = "td_sk_test_terradeed_2026"
    key_hash = hash_key(test_key)
    
    with get_db() as conn:
        cursor = conn.execute("SELECT 1 FROM api_keys WHERE key_hash = ?", (key_hash,))
        if not cursor.fetchone():
            conn.execute(
                """INSERT INTO api_keys 
                   (key_hash, key_prefix, credits_remaining, rate_limit_per_minute, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key_hash, get_key_prefix(test_key), 1000, 60, 
                 datetime.now(timezone.utc).isoformat(), 
                 json.dumps({"source": "hardcoded_test_key"}))
            )
            conn.commit()
            print(f"✓ Test key created: {test_key[:20]}...")

ensure_test_key()

# ── x402 accepts arrays ───────────────────────────────────────────────────────

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

    def _should_intercept(self, method: str, path: str) -> bool:
        return f"{method} {path}" in self.route_accepts

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")

        if not self._should_intercept(method, path):
            await self.app(scope, receive, send)
            return

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
    description="Pay-per-use web scraping and structured data extraction via x402 USDC micropayments or API keys with prepaid credits.",
    version="0.7.0",
)

auth_provider = _build_cdp_auth_provider()
facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR, auth_provider=auth_provider))
server = x402ResourceServer(facilitator)
server.register(NETWORK_INTERNAL, ExactEvmServerScheme())

routes: dict[str, RouteConfig] = {
    "POST /scrape": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=PAY_TO, price=SCRAPE_PRICE, network=NETWORK_INTERNAL)],
        mime_type="application/json",
        description="Scrape any public URL — clean LLM-ready markdown. $0.01 USDC or 1 credit.",
    ),
    "POST /extract": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=PAY_TO, price=EXTRACT_PRICE, network=NETWORK_INTERNAL)],
        mime_type="application/json",
        description="Schema-driven structured JSON extraction. $0.05 USDC or 5 credits.",
    ),
}

# ── API Key Middleware (must be BEFORE x402 middleware) ────────────────────────

class APIKeyMiddleware:
    """ASGI middleware that checks for API keys and bypasses x402 if valid."""
    def __init__(self, app: Any) -> None:
        self.app = app
    
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Check if this is a protected route
        method = scope.get("method", "")
        path = scope.get("path", "")
        route_key = f"{method} {path}"
        
        if route_key not in routes:
            await self.app(scope, receive, send)
            return
        
        # Check for API key in headers
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        
        if auth_header.lower().startswith("bearer "):
            api_key = auth_header[7:].strip()
            key_info = validate_api_key(api_key)
            
            if key_info and key_info.get("valid"):
                # Valid API key - store in scope and bypass x402
                scope["api_key"] = api_key
                scope["api_key_valid"] = True
                scope["api_key_info"] = key_info
                await self.app(scope, receive, send)
                return
        
        # No valid API key - proceed to x402 middleware
        await self.app(scope, receive, send)

# NOTE: Order matters! API key check must happen BEFORE x402 middleware
# so that API key requests don't trigger x402 payment flow
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
app.add_middleware(X402ResponseBodyMiddleware, route_accepts=ROUTE_ACCEPTS)
app.add_middleware(NetworkNormalisationMiddleware)
app.add_middleware(APIKeyMiddleware)

# Security scheme for API key docs
security = HTTPBearer(auto_error=False)

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
    auth_method: str = "x402"  # "x402" or "api_key"

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
    auth_method: str = "x402"

class CreateKeyRequest(BaseModel):
    credits: int = 100
    rate_limit: int = 60
    admin_secret: str

class CreateKeyResponse(BaseModel):
    api_key: str
    credits: int
    rate_limit: int
    created_at: str
    message: str

class KeyInfoResponse(BaseModel):
    key_prefix: str
    credits_remaining: int
    rate_limit: int
    total_calls: int
    created_at: str
    last_used_at: Optional[str]
    is_active: bool

# ── Auth Helpers ──────────────────────────────────────────────────────────────

async def get_auth_method(request: Request) -> tuple[str, Optional[str]]:
    """
    Determine authentication method from request headers.
    Returns: (method, api_key_or_none)
    """
    # Check for API key first
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        api_key = auth_header[7:].strip()
        return ("api_key", api_key)
    
    # Check for x402 payment signature
    if request.headers.get("payment-signature"):
        return ("x402", None)
    
    return ("none", None)

def require_api_key(endpoint_credits: int, endpoint_name: str):
    """
    Decorator factory for endpoints that accept API key auth.
    Validates the key, checks credits, deducts on success.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request from args/kwargs
            request = kwargs.get('request') or (args[0] if args else None)
            if not request:
                raise HTTPException(status_code=500, detail="Request object not found")
            
            auth_method, api_key = await get_auth_method(request)
            
            if auth_method == "api_key" and api_key:
                # Validate the key
                key_info = validate_api_key(api_key)
                
                if not key_info:
                    raise HTTPException(status_code=401, detail="Invalid API key")
                
                if "error" in key_info:
                    raise HTTPException(status_code=403, detail=key_info["error"])
                
                # Check if enough credits
                if key_info["credits_remaining"] < endpoint_credits:
                    raise HTTPException(
                        status_code=402, 
                        detail={
                            "error": "Insufficient credits",
                            "credits_remaining": key_info["credits_remaining"],
                            "credits_required": endpoint_credits,
                            "top_up_url": "https://terradeed.co.uk/api-keys"
                        }
                    )
                
                # Store key info in request state for later deduction
                request.state.api_key = api_key
                request.state.credits_to_deduct = endpoint_credits
                request.state.endpoint_name = endpoint_name
                request.state.auth_method = "api_key"
                
            elif auth_method == "x402":
                request.state.auth_method = "x402"
            else:
                # No valid auth - let x402 middleware handle it (will return 402)
                request.state.auth_method = "none"
            
            return await func(*args, **kwargs)
        return wrapper
    return decorator

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
async def scrape(body: ScrapeRequest, request: Request) -> dict[str, Any]:
    auth_method, api_key = await get_auth_method(request)
    
    # Handle API key auth
    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info or "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info.get("error", "Invalid API key"))
        
        if key_info["credits_remaining"] < SCRAPE_CREDITS:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Insufficient credits",
                    "credits_remaining": key_info["credits_remaining"],
                    "credits_required": SCRAPE_CREDITS,
                    "top_up_url": "https://terradeed.co.uk/api-keys"
                }
            )
        
        # Perform scrape
        result = await _scrape(body.url, body.js_render)
        
        # Deduct credits on success
        deduct_credits(api_key, SCRAPE_CREDITS, "/scrape", success=True)
        result["auth_method"] = "api_key"
        result["credits_remaining"] = key_info["credits_remaining"] - SCRAPE_CREDITS
        return result
    
    # x402 auth - the middleware handles verification, we just do the work
    result = await _scrape(body.url, body.js_render)
    result["auth_method"] = "x402"
    return result


@app.post("/extract", response_model=ExtractResponse)
async def extract(body: ExtractRequest, request: Request) -> dict[str, Any]:
    if not body.fields:
        raise HTTPException(status_code=422, detail="At least one field must be specified.")
    if len(body.fields) > 20:
        raise HTTPException(status_code=422, detail="Maximum 20 fields per request.")
    
    auth_method, api_key = await get_auth_method(request)
    
    # Handle API key auth
    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info or "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info.get("error", "Invalid API key"))
        
        if key_info["credits_remaining"] < EXTRACT_CREDITS:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Insufficient credits",
                    "credits_remaining": key_info["credits_remaining"],
                    "credits_required": EXTRACT_CREDITS,
                    "top_up_url": "https://terradeed.co.uk/api-keys"
                }
            )
        
        # Perform extract
        scrape_result = await _scrape(body.url, body.js_render)
        result = await _extract_structured(scrape_result["content"], body.url, body.fields, scrape_result["js_rendered"])
        
        # Deduct credits on success
        deduct_credits(api_key, EXTRACT_CREDITS, "/extract", success=True)
        result["auth_method"] = "api_key"
        result["credits_remaining"] = key_info["credits_remaining"] - EXTRACT_CREDITS
        return result
    
    # x402 auth
    scrape_result = await _scrape(body.url, body.js_render)
    result = await _extract_structured(scrape_result["content"], body.url, body.fields, scrape_result["js_rendered"])
    result["auth_method"] = "x402"
    return result


@app.get("/")
async def root():
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=402,
        content={
            "x402Version": 2,
            "name": "TerraDeed Scrape API",
            "description": "Pay-per-use web scraping and structured data extraction via x402 USDC micropayments or API keys with prepaid credits.",
            "version": "0.7.0",
            "authentication": {
                "x402": {"header": "Payment-Signature", "currency": "USDC", "network": NETWORK_CLIENT},
                "api_key": {"header": "Authorization: Bearer <key>", "credit_pricing": {"scrape": 1, "extract": 5}}
            },
            "endpoints": {
                "POST /scrape":  {"price_usdc": SCRAPE_PRICE, "credits": SCRAPE_CREDITS, "description": "Clean LLM-ready markdown from any URL"},
                "POST /extract": {"price_usdc": EXTRACT_PRICE, "credits": EXTRACT_CREDITS, "description": "Schema-driven structured JSON extraction"},
            },
            "payment": {"protocol": "x402", "network": NETWORK_CLIENT, "facilitator": FACILITATOR},
            "docs": f"{BASE_URL}/docs",
            "well_known": f"{BASE_URL}/.well-known/x402",
            "extensions": {"bazaar": {"name": "TerraDeed Scrape API", "description": "Pay-per-use web scraping and structured data extraction via x402 USDC micropayments on Base."}},
            "accepts": SCRAPE_ACCEPTS + EXTRACT_ACCEPTS,
        },
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    # Get some basic stats
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) as count FROM api_keys WHERE is_active = 1")
        active_keys = cursor.fetchone()["count"]
        cursor = conn.execute("SELECT COUNT(*) as count FROM api_keys")
        total_keys = cursor.fetchone()["count"]
        cursor = conn.execute("SELECT SUM(total_calls) as total FROM api_keys")
        total_calls = cursor.fetchone()["total"] or 0
    
    return {
        "status": "ok", 
        "version": "0.7.0",
        "cdp_auth": "configured" if CDP_API_KEY_ID else "missing", 
        "anthropic": "configured" if ANTHROPIC_API_KEY else "missing", 
        "network": NETWORK_CLIENT, 
        "capabilities": "static+js-rendering+structured-extraction",
        "auth_methods": ["x402", "api_key"],
        "api_keys": {"active": active_keys, "total": total_keys, "total_calls": total_calls}
    }


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
        "name": "TerraDeed Scrape API",
        "description": "Pay-per-use web scraping and structured data extraction via x402 USDC micropayments on Base.",
        "resources": [
            {"url": f"{BASE_URL}/scrape", "method": "POST", "description": "LLM-ready markdown from any URL.", "accepts": SCRAPE_ACCEPTS, "info": {"name": "TerraDeed Web Scraper", "category": "search", "tags": ["scraping", "web-data", "markdown", "llm"]}},
            {"url": f"{BASE_URL}/extract", "method": "POST", "description": "Schema-driven structured JSON extraction.", "accepts": EXTRACT_ACCEPTS, "info": {"name": "TerraDeed Structured Extractor", "category": "search", "tags": ["extraction", "structured-data", "json", "llm"]}},
        ],
    }


# ── Admin Endpoints ───────────────────────────────────────────────────────────

@app.post("/admin/keys", response_model=CreateKeyResponse)
async def create_key(request: CreateKeyRequest):
    """Create a new API key (admin only)."""
    if request.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    result = create_api_key(credits=request.credits, rate_limit=request.rate_limit)
    return CreateKeyResponse(**result)


@app.get("/admin/keys")
async def list_keys(admin_secret: str):
    """List all API keys (admin only)."""
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    stats = get_key_stats()
    return {"keys": stats, "count": len(stats)}


@app.get("/admin/keys/{key_prefix}")
async def get_key_info(key_prefix: str, admin_secret: str):
    """Get info about a specific API key (admin only)."""
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    stats = get_key_stats(key_prefix)
    if not stats:
        raise HTTPException(status_code=404, detail="Key not found")
    return stats[0]


@app.post("/admin/keys/{key_prefix}/revoke")
async def revoke_key(key_prefix: str, admin_secret: str):
    """Revoke an API key (admin only)."""
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_prefix LIKE ?",
            (f"%{key_prefix}%",)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Key not found")
    
    return {"message": "Key revoked successfully", "key_prefix": key_prefix}


@app.post("/admin/keys/{key_prefix}/add-credits")
async def add_credits(key_prefix: str, credits: int, admin_secret: str):
    """Add credits to an API key (admin only)."""
    if admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE api_keys SET credits_remaining = credits_remaining + ? WHERE key_prefix LIKE ?",
            (credits, f"%{key_prefix}%")
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Key not found")
        
        cursor = conn.execute(
            "SELECT credits_remaining FROM api_keys WHERE key_prefix LIKE ?",
            (f"%{key_prefix}%",)
        )
        new_balance = cursor.fetchone()["credits_remaining"]
    
    return {"message": "Credits added", "key_prefix": key_prefix, "credits_added": credits, "new_balance": new_balance}


# ── Test Endpoint ─────────────────────────────────────────────────────────────

@app.get("/test-key")
async def test_key(api_key: str):
    """Test an API key and see its info."""
    info = validate_api_key(api_key)
    if not info:
        return {"valid": False, "error": "Invalid key"}
    if "error" in info:
        return {"valid": False, "error": info["error"]}
    return {"valid": True, **info}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
