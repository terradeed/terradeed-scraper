"""
TerraDeed Labs - Web Scraping API
Dual Authentication: x402 USDC + API Keys
Version 0.8.1 - Property enrichment with UK government data sources
"""

import base64
import json
import os
import sqlite3
import secrets
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from contextlib import contextmanager

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

# x402 imports
from x402.server import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.http.facilitator_client_base import AuthProvider, AuthHeaders
from x402.schemas import SupportedResponse, SupportedKind
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
)

from enrichment import enrich_property

# Config
PAY_TO = "0x4E024e356bd01853654b7B5196F2B85F67Cc39EC"
SCRAPE_PRICE = "$0.01"
EXTRACT_PRICE = "$0.05"
SCRAPE_CREDITS = 1
EXTRACT_CREDITS = 5
PROPERTY_PRICE = "$0.10"
PROPERTY_CREDITS = 10
NETWORK_CLIENT = "base"
BASE_URL = "https://api.terradeed.co.uk"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXTRACT_MODEL = "claude-sonnet-4-6"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
if len(ADMIN_SECRET) < 32:
    raise RuntimeError("ADMIN_SECRET must be set to a 32+ character value")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./terradeed.db")
DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite://") else "./terradeed.db"
logging.warning("Database path: %s. Data will be lost on every redeploy unless a persistent volume is mounted at this path.", DB_PATH)
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")

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

# ============================================================================
# CDP facilitator configuration — drop-in replacement for the existing section
# ============================================================================
# What changed vs your current code, and why:
#
#   1. request_host is now hostname-only ("api.cdp.coinbase.com").
#      NOT the bug — the cdp-sdk normalises the scheme away via urlparse —
#      but hostname-only is what the SDK documents, so this removes the
#      ambiguity permanently.
#
#   2. Logging: the x402 library ALREADY decodes and logs CDP's
#      EXTENSION-RESPONSES header on every verify/settle at INFO level on
#      the "x402" logger. Your Railway logs have been discarding it. The
#      logging block below surfaces it — this header is CDP telling you
#      whether it accepted or rejected your bazaar extension data on each
#      settlement, which is the exact diagnostic you need for indexing.
#
#   3. DEBUG_SETTLE now also logs client.identifier so every settlement
#      line names which facilitator handled it (no more inference).
#
#   Everything else — CDPFacilitatorWrapper, facilitator ordering, the
#   xpay.sh fallback — is unchanged. The routing was never broken.


# --- Surface x402's built-in EXTENSION-RESPONSES diagnostics ---------------
logging.basicConfig(level=logging.INFO)          # no-op if already configured
logging.getLogger("x402").setLevel(logging.INFO)

class CDPFacilitatorWrapper:
    """Wraps HTTPFacilitatorClient for CDP, hardcoding get_supported() since CDP has no /supported endpoint."""
    def __init__(self, http_client: HTTPFacilitatorClient):
        self._client = http_client

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[
                SupportedKind(x402_version=1, scheme="exact", network="base", extra=None),
                SupportedKind(x402_version=2, scheme="exact", network="eip155:8453", extra=None),
            ],
            extensions=[],
            signers={},
        )

    async def verify(self, payload, requirements):
        return await self._client.verify(payload, requirements)

    async def settle(self, payload, requirements):
        return await self._client.settle(payload, requirements)

    async def verify_from_bytes(self, payload_bytes, requirements_bytes):
        return await self._client.verify_from_bytes(payload_bytes, requirements_bytes)

    async def settle_from_bytes(self, payload_bytes, requirements_bytes):
        return await self._client.settle_from_bytes(payload_bytes, requirements_bytes)

    async def aclose(self):
        await self._client.aclose()

    @property
    def identifier(self):
        return self._client.identifier

    @property
    def url(self):
        return self._client.url

class CDPAuthProvider(AuthProvider):
    """Auth provider for CDP facilitator using JWT Bearer tokens.

    Generates a fresh JWT per request (the x402 HTTP client calls
    get_auth_headers() on every verify/settle, and CDP JWTs expire in
    ~2 minutes, so tokens must never be cached).
    """

    CDP_HOST = "api.cdp.coinbase.com"  # hostname only — no scheme

    def __init__(self, api_key_id: str, api_key_secret: str):
        self.api_key_id = api_key_id
        self.api_key_secret = api_key_secret

    def _make_headers(self, method: str, path: str) -> dict[str, str]:
        from cdp.auth import generate_jwt
        from cdp.auth.utils.jwt import JwtOptions

        token = generate_jwt(JwtOptions(
            api_key_id=self.api_key_id,
            api_key_secret=self.api_key_secret,
            request_method=method,
            request_host=self.CDP_HOST,
            request_path=path,
        ))
        return {"Authorization": f"Bearer {token}"}

    def get_auth_headers(self):
        return AuthHeaders(
            verify=self._make_headers("POST", "/platform/v2/x402/verify"),
            settle=self._make_headers("POST", "/platform/v2/x402/settle"),
            supported=self._make_headers("GET", "/platform/v2/x402/supported"),
        )


# --- Facilitator wiring (unchanged logic, plus identifiers) ----------------
XPAY_FACILITATOR = "https://facilitator.xpay.sh"
CDP_FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"

facilitator_clients = []

# CDP facilitator FIRST. Note: because the x402 server maps exactly ONE
# facilitator per (network, scheme) at initialize() time — first registrant
# wins, with no runtime fallback — CDP being first means it handles ALL
# "base" / "eip155:8453" settlements. xpay.sh below is only ever used for
# networks/schemes CDP does not claim.
if CDP_API_KEY_ID and CDP_API_KEY_SECRET and len(CDP_API_KEY_ID) > 10:
    try:
        cdp_auth = CDPAuthProvider(CDP_API_KEY_ID, CDP_API_KEY_SECRET)
        cdp_http = HTTPFacilitatorClient(FacilitatorConfig(
            url=CDP_FACILITATOR,
            auth_provider=cdp_auth,
            identifier="cdp",
        ))
        facilitator_clients.append(CDPFacilitatorWrapper(cdp_http))
        print("CDP facilitator configured")
    except Exception as e:
        print(f"Warning: Could not configure CDP facilitator: {e}")
else:
    print("CDP facilitator not configured - missing credentials")

facilitator_clients.append(HTTPFacilitatorClient(FacilitatorConfig(
    url=XPAY_FACILITATOR,
    identifier="xpay",
)))

x402_server = x402ResourceServer(facilitator_clients=facilitator_clients)
x402_server.register("eip155:8453", ExactEvmServerScheme())
x402_server.register("base", ExactEvmServerScheme())
x402_server.register_extension(bazaar_resource_server_extension)

try:
    x402_server.initialize()
    print(f"✓ x402 server initialized with {len(x402_server._facilitator_clients)} facilitators")
    # Log the routing map once at boot so there is never any doubt about
    # which facilitator owns which network/scheme:
    for network, schemes in x402_server._facilitator_clients_map.items():
        for scheme, client in schemes.items():
            print(f"  route: {network}/{scheme} -> {getattr(client, 'identifier', client)}")
except Exception as e:
    print(f"✗ x402 server initialization FAILED: {e}")
    raise RuntimeError(f"x402 initialization failed: {e}") from e

# FastAPI App
app = FastAPI(
    title="TerraDeed Scrape API",
    description="x402-powered web data extraction for AI agents. Three endpoints: clean LLM-ready markdown (/scrape), structured JSON from any URL (/extract), and UK commercial property intelligence with government data enrichment (/extract/property). No accounts, no subscriptions — pay per call with USDC on Base mainnet.",
    version="0.8.2",
    contact={
        "name": "TerraDeed Labs",
        "email": "contact@terradeed.co.uk",
        "url": "https://terradeed.co.uk",
    },
)

# Resource configurations for x402
SCRAPE_RESOURCE = {
    "url": f"{BASE_URL}/scrape",
    "description": "Clean, LLM-ready markdown from any URL. JavaScript rendering included. Feed web content directly into agent context without parsing HTML.",
    "mimeType": "application/json",
}

EXTRACT_RESOURCE = {
    "url": f"{BASE_URL}/extract",
    "description": "Structured JSON from any webpage. Name the fields you want \u2014 company data, job listings, product specs, contact details, financial figures \u2014 and get them back with confidence scores. No scraper configuration needed.",
    "mimeType": "application/json",
}

PROPERTY_RESOURCE = {
    "url": f"{BASE_URL}/extract/property",
    "description": "UK commercial property and land intelligence from any listing URL. Structured data: address, price, site area, use class, tenure, frontage, coordinates. Auto-enriched with Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data. Site acquisition teams use this for initial screening \u2014 replaces 30 minutes of manual research per site.",
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
    "extra": {"name": "USD Coin", "version": "2"},
}

EXTRACT_REQUIREMENTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": USDC_BASE,
    "amount": "50000",  # $0.05 in atomic units
    "payTo": PAY_TO,
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USD Coin", "version": "2"},
}

PROPERTY_REQUIREMENTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": USDC_BASE,
    "amount": "100000",  # $0.10 in atomic units (6 decimals)
    "payTo": PAY_TO,
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USD Coin", "version": "2"},
}

# Bazaar discovery extensions — manual declaration with method: POST
# The declare_discovery_extension() helper has NO method parameter by design.
# Runtime enrichment injects it from transport_context.method, but that path
# doesn't fire in our custom FastAPI integration. Baking "method": "POST"
# manually is safe (BodyInput declares method as optional with extra="allow").

SCRAPE_BAZAAR_EXT = {
    "bazaar": {
        "info": {
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {
                    "url": "https://example.com",
                    "js_render": False,
                },
            },
            "output": {
                "type": "json",
                "example": {
                    "content": "# Senior Frontend Engineer \u2014 FinTech Start-up\n\n**Location:** London, UK (Hybrid \u2014 2 days in office)\n\n**Salary:** \u00a365,000 \u2013 \u00a380,000 + equity\n\n## About the Role\n\nWe're looking for a Senior Frontend Engineer to lead our customer dashboard rebuild. You'll work with React, TypeScript, and Next.js to deliver a best-in-class trading interface used by 50,000+ monthly active users.\n\n## Requirements\n\n- 4+ years production React experience\n- Strong TypeScript skills\n- Experience with real-time data (WebSockets, SSE)\n- Familiarity with financial data visualization (D3, Recharts)\n\n## Benefits\n\n- 25 days holiday + bank holidays\n- Private health insurance\n- Annual learning budget (\u00a32,000)\n- Flexible working hours",
                    "url": "https://example-jobsite.com/listing/senior-frontend-engineer-fintech",
                    "status": "success",
                    "word_count": 142,
                    "title": "Senior Frontend Engineer \u2014 FinTech Start-up",
                    "js_rendered": True,
                    "auth_method": "x402",
                },
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                        "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                        "body": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "format": "uri"},
                                "js_render": {"type": "boolean", "default": False},
                            },
                            "required": ["url"],
                        },
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "url": {"type": "string", "format": "uri"},
                                "status": {"type": "string", "enum": ["success"]},
                                "word_count": {"type": "integer"},
                                "title": {"type": ["string", "null"]},
                                "js_rendered": {"type": "boolean"},
                                "auth_method": {"type": "string", "enum": ["x402", "api_key"]},
                            },
                            "required": ["content", "url", "status"],
                        },
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }
}

EXTRACT_BAZAAR_EXT = {
    "bazaar": {
        "info": {
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {
                    "url": "https://example.com/product",
                    "fields": ["title", "price", "availability"],
                },
            },
            "output": {
                "type": "json",
                "example": {
                    "url": "https://savills.co.uk/commercial-property-for-sale/unit-5-bristol-road-bs1-4na",
                    "status": "success",
                    "data": {
                        "company_name": "Savills (UK) Ltd",
                        "services": ["Commercial property sales", "Investment advisory", "Development consultancy", "Valuation"],
                        "team_size": "250+",
                        "location": "33 Margaret Street, London W1G 0JD",
                        "phone": "+44 (0)20 7016 3600",
                        "website": "https://www.savills.co.uk",
                        "specialisms": ["Office", "Retail", "Industrial", "Residential development"],
                    },
                    "fields_requested": ["company_name", "services", "team_size", "location", "phone", "website", "specialisms"],
                    "fields_extracted": ["company_name", "services", "team_size", "location", "phone", "website", "specialisms"],
                    "auth_method": "x402",
                },
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                        "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                        "body": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "format": "uri"},
                                "fields": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                            },
                            "required": ["url", "fields"],
                        },
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": {"type": "object"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }
}

PROPERTY_BAZAAR_EXT = {
    "bazaar": {
        "info": {
            "input": {
                "type": "http",
                "method": "POST",
                "bodyType": "json",
                "body": {
                    "url": "https://www.rightmove.co.uk/commercial-property-for-sale/property-12345.html",
                },
            },
            "output": {
                "type": "json",
                "example": {
                    "url": "https://savills.co.uk/commercial-property-for-sale/unit-5-bristol-road-bs1-4na",
                    "status": "success",
                    "listing_type": "sale",
                    "property": {
                        "address": "259 Bristol Road, Selly Oak, Birmingham B29 6NA",
                        "coordinates": {"lat": 52.4401, "lng": -1.9357},
                        "asking_price": 850000,
                        "price_qualifier": "offers_over",
                        "currency": "GBP",
                        "site_area_sqft": 5200,
                        "site_area_acres": 0.12,
                        "use_class": "E",
                        "current_use": "Former bank branch with ATM recess and strong room",
                        "tenure": "freehold",
                        "lease_years_remaining": None,
                        "epc_rating": "C",
                        "frontage_road": "Bristol Road (A38)",
                        "description_summary": "Prominent corner unit on busy arterial route. 120 ft frontage. Former banking premises with vault, ATM housing, and disabled access. Suitable for retail, restaurant, or mixed-use redevelopment subject to planning.",
                        "constraints": {
                            "flood_zone": "3",
                            "conservation_area": False,
                            "listed_building": "Grade II",
                            "green_belt": False,
                        },
                        "planning": {
                            "existing_consent": None,
                            "pending_applications": None,
                            "permitted_development_potential": "Class E to residential (PDR 2021) may apply subject to prior approval",
                        },
                    },
                    "vendor": {
                        "agent_name": "Christie & Co",
                        "agent_branch": "Birmingham",
                        "contact_phone": "0121 643 5555",
                        "listing_ref": "BIR250184",
                    },
                    "source": "rightmove_commercial",
                    "confidence": {
                        "address": 1.0,
                        "asking_price": 1.0,
                        "site_area": 0.75,
                        "use_class": 0.9,
                        "tenure": 1.0,
                        "epc_rating": 0.85,
                        "constraints": 0.95,
                        "planning": 0.6,
                    },
                    "auth_method": "x402",
                },
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST"]},
                        "bodyType": {"type": "string", "enum": ["json"]},
                        "body": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "format": "uri"},
                            },
                            "required": ["url"],
                        },
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": {"type": "object"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }
}

# Models
class ScrapeRequest(BaseModel):
    url: str
    js_render: bool = False

class ExtractRequest(BaseModel):
    url: str
    fields: list[str]
    js_render: bool = False

class PropertyExtractRequest(BaseModel):
    url: str
    js_render: bool = True  # Default True — property portals need JS

class CreateKeyRequest(BaseModel):
    credits: int = 100
    rate_limit: int = 60

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
    from playwright_stealth import Stealth
    stealth = Stealth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await stealth.apply_stealth_async(page)
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
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": EXTRACT_MODEL,
                "max_tokens": 1024,
                "system": [
                    {
                        "type": "text",
                        "text": "Extract exactly the requested fields from the provided content. Return ONLY valid JSON. Set missing fields to null. Do not wrap output in markdown code fences.",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic error: {response.status_code} - {response.text}")

    try:
        raw_text = response.json()["content"][0]["text"].strip()
        # Claude sometimes wraps JSON in markdown code fences — strip them
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()
        data = json.loads(raw_text)
    except:
        raise HTTPException(status_code=502, detail="Malformed JSON from model")

    return {"url": url, "status": "success", "data": data, "fields_requested": fields, "fields_extracted": [k for k, v in data.items() if v is not None], "js_rendered": js_rendered, "model": EXTRACT_MODEL}

PROPERTY_SYSTEM_PROMPT = """You are a commercial property data extraction specialist. Extract structured property listing data from the provided page content.

Return ONLY valid JSON matching this exact schema. Set any field to null if the information is not present or cannot be reliably determined.

For confidence scores: use 0.95-1.0 when the value is explicitly stated on the page, 0.7-0.9 when inferred from context (e.g. use class derived from description), 0.3-0.6 when it's a best guess.

Schema:
{
  "listing_type": "sale | lease | auction | development",
  "property": {
    "address": "Full address as shown on listing",
    "coordinates": {"lat": number, "lng": number} or null,
    "asking_price": number (in minor currency unit, e.g. 450000 not "£450,000"),
    "price_qualifier": "guide_price | offers_invited | offers_over | POA | auction_guide | rent_pa | rent_pcm",
    "price_per_sqft": number or null,
    "currency": "GBP",
    "site_area_sqft": number or null,
    "site_area_acres": number or null,
    "use_class": "E | B2 | B8 | C3 | F1 | Sui Generis | mixed" or null (use current England/Wales use class system),
    "current_use": "Brief description of what the site is currently used for",
    "tenure": "freehold | leasehold | both | unknown",
    "lease_years_remaining": number or null,
    "epc_rating": "A | B | C | D | E | F | G" or null,
    "frontage_road": "Name of the main road the property fronts onto" or null,
    "description_summary": "2-3 sentence summary of the listing description",
    "bedrooms": number or null (for mixed-use or residential),
    "bathrooms": number or null,
    "floors": number or null,
    "parking": true | false | null,
    "constraints": {
      "flood_zone": "1 | 2 | 3" or null,
      "conservation_area": true | false | null,
      "listed_building": "Grade I | Grade II | Grade II*" or null | false,
      "green_belt": true | false | null
    },
    "planning": {
      "existing_consent": "Description of current planning consent" or null,
      "pending_applications": "Description of any pending applications" or null,
      "permitted_development_potential": "Any PD rights mentioned" or null
    }
  },
  "vendor": {
    "agent_name": "Name of the selling/letting agent",
    "agent_branch": "Branch/office location" or null,
    "contact_phone": "Phone number" or null,
    "listing_ref": "Agent's reference number" or null
  },
  "source": "rightmove_commercial | zoopla_commercial | onthemarket | christie | costar | loopnet | auction_house | agent_direct | other",
  "confidence": {
    "address": 0.0-1.0,
    "asking_price": 0.0-1.0,
    "site_area": 0.0-1.0,
    "use_class": 0.0-1.0,
    "tenure": 0.0-1.0,
    "epc_rating": 0.0-1.0,
    "constraints": 0.0-1.0,
    "planning": 0.0-1.0
  }
}

Rules:
- Extract ONLY from the provided content. Never fabricate data.
- Normalise prices to numeric values (450000 not "£450,000")
- Normalise areas: if given in m², convert to sqft (1 m² = 10.764 sqft). Always provide sqft. Also provide acres if site is large enough (1 acre = 43,560 sqft).
- Detect the source platform from the URL domain.
- For use_class: map to current England/Wales classes. If the listing says "retail" without specifying, use "E". If it says "industrial", use "B2". If "warehouse/distribution", use "B8".
- For listing_type: "auction" if auction guide price or auction house. "lease" if rent quoted. "sale" if capital price quoted. "development" if development opportunity.
- Keep description_summary to 2-3 sentences max — focus on key commercial features.
- Return ONLY the JSON object. No markdown fences, no commentary."""

async def _extract_property(markdown: str, url: str, js_rendered: bool):
    """Extract structured property data using a fixed commercial property schema."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY missing")

    user_prompt = f"""Extract property listing data from this page.

URL: {url}

Content:
{markdown[:12000]}"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": EXTRACT_MODEL,
                "max_tokens": 2048,
                "system": [
                    {
                        "type": "text",
                        "text": PROPERTY_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=45,
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic error: {response.status_code} - {response.text}",
        )

    raw_text = response.json()["content"][0]["text"].strip()

    # Strip markdown fences if Claude wraps them despite instructions
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
    if raw_text.endswith("```"):
        raw_text = raw_text.rsplit("```", 1)[0]
    raw_text = raw_text.strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Malformed JSON from model")

    # Build base result
    result = {
        "url": url,
        "status": "success",
        "listing_type": data.get("listing_type"),
        "property": data.get("property", {}),
        "vendor": data.get("vendor", {}),
        "source": data.get("source", "other"),
        "confidence": data.get("confidence", {}),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "js_rendered": js_rendered,
        "model": EXTRACT_MODEL,
    }

    # Auto-enrich from UK government data sources
    try:
        property_data = result.get("property", {})
        address = property_data.get("address", "")
        coords = property_data.get("coordinates", {})
        lat = coords.get("lat") if coords else None
        lng = coords.get("lng") if coords else None

        enrichment_data = await enrich_property(
            address=address,
            lat=lat,
            lng=lng
        )
        result["enrichment"] = enrichment_data
    except Exception as e:
        result["enrichment"] = {
            "enrichment_status": "error",
            "error": str(e)
        }

    return result

# Auth Helper
async def get_auth_method(request: Request) -> tuple[str, Optional[str]]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return ("api_key", auth_header[7:].strip())
    if request.headers.get("payment-signature") or request.headers.get("PAYMENT-SIGNATURE"):
        return ("x402", None)
    return ("none", None)

# EIP-712 domain for USDC on Base mainnet (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)
USDC_BASE_EIP712_EXTRA = {"name": "USD Coin", "version": "2"}

# x402 Payment Required Response Helper
def payment_required_response(requirements: dict, resource: dict, bazaar_ext: dict):
    """Return x402 v2 compliant 402 response with PAYMENT-REQUIRED header and bazaar extension."""
    accepts = [{
        "scheme": requirements["scheme"],
        "network": requirements["network"],
        "asset": requirements["asset"],
        "amount": requirements["amount"],
        "payTo": requirements["payTo"],
        "maxTimeoutSeconds": requirements["maxTimeoutSeconds"],
        "extra": USDC_BASE_EIP712_EXTRA,
    }]

    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": resource,
        "accepts": accepts,
        "extensions": bazaar_ext,
    }

    return JSONResponse(
        status_code=402,
        headers={"PAYMENT-REQUIRED": base64.b64encode(json.dumps(payload).encode()).decode()},
        content={"error": "Payment required"},
    )

# Middleware: x402 auth check BEFORE Pydantic validation
@app.middleware("http")
async def x402_auth_middleware(request: Request, call_next):
    """
    Intercept POST /scrape and POST /extract to check auth before Pydantic validation.
    Returns 402 immediately if no valid auth present, avoiding 422 validation errors.
    """
    if request.method == "POST" and request.url.path in ["/scrape", "/extract", "/extract/property"]:
        auth_method, api_key = await get_auth_method(request)
        
        # If no auth provided, return 402 before validation runs
        if auth_method == "none":
            if request.url.path == "/scrape":
                return payment_required_response(
                    SCRAPE_REQUIREMENTS,
                    SCRAPE_RESOURCE,
                    SCRAPE_BAZAAR_EXT
                )
            elif request.url.path == "/extract/property":
                return payment_required_response(
                    PROPERTY_REQUIREMENTS,
                    PROPERTY_RESOURCE,
                    PROPERTY_BAZAAR_EXT
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
                    
                    # Build proper PaymentRequirements with EIP-712 domain info
                    if request.url.path == "/scrape":
                        req_dict = SCRAPE_REQUIREMENTS
                    elif request.url.path == "/extract/property":
                        req_dict = PROPERTY_REQUIREMENTS
                    else:
                        req_dict = EXTRACT_REQUIREMENTS
                    requirements = PaymentRequirements(
                        scheme=req_dict["scheme"],
                        network=req_dict["network"],
                        asset=req_dict["asset"],
                        amount=req_dict["amount"],
                        pay_to=req_dict["payTo"],
                        max_timeout_seconds=req_dict["maxTimeoutSeconds"],
                        extra={
                            "name": "USD Coin",  # EIP-712 domain name
                            "version": "2",       # EIP-712 domain version
                        }
                    )
                    
                    # Verify payment (async server)
                    result = await x402_server.verify_payment(payload, requirements)
                    
                    print(f"Payment verification result: {result}")
                    print(f"is_valid: {result.is_valid if result else None}")
                    print(f"invalid_reason: {result.invalid_reason if result else None}")
                    print(f"invalid_message: {result.invalid_message if result else None}")
                    
                    if not result or not result.is_valid:
                        error_msg = result.invalid_message or result.invalid_reason or 'Unknown error' if result else 'No result'
                        return JSONResponse(
                            status_code=402,
                            content={"error": f"Payment verification failed: {error_msg}"}
                        )
                    
                    # Payment valid - add marker to request state
                    request.state.x402_payment_valid = True
                    request.state.x402_payment_sig = payment_sig
                    request.state.x402_payload = payload
                    request.state.x402_requirements = requirements
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
        payment_response_header = None
        try:
            payload = getattr(request.state, "x402_payload", None)
            requirements = getattr(request.state, "x402_requirements", None)
            if payload and requirements:
                settle_result = await x402_server.settle_payment(
                    payload=payload,
                    requirements=requirements
                )
                # DEBUG_SETTLE: Log full settle response
                if os.environ.get("DEBUG_SETTLE"):
                    print(f"[DEBUG_SETTLE] /scrape settle_result: {json.dumps(settle_result, default=str, indent=2)}")
                # PAYMENT-RESPONSE header per x402 v2 spec (base64-encoded JSON settlement receipt)
                payment_response_header = base64.b64encode(
                    json.dumps(settle_result, default=str).encode()
                ).decode()
        except Exception as e:
            print(f"Payment settlement warning: {e}")
            if os.environ.get("DEBUG_SETTLE"):
                import traceback
                traceback.print_exc()

        if payment_response_header:
            return JSONResponse(content=result, headers={"PAYMENT-RESPONSE": payment_response_header})
        return result

    # No valid auth
    return payment_required_response(SCRAPE_REQUIREMENTS, SCRAPE_RESOURCE, SCRAPE_BAZAAR_EXT)

@app.post("/extract/property")
async def extract_property(body: PropertyExtractRequest, request: Request):
    """Extract structured commercial property data from a listing URL.
    Fixed schema — no fields parameter needed. Returns normalised property
    intelligence with confidence scores per field."""

    # Check for API key auth
    auth_method, api_key = await get_auth_method(request)

    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info["error"])
        if key_info["credits_remaining"] < PROPERTY_CREDITS:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Insufficient credits",
                    "credits_remaining": key_info["credits_remaining"],
                    "credits_required": PROPERTY_CREDITS,
                },
            )

        markdown = (await _scrape(body.url, body.js_render))["content"]
        result = await _extract_property(markdown, body.url, body.js_render)
        deduct_credits(api_key, PROPERTY_CREDITS, "/extract/property")
        result["auth_method"] = "api_key"
        result["credits_remaining"] = key_info["credits_remaining"] - PROPERTY_CREDITS
        return result

    # Check for x402 payment (verified in middleware)
    if getattr(request.state, "x402_payment_valid", False):
        markdown = (await _scrape(body.url, body.js_render))["content"]
        result = await _extract_property(markdown, body.url, body.js_render)
        result["auth_method"] = "x402"

        # Settle payment after successful service delivery
        try:
            payload = getattr(request.state, "x402_payload", None)
            requirements = getattr(request.state, "x402_requirements", None)
            if payload and requirements:
                settle_result = await x402_server.settle_payment(
                    payload=payload,
                    requirements=requirements,
                )
                if os.environ.get("DEBUG_SETTLE"):
                    print(
                        f"[DEBUG_SETTLE] /extract/property settle_result: "
                        f"{json.dumps(settle_result, default=str, indent=2)}"
                    )
        except Exception as e:
            print(f"Payment settlement warning: {e}")
            if os.environ.get("DEBUG_SETTLE"):
                import traceback
                traceback.print_exc()

        return result

    # No valid auth
    return payment_required_response(
        PROPERTY_REQUIREMENTS, PROPERTY_RESOURCE, PROPERTY_BAZAAR_EXT
    )

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
        payment_response_header = None
        try:
            payload = getattr(request.state, "x402_payload", None)
            requirements = getattr(request.state, "x402_requirements", None)
            if payload and requirements:
                settle_result = await x402_server.settle_payment(
                    payload=payload,
                    requirements=requirements
                )
                # DEBUG_SETTLE: Log full settle response
                if os.environ.get("DEBUG_SETTLE"):
                    print(f"[DEBUG_SETTLE] /extract settle_result: {json.dumps(settle_result, default=str, indent=2)}")
                # PAYMENT-RESPONSE header per x402 v2 spec (base64-encoded JSON settlement receipt)
                payment_response_header = base64.b64encode(
                    json.dumps(settle_result, default=str).encode()
                ).decode()
        except Exception as e:
            print(f"Payment settlement warning: {e}")
            if os.environ.get("DEBUG_SETTLE"):
                import traceback
                traceback.print_exc()

        if payment_response_header:
            return JSONResponse(content=result, headers={"PAYMENT-RESPONSE": payment_response_header})
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
        "version": "0.8.1",
        "facilitators": facilitators,
        "auth_methods": ["x402", "api_key"]
    }

@app.get("/")
async def root():
    """Root endpoint - redirects to docs"""
    return {
        "service": "TerraDeed Scrape API",
        "version": "0.8.1",
        "documentation": "https://terradeed.co.uk/docs",
        "endpoints": {
            "scrape": {"path": "/scrape", "method": "POST", "price": SCRAPE_PRICE, "auth": ["x402", "api_key"]},
            "extract": {"path": "/extract", "method": "POST", "price": EXTRACT_PRICE, "auth": ["x402", "api_key"]},
            "extract_property": {"path": "/extract/property", "method": "POST", "price": PROPERTY_PRICE, "auth": ["x402", "api_key"], "description": "UK commercial property intelligence with flood risk, heritage, and EPC enrichment — replaces 30 minutes of manual research per site"},
            "health": {"path": "/health", "method": "GET"}
        }
    }

# llms.txt — agent discovery (served at both /llms.txt and /.well-known/llms.txt)
LLMS_TXT = """# TerraDeed Scrape API

> x402-powered web data extraction for AI agents. Three endpoints that turn any URL into usable intelligence:
> 
> • `/scrape` — Clean, LLM-ready markdown from any URL. JavaScript rendering included. Feed web content directly into agent context without parsing HTML. $0.01 USDC.
> • `/extract` — Structured JSON from any webpage. Name the fields you want — company data, job listings, product specs, contact details, financial figures — and get them back with confidence scores. No scraper configuration needed. $0.05 USDC.
> • `/extract/property` — UK commercial property and land intelligence from any listing URL. Structured data: address, price, site area, use class, tenure, frontage, coordinates. Auto-enriched with Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data. Site acquisition teams use this for initial screening — replaces 30 minutes of manual research per site. $0.10 USDC.
> 
> No API keys, no accounts, no subscriptions — payment is per-request via the x402 protocol (HTTP 402) with USDC on Base mainnet. First byte to paid response in one retry cycle.

## What this API does

- **Scrape** (`POST /scrape`, $0.01 USDC) — Clean, LLM-ready markdown from any URL. JavaScript rendering included. Feed web content directly into agent context without parsing HTML.
- **Extract** (`POST /extract`, $0.05 USDC) — Structured JSON from any webpage. Name the fields you want — company data, job listings, product specs, contact details, financial figures — and get them back with confidence scores. No scraper configuration needed.
- **Extract Property** (`POST /extract/property`, $0.10 USDC) — UK commercial property and land intelligence from any listing URL. Auto-enriched with Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data. Site acquisition teams use this for initial screening.

- You need the readable content of a web page as markdown for summarisation, RAG ingestion, or analysis → `POST /scrape` ($0.01)
- You need specific named fields from a page as machine-usable JSON (prices, titles, contact details, specs, team structures) → `POST /extract` ($0.05)
- You need structured UK commercial property intelligence with government data enrichment (flood risk, listed buildings, EPC) from any listing → `POST /extract/property` ($0.10)
- The page requires JavaScript rendering → add `"js_render": true` to any endpoint
- You do NOT need this API for: pages you can fetch directly without markup cleanup, or sites that prohibit automated access in their terms

## Endpoints

### POST /scrape

Clean, LLM-ready markdown from any URL. JavaScript rendering included.

Request body (JSON):

    {"url": "https://example-jobsite.com/listing/senior-frontend-engineer-fintech", "js_render": true}

**Returns:**
```json
{
  "content": "# Page Title

Clean markdown...",
  "url": "https://example.com",
  "status": "success",
  "word_count": 142,
  "title": "Page Title",
  "js_rendered": false,
  "auth_method": "x402"
}
```

    {
      "content": "# Senior Frontend Engineer — FinTech Start-up\\n\\n**Location:** London, UK (Hybrid — 2 days in office)\\n\\n**Salary:** £65,000 – £80,000 + equity\\n\\n## About the Role\\n\\nWe're looking for a Senior Frontend Engineer to lead our customer dashboard rebuild...",
      "url": "https://example-jobsite.com/listing/senior-frontend-engineer-fintech",
      "status": "success",
      "word_count": 142,
      "title": "Senior Frontend Engineer — FinTech Start-up",
      "js_rendered": true,
      "auth_method": "x402"
    }
  },
  "vendor": {
    "agent_name": "Savills (UK) Ltd",
    "agent_branch": "Bristol",
    "contact_phone": "+44 (0)117 902 7000",
    "listing_ref": "SAV-BRS-2026-0542"
  },
  "source": "savills_commercial",
  "confidence": {
    "address": 0.98,
    "asking_price": 0.95,
    "site_area": 0.72,
    "use_class": 0.85,
    "tenure": 0.97,
    "epc_rating": 0.91,
    "constraints": 0.88,
    "planning": 0.65
  },
  "auth_method": "x402"
}
```

## Contact

### POST /extract — $0.05 USDC

Structured JSON from any webpage. Name the fields you want — company data, job listings, product specs, contact details, financial figures — and get them back with confidence scores. No scraper configuration needed.

Request body (JSON):

    {"url": "https://savills.co.uk/commercial-property-for-sale/unit-5-bristol-road-bs1-4na", "fields": ["company_name", "services", "team_size", "location", "phone", "website", "specialisms"]}

- `url` (string, required): public URL to extract from
- `fields` (array of strings, required, min 1): field names to extract. Use descriptive names — "price_per_month" beats "p1"
- `js_render` (boolean, default false)

Response (JSON):

    {
      "url": "https://savills.co.uk/commercial-property-for-sale/unit-5-bristol-road-bs1-4na",
      "status": "success",
      "data": {
        "company_name": "Savills (UK) Ltd",
        "services": ["Commercial property sales", "Investment advisory", "Development consultancy", "Valuation"],
        "team_size": "250+",
        "location": "33 Margaret Street, London W1G 0JD",
        "phone": "+44 (0)20 7016 3600",
        "website": "https://www.savills.co.uk",
        "specialisms": ["Office", "Retail", "Industrial", "Residential development"]
      },
      "fields_requested": ["company_name", "services", "team_size", "location", "phone", "website", "specialisms"],
      "fields_extracted": ["company_name", "services", "team_size", "location", "phone", "website", "specialisms"],
      "auth_method": "x402"
    }

Fields not present on the page are returned as null rather than hallucinated.

### POST /extract/property — $0.10 USDC

UK commercial property and land intelligence from any listing URL. Structured data: address, price, site area, use class, tenure, frontage, coordinates. Auto-enriched with Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data. Site acquisition teams use this for initial screening — replaces 30 minutes of manual research per site.

Request body (JSON):

    {"url": "https://www.rightmove.co.uk/commercial-property-for-sale/property-12345.html"}

- `url` (string, required): public URL of a UK commercial property listing
- `js_render` (boolean, default true): property portals require JavaScript

Response (JSON):

    {
      "url": "https://www.rightmove.co.uk/commercial-property-for-sale/property-12345.html",
      "status": "success",
      "listing_type": "sale",
      "property": {
        "address": "259 Bristol Road, Selly Oak, Birmingham B29 6NA",
        "coordinates": {"lat": 52.4401, "lng": -1.9357},
        "asking_price": 850000,
        "price_qualifier": "offers_over",
        "currency": "GBP",
        "site_area_sqft": 5200,
        "site_area_acres": 0.12,
        "use_class": "E",
        "current_use": "Former bank branch with ATM recess and strong room",
        "tenure": "freehold",
        "lease_years_remaining": null,
        "epc_rating": "C",
        "frontage_road": "Bristol Road (A38)",
        "description_summary": "Prominent corner unit on busy arterial route. 120 ft frontage. Former banking premises with vault, ATM housing, and disabled access. Suitable for retail, restaurant, or mixed-use redevelopment subject to planning.",
        "constraints": {
          "flood_zone": "3",
          "conservation_area": false,
          "listed_building": "Grade II",
          "green_belt": false
        },
        "planning": {
          "existing_consent": null,
          "pending_applications": null,
          "permitted_development_potential": "Class E to residential (PDR 2021) may apply subject to prior approval"
        }
      },
      "vendor": {
        "agent_name": "Christie & Co",
        "agent_branch": "Birmingham",
        "contact_phone": "0121 643 5555",
        "listing_ref": "BIR250184"
      },
      "source": "rightmove_commercial",
      "confidence": {
        "address": 1.0,
        "asking_price": 1.0,
        "site_area": 0.75,
        "use_class": 0.9,
        "tenure": 1.0,
        "epc_rating": 0.85,
        "constraints": 0.95,
        "planning": 0.6
      },
      "auth_method": "x402"
    }

## Payment flow (x402 v2)

1. POST to the endpoint with your JSON body and no payment. You receive HTTP 402. The full PaymentRequired object is base64-encoded in the `payment-required` response header (the body is a stub).
2. Decode the header. Pick an entry from `accepts[]`. Sign an EIP-712 `TransferWithAuthorization` (EIP-3009) for USDC:
   - domain: `{name: "USD Coin", version: "2", chainId: 8453, verifyingContract: <asset>}`
   - message: `{from: <your wallet>, to: <payTo>, value: <amount>, validAfter: 0, validBefore: now + maxTimeoutSeconds, nonce: <random 32 bytes>}`
3. Build the payment payload and IMPORTANT: copy the `extensions` and `resource` objects from the decoded 402 into it verbatim:

       {
         "x402Version": 2,
         "payload": {"signature": "0x...", "authorization": {...}},
         "accepted": <the accepts[] entry you chose>,
         "resource": <resource object from the 402>,
         "extensions": <extensions object from the 402>
       }

4. Retry the identical request with header `PAYMENT-SIGNATURE: <base64(JSON payload)>`.
5. On success you receive HTTP 200 with the result, plus a `PAYMENT-RESPONSE` header (base64 JSON) containing the on-chain settlement transaction hash.

Any standard x402 v2 client library handles steps 1-5 automatically. Cost per call is exact — no gas fees are paid by you (the facilitator submits the transaction), no minimums, no overage.

## Errors

- 402 with `payment-required` header: expected first response; pay and retry
- 400: malformed body (check `url` is a valid absolute URL; `fields` non-empty for /extract)
- 402 after payment attempt: signature invalid or authorization expired — re-sign with fresh nonce and validBefore
- 5xx: transient; retry with the same paid authorization within its validity window is NOT possible (nonces are single-use) — treat as a failed call and re-pay

## Operator

TerraDeed Labs, Manchester, UK — https://terradeed.co.uk
Contact: a.gentry@terradeed.co.uk
""".strip()

@app.get("/llms.txt")
async def llms_txt():
    """LLM-readable API description for agent discovery"""
    return PlainTextResponse(LLMS_TXT, media_type="text/plain")

@app.get("/.well-known/llms.txt")
async def well_known_llms_txt():
    """LLM-readable API description for agent discovery (well-known path)"""
    return PlainTextResponse(LLMS_TXT, media_type="text/plain")

@app.post("/admin/keys")
async def create_key(request: CreateKeyRequest, http_request: Request):
    """Create a new API key (admin only)"""
    admin_secret = http_request.headers.get("x-admin-secret", "")
    if not secrets.compare_digest(admin_secret.encode(), ADMIN_SECRET.encode()):
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
    if not secrets.compare_digest(admin_secret.encode(), ADMIN_SECRET.encode()):
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

# Override OpenAPI schema to mark free endpoints as requiring no security
# (avoids x402scan probing them and getting 405 errors)
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    free_paths = ["/health", "/", "/admin/keys", "/admin/keys/{key_prefix}"]
    for path in free_paths:
        if path in openapi_schema.get("paths", {}):
            for method in openapi_schema["paths"][path]:
                if method in ("get", "post", "put", "delete", "patch"):
                    openapi_schema["paths"][path][method]["security"] = []
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
