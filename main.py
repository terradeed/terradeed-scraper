"""
TerraDeed Labs - Web Scraping API
Dual Authentication: x402 USDC + API Keys
Version 0.9.0 - UK public tenders feed (Find a Tender + Contracts Finder, OGL)
"""

import asyncio
import base64
import json
import os
import sqlite3
import secrets
import hashlib
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Optional
from contextlib import contextmanager

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
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
from uk_tenders import TenderQueryError, TenderService, TenderUpstreamError, grants_stub

# Config
PAY_TO = "0x4E024e356bd01853654b7B5196F2B85F67Cc39EC"
SCRAPE_PRICE = "$0.01"
EXTRACT_PRICE = "$0.05"
SCRAPE_CREDITS = 1
EXTRACT_CREDITS = 5
PROPERTY_PRICE = "$0.10"
PROPERTY_CREDITS = 10
TENDER_PRICE = "$0.02"
TENDER_CREDITS = 2
APP_VERSION = "0.9.0"
API_TITLE = "Terradeed Scrape API"
USDC_DECIMALS = 6
NETWORK_CLIENT = "base"
BASE_URL = "https://api.terradeed.co.uk"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXTRACT_MODEL = "claude-sonnet-4-6"
# Sonnet 4.6 accepts this and rejects "between_tools". Omitting the field
# also leaves thinking off on 4.6. Sending "disabled" keeps a later model
# that thinks by default from turning it on with no other code change.
# Sonnet 5.5 rejects "disabled" (HTTP 400) and uses "between_tools" instead.
EXTRACT_THINKING = {"type": "disabled"}

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

def api_description() -> str:
    """Public description. Prices come from the constants used by the paid routes."""
    return (
        "Pay-per-call web data for AI agents, settled in USDC on Base mainnet. "
        f"POST /scrape returns clean markdown ({SCRAPE_PRICE}). "
        f"POST /extract returns named JSON fields ({EXTRACT_PRICE}). "
        f"POST /extract/property returns UK commercial property data ({PROPERTY_PRICE}). "
        f"GET /v1/uk/tenders returns UK public tender notices from Find a Tender and "
        f"Contracts Finder, up to 25 results ({TENDER_PRICE}). "
        "GET /v1/uk/grants is free and does not republish competition text. "
        "A call without payment returns HTTP 402. "
        "Failed calls are not settled and credits are not deducted."
    )


def api_guidance() -> str:
    """Agent guidance for OpenAPI info.x-guidance."""
    return (
        "Send the request with no payment first. Read the PAYMENT-REQUIRED header "
        "on the HTTP 402 response, sign an x402 v2 USDC transfer on Base (eip155:8453), "
        "and retry with the PAYMENT-SIGNATURE header. "
        f"POST /scrape ({SCRAPE_PRICE}) takes url and optional js_render and returns markdown. "
        f"POST /extract ({EXTRACT_PRICE}) takes url and fields and returns JSON. "
        f"POST /extract/property ({PROPERTY_PRICE}) takes a UK listing url. "
        f"GET /v1/uk/tenders ({TENDER_PRICE}) takes q, region, updated_from, stage, "
        "limit (1 to 25), and cursor. "
        "An API key in Authorization: Bearer is accepted instead of x402. "
        "Payment is taken only when the response is successful and contains usable content."
    )


# FastAPI App
app = FastAPI(
    title=API_TITLE,
    description=api_description(),
    version=APP_VERSION,
    contact={
        "name": "Terradeed",
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
    "description": "Structured JSON from any webpage. Name the fields you want \u2014 company data, job listings, product specs, contact details, financial figures \u2014 and get them back. No scraper configuration needed.",
    "mimeType": "application/json",
}

PROPERTY_RESOURCE = {
    "url": f"{BASE_URL}/extract/property",
    "description": "UK commercial property and land intelligence from any listing URL. Structured data: address, price, site area, use class, tenure, frontage, coordinates. Attempts enrichment from Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data. Each source reports its own status; enrichment is not guaranteed.",
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

TENDER_RESOURCE = {
    "url": f"{BASE_URL}/v1/uk/tenders",
    "description": "UK public tender notices from Find a Tender and Contracts Finder. Search by keyword, region, stage, and update time. Up to 25 notices per call. Open Government Licence v3.0. Contact names, emails, and phone numbers are omitted.",
    "mimeType": "application/json",
}

TENDER_REQUIREMENTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": USDC_BASE,
    "amount": "20000",  # $0.02 in atomic units
    "payTo": PAY_TO,
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USD Coin", "version": "2"},
}

TENDER_BAZAAR_EXT = {
    "bazaar": {
        "info": {
            "input": {
                "type": "http",
                "method": "GET",
                "queryParams": {
                    "q": "AI training",
                    "region": "north-west",
                    "updated_from": "2026-11-01",
                    "stage": "tender",
                    "limit": 25,
                },
            },
            "output": {
                "type": "json",
                "example": {
                    "licence": "OGL-UK-3.0",
                    "attribution": "Contains public sector information licensed under the Open Government Licence v3.0.",
                    "licence_url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
                    "sources": ["find-tender", "contracts-finder"],
                    "results": [
                        {
                            "ocid": "ocds-h6vhtk-000000",
                            "title": "AI training services",
                            "buyer": "Example Council",
                            "value_gbp": 100000,
                            "published": "2026-11-03T12:00:00Z",
                            "deadline": "2026-12-01T12:00:00Z",
                            "stage": "tender",
                            "region_hints": ["north-west"],
                            "url": "https://www.find-tender.service.gov.uk/Notice/000001-2026",
                            "source": "find-tender",
                        }
                    ],
                    "next_cursor": None,
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
                        "method": {"type": "string", "enum": ["GET", "HEAD", "DELETE"]},
                        "queryParams": {
                            "type": "object",
                            "properties": {
                                "q": {"type": "string"},
                                "region": {"type": "string"},
                                "updated_from": {"type": "string"},
                                "stage": {"type": "string", "enum": ["tender", "award", "planning"]},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                                "cursor": {"type": "string"},
                            },
                        },
                    },
                    "required": ["type", "method"],
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": {"type": "object"},
                    },
                },
            },
            "required": ["input"],
        },
    }
}

tender_service = TenderService()

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

# One attempt plus one retry. Each attempt is capped so a hung Chromium
# cannot hold the worker until the platform proxy returns an empty 500.
RENDER_ATTEMPTS = 2
RENDER_ATTEMPT_TIMEOUT_SECONDS = 20
# Analytics and chat widgets often never go idle. Wait briefly, then use the DOM.
RENDER_IDLE_TIMEOUT_MS = 3000
# Railway/Docker ships a 64MB /dev/shm. Chromium crashes without this flag,
# and that crash is what surfaces as an empty 500 on heavier pages.
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
]


class RenderError(Exception):
    """Playwright could not render a page after its retries."""


def _short_error(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return message[:300]


async def _render_page(url: str) -> str:
    """One Playwright attempt. Caller applies the timeout and retry policy."""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    stealth = Stealth()
    timeout_ms = int(RENDER_ATTEMPT_TIMEOUT_SECONDS * 1000)
    navigation_timeout_ms = max(1000, timeout_ms - 5000)
    playwright = await async_playwright().start()
    browser = None
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
            timeout=timeout_ms,
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        # Apply stealth to the context so init scripts run before site JS.
        await stealth.apply_stealth_async(context)
        page = await context.new_page()
        # domcontentloaded returns even when third parties keep the network busy.
        # networkidle is best-effort only. Waiting on it outright was timing out,
        # and the unhandled TimeoutError became an empty HTTP 500.
        await page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=RENDER_IDLE_TIMEOUT_MS)
        except Exception:
            logging.info("networkidle not reached for %s; continuing with current DOM", url)
        return await page.content()
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logging.warning("Playwright browser.close failed for %s", url, exc_info=True)
        try:
            await playwright.stop()
        except Exception:
            logging.warning("Playwright stop failed for %s", url, exc_info=True)


async def _fetch_with_playwright(url: str) -> str:
    """Render url, retrying once. Raises RenderError if both attempts fail."""
    last_error: Exception | None = None
    for attempt in range(1, RENDER_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(
                _render_page(url),
                timeout=RENDER_ATTEMPT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_error = exc
            logging.warning(
                "Playwright render failed for %s (attempt %s/%s): %s",
                url,
                attempt,
                RENDER_ATTEMPTS,
                _short_error(exc),
            )
    raise RenderError(
        f"Playwright render failed after {RENDER_ATTEMPTS} attempts: {_short_error(last_error)}"
    ) from last_error

def _extract_content(html: str) -> tuple[str, str]:
    content = trafilatura.extract(html, output_format="markdown", include_links=False, include_images=False, include_tables=True)
    meta = trafilatura.extract_metadata(html)
    return content, (meta.title if meta else None)


class _HeadMetaParser(HTMLParser):
    """Read the document title and description tags trafilatura leaves out of the body."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._title_done = False
        self._title_parts: list[str] = []
        self.meta_description: str | None = None
        self.og_title: str | None = None
        self.og_description: str | None = None

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        attr = {key.lower(): (value or "") for key, value in attrs if key}
        if name == "title" and not self._title_done:
            self._in_title = True
            self._title_parts = []
        if name != "meta":
            return
        content = attr.get("content", "").strip()
        if not content:
            return
        meta_name = attr.get("name", "").strip().lower()
        prop = attr.get("property", "").strip().lower()
        if meta_name == "description" and self.meta_description is None:
            self.meta_description = content
        if "og:title" in (meta_name, prop) and self.og_title is None:
            self.og_title = content
        if "og:description" in (meta_name, prop) and self.og_description is None:
            self.og_description = content

    def handle_endtag(self, tag):
        if tag.lower() == "title" and self._in_title:
            self._in_title = False
            self._title_done = True

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())


def _page_context(html: str) -> str:
    """Labelled title and descriptions for the extractor.

    Trafilatura's markdown omits ``<title>`` and meta descriptions, so a
    field named title or description would otherwise come back null.
    """
    if not html:
        return ""
    parser = _HeadMetaParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logging.warning("Could not read page title or description", exc_info=True)
        return ""
    lines = []
    if parser.title:
        lines.append(f"Page title: {parser.title}")
    if parser.meta_description:
        lines.append(f"Meta description: {parser.meta_description}")
    if parser.og_title:
        lines.append(f"Open Graph title: {parser.og_title}")
    if parser.og_description:
        lines.append(f"Open Graph description: {parser.og_description}")
    return "\n".join(lines)


def _model_input(markdown: str, page_context: str, limit: int) -> str:
    """Body text for the model, with page metadata first so the limit keeps it."""
    body = markdown or ""
    context = (page_context or "").strip()
    if not context:
        return body[:limit]
    labelled = (
        "Page metadata (not always repeated in the body text):\n"
        f"{context}\n\n"
        "Page content:\n"
        f"{body}"
    )
    return labelled[:limit]


def _hide_page_context(result: dict) -> dict:
    """Drop extractor-only metadata before a scrape response is returned."""
    result.pop("page_context", None)
    return result

def _with_render_meta(result: dict, scraped: dict) -> dict:
    """Copy render outcome onto an extract response, including fallback."""
    result["js_rendered"] = bool(scraped.get("js_rendered"))
    result["js_render_fallback"] = bool(scraped.get("js_render_fallback"))
    if scraped.get("fallback_reason"):
        result["fallback_reason"] = scraped["fallback_reason"]
    return result

async def _scrape(url: str, js_render: bool = False):
    js_rendered = False
    js_render_fallback = False
    fallback_reason = None
    if js_render:
        try:
            html = await _fetch_with_playwright(url)
            js_rendered = True
        except Exception as exc:
            # A renderer crash must not escape the request. BaseHTTPMiddleware
            # turns that into an HTTP 500, and the platform proxy often drops
            # the body. Serve the static page instead and say so.
            fallback_reason = _short_error(exc)
            logging.warning(
                "JS render failed for %s; falling back to static scrape: %s",
                url,
                fallback_reason,
            )
            try:
                html, _ = _fetch_static(url)
            except HTTPException as static_exc:
                detail = static_exc.detail
                if isinstance(detail, str):
                    detail = (
                        f"{detail}. JS render failed ({fallback_reason}) "
                        "and no static page could be used."
                    )
                raise HTTPException(status_code=static_exc.status_code, detail=detail) from exc
            js_render_fallback = True
    else:
        html, _ = _fetch_static(url)

    content, title = _extract_content(html)
    page_context = _page_context(html)

    if not content and not js_render:
        try:
            html = await _fetch_with_playwright(url)
            content, title = _extract_content(html)
            page_context = _page_context(html)
            js_rendered = True
        except Exception:
            logging.warning("Optional JS render failed for %s", url, exc_info=True)

    if not content:
        detail = f"Could not extract content from {url}"
        if js_render_fallback and fallback_reason:
            detail = (
                f"{detail}. JS render failed ({fallback_reason}) "
                "and the static page had no extractable content."
            )
        raise HTTPException(status_code=422, detail=detail)

    result = {
        "content": content,
        "url": url,
        "status": "success",
        "word_count": len(content.split()),
        "title": title,
        "js_rendered": js_rendered,
        "js_render_fallback": js_render_fallback,
        "page_context": page_context,
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result

# Sonnet 5.5 thinks before it answers when the request omits `thinking`.
# That leading block has no "text" key, so content[0]["text"] raises KeyError.
MODEL_NO_TEXT_DETAIL = "Model response contained no usable text"


def _anthropic_message_text(response) -> str:
    """Join the text blocks from an Anthropic Messages response.

    Adaptive thinking is the default on Claude Sonnet 5.5, so a reply can
    start with a thinking block (or another non-text block) and put the
    answer in a later block. Blocks whose type is not ``text`` are skipped.
    A block with no type and a string ``text`` field is kept so a plain
    single-block reply still parses.
    """
    try:
        payload = response.json()
    except Exception:
        logging.warning("Anthropic response body was not JSON", exc_info=True)
        raise HTTPException(status_code=502, detail=MODEL_NO_TEXT_DETAIL)

    content = payload.get("content") if isinstance(payload, dict) else None
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in (None, "text"):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)

    raw_text = "".join(parts).strip()
    if raw_text:
        return raw_text

    kinds: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                kinds.append(str(block.get("type") or "untyped"))
            else:
                kinds.append(type(block).__name__)
    logging.warning("Anthropic response contained no text blocks: %s", kinds)
    raise HTTPException(status_code=502, detail=MODEL_NO_TEXT_DETAIL)


async def _extract_structured(
    markdown: str,
    url: str,
    fields: list[str],
    js_rendered: bool,
    *,
    model: str | None = None,
    page_context: str = "",
):
    """Extract named fields. ``model`` defaults to EXTRACT_MODEL and does not change it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or ANTHROPIC_API_KEY
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY missing")

    model_id = model or EXTRACT_MODEL
    fields_str = ", ".join(f'"{f}"' for f in fields)
    prompt = f"""Extract these fields: [{fields_str}]

Return ONLY valid JSON. Set missing fields to null.

URL: {url}

Content:
{_model_input(markdown, page_context, 8000)}"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": model_id,
                "max_tokens": 1024,
                "thinking": EXTRACT_THINKING,
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

    raw_text = _anthropic_message_text(response)
    try:
        # Claude sometimes wraps JSON in markdown code fences — strip them
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Malformed JSON from model")

    return {"url": url, "status": "success", "data": data, "fields_requested": fields, "fields_extracted": [k for k, v in data.items() if v is not None], "js_rendered": js_rendered, "model": model_id}

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

async def _extract_property(
    markdown: str,
    url: str,
    js_rendered: bool,
    *,
    page_context: str = "",
):
    """Extract structured property data using a fixed commercial property schema."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY missing")

    user_prompt = f"""Extract property listing data from this page.

URL: {url}

Content:
{_model_input(markdown, page_context, 12000)}"""

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
                "thinking": EXTRACT_THINKING,
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

    raw_text = _anthropic_message_text(response)

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
        result["data_complete"] = enrichment_data.get("data_complete", False)
        result["failed_sources"] = enrichment_data.get("failed_sources", [])
    except Exception as e:
        result["enrichment"] = {
            "enrichment_status": "error",
            "data_complete": False,
            "failed_sources": ["enrichment_orchestrator"],
            "error": str(e)
        }
        result["data_complete"] = False
        result["failed_sources"] = ["enrichment_orchestrator"]

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

def _payment_for(path: str):
    """x402 price, resource, and bazaar metadata for a paid path."""
    if path == "/scrape":
        return SCRAPE_REQUIREMENTS, SCRAPE_RESOURCE, SCRAPE_BAZAAR_EXT
    if path == "/extract/property":
        return PROPERTY_REQUIREMENTS, PROPERTY_RESOURCE, PROPERTY_BAZAAR_EXT
    if path == "/extract":
        return EXTRACT_REQUIREMENTS, EXTRACT_RESOURCE, EXTRACT_BAZAAR_EXT
    if path == "/v1/uk/tenders":
        return TENDER_REQUIREMENTS, TENDER_RESOURCE, TENDER_BAZAAR_EXT
    return None


PAID_ROUTES = {
    ("POST", "/scrape"),
    ("POST", "/extract"),
    ("POST", "/extract/property"),
    ("GET", "/v1/uk/tenders"),
}

# Returned when a handler produces 2xx without content worth paying for.
# Upstream 4xx/5xx responses are passed through unchanged.
EMPTY_EXTRACTION_DETAIL = (
    "Empty extraction: no usable content was returned, so this call was not charged."
)


def _arm_credit_charge(request: Request, api_key: str, key_info: dict, credits: int) -> None:
    """Record an API-key charge. It is applied only after a usable 2xx response."""
    request.state.bill_api_key = api_key
    request.state.bill_credits = credits
    request.state.credits_before = key_info["credits_remaining"]


def _value_is_usable(value: Any) -> bool:
    """True when a value is present. False, zero, and nested data all count."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_value_is_usable(item) for item in value.values())
    if isinstance(value, list):
        return any(_value_is_usable(item) for item in value)
    return True


def _payload_is_usable(path: str, payload: Any) -> bool:
    """Whether a paid response is complete enough to settle or deduct for.

    A tender search with an empty result list is usable: the search ran.
    A scrape or extraction with no content is not.
    """
    if not isinstance(payload, dict):
        return False
    if path == "/scrape":
        content = payload.get("content")
        return (
            payload.get("status") == "success"
            and isinstance(content, str)
            and bool(content.strip())
        )
    if path == "/extract":
        data = payload.get("data")
        return (
            payload.get("status") == "success"
            and isinstance(data, dict)
            and any(_value_is_usable(item) for item in data.values())
        )
    if path == "/extract/property":
        prop = payload.get("property")
        return (
            payload.get("status") == "success"
            and isinstance(prop, dict)
            and _value_is_usable(prop)
        )
    if path == "/v1/uk/tenders":
        licence = payload.get("licence")
        return isinstance(payload.get("results"), list) and isinstance(licence, str) and bool(licence.strip())
    return False


async def _read_response_body(response: Response) -> bytes:
    """Read a handler response.

    BaseHTTPMiddleware hands back a stream with no ``body`` attribute.
    A normal Response already has ``body`` and must not be iterated as well.
    """
    body = getattr(response, "body", None)
    if isinstance(body, memoryview):
        return body.tobytes()
    if isinstance(body, str):
        return body.encode()
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    chunks: list[bytes] = []
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return b""
    async for chunk in iterator:
        if isinstance(chunk, dict):
            continue
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


async def _response_payload(response: Response) -> Any:
    raw = await _read_response_body(response)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


async def _settle_x402(request: Request, path: str) -> Optional[str]:
    """Settle a verified payment. Returns the PAYMENT-RESPONSE header, or None.

    The settle call, receipt encoding, and failure logging match the previous
    per-route blocks. A settlement exception does not fail the request.
    """
    payload = getattr(request.state, "x402_payload", None)
    requirements = getattr(request.state, "x402_requirements", None)
    if not payload or not requirements:
        return None
    try:
        settle_result = await x402_server.settle_payment(
            payload=payload,
            requirements=requirements,
        )
        if os.environ.get("DEBUG_SETTLE"):
            print(
                f"[DEBUG_SETTLE] {path} settle_result: "
                f"{json.dumps(settle_result, default=str, indent=2)}"
            )
        return base64.b64encode(
            json.dumps(settle_result, default=str).encode()
        ).decode()
    except Exception as exc:
        print(f"Payment settlement warning: {exc}")
        if os.environ.get("DEBUG_SETTLE"):
            import traceback
            traceback.print_exc()
        return None


def _apply_credit_charge(request: Request, payload: dict, status_code: int) -> JSONResponse:
    """Deduct credits after a usable success. A lost race returns 402 and no body."""
    api_key = request.state.bill_api_key
    credits = request.state.bill_credits
    if not deduct_credits(api_key, credits, request.url.path):
        return JSONResponse(
            status_code=402,
            content={
                "detail": {
                    "error": "Insufficient credits",
                    "credits_remaining": request.state.credits_before,
                    "credits_required": credits,
                }
            },
        )
    body = dict(payload)
    body["auth_method"] = "api_key"
    body["credits_remaining"] = request.state.credits_before - credits
    return JSONResponse(status_code=status_code, content=body)


async def _commit_payment(request: Request, response: Response) -> Response:
    """Settle x402 or deduct credits only when the handler returned usable 2xx content."""
    path = request.url.path
    x402_ok = bool(getattr(request.state, "x402_payment_valid", False))
    api_key = getattr(request.state, "bill_api_key", None)
    if not x402_ok and not api_key:
        return response

    if response.status_code < 200 or response.status_code >= 300:
        logging.info(
            "Not charging %s %s: handler status %s",
            request.method,
            path,
            response.status_code,
        )
        return response

    payload = await _response_payload(response)
    if not _payload_is_usable(path, payload):
        logging.info(
            "Not charging %s %s: response had no usable content",
            request.method,
            path,
        )
        return JSONResponse(status_code=422, content={"detail": EMPTY_EXTRACTION_DETAIL})

    if x402_ok:
        header = await _settle_x402(request, path)
        headers = {"PAYMENT-RESPONSE": header} if header else {}
        return JSONResponse(
            status_code=response.status_code,
            content=payload,
            headers=headers,
        )

    return _apply_credit_charge(request, payload, response.status_code)


# Middleware: x402 auth check BEFORE Pydantic validation
@app.middleware("http")
async def x402_auth_middleware(request: Request, call_next):
    """
    Intercept paid routes to check auth before Pydantic validation.
    Returns 402 immediately if no valid auth present, avoiding 422 validation errors.
    Verified x402 payments are settled, and API-key credits are deducted, only
    after the handler returns a 2xx response with usable content.
    """
    if (request.method, request.url.path) in PAID_ROUTES:
        requirements_dict, resource, bazaar_ext = _payment_for(request.url.path)
        auth_method, api_key = await get_auth_method(request)

        # If no auth provided, return 402 before validation runs
        if auth_method == "none":
            return payment_required_response(requirements_dict, resource, bazaar_ext)

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
                    req_dict = requirements_dict
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

    response = await call_next(request)
    if (request.method, request.url.path) in PAID_ROUTES:
        return await _commit_payment(request, response)
    return response

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

        _arm_credit_charge(request, api_key, key_info, SCRAPE_CREDITS)
        result = _hide_page_context(await _scrape(body.url, body.js_render))
        result["auth_method"] = "api_key"
        return result

    # x402 payment was verified in middleware. Settlement happens after this
    # returns, and only when the response is a usable success.
    if getattr(request.state, "x402_payment_valid", False):
        result = _hide_page_context(await _scrape(body.url, body.js_render))
        result["auth_method"] = "x402"
        return result

    # No valid auth
    return payment_required_response(SCRAPE_REQUIREMENTS, SCRAPE_RESOURCE, SCRAPE_BAZAAR_EXT)

@app.post("/extract/property")
async def extract_property(body: PropertyExtractRequest, request: Request):
    """Extract structured commercial property data from a listing URL.
    Fixed schema. No fields parameter is needed. Returns normalised property
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

        _arm_credit_charge(request, api_key, key_info, PROPERTY_CREDITS)
        scraped = await _scrape(body.url, body.js_render)
        result = await _extract_property(
            scraped["content"],
            body.url,
            scraped["js_rendered"],
            page_context=scraped.get("page_context") or "",
        )
        _with_render_meta(result, scraped)
        result["auth_method"] = "api_key"
        return result

    # x402 payment was verified in middleware and is settled only on success.
    if getattr(request.state, "x402_payment_valid", False):
        scraped = await _scrape(body.url, body.js_render)
        result = await _extract_property(
            scraped["content"],
            body.url,
            scraped["js_rendered"],
            page_context=scraped.get("page_context") or "",
        )
        _with_render_meta(result, scraped)
        result["auth_method"] = "x402"
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

        _arm_credit_charge(request, api_key, key_info, EXTRACT_CREDITS)
        scraped = await _scrape(body.url, body.js_render)
        result = await _extract_structured(
            scraped["content"],
            body.url,
            body.fields,
            scraped["js_rendered"],
            page_context=scraped.get("page_context") or "",
        )
        _with_render_meta(result, scraped)
        result["auth_method"] = "api_key"
        return result

    # x402 payment was verified in middleware and is settled only on success.
    if getattr(request.state, "x402_payment_valid", False):
        scraped = await _scrape(body.url, body.js_render)
        result = await _extract_structured(
            scraped["content"],
            body.url,
            body.fields,
            scraped["js_rendered"],
            page_context=scraped.get("page_context") or "",
        )
        _with_render_meta(result, scraped)
        result["auth_method"] = "x402"
        return result

    # No valid auth
    return payment_required_response(EXTRACT_REQUIREMENTS, EXTRACT_RESOURCE, EXTRACT_BAZAAR_EXT)

@app.get("/v1/uk/tenders")
async def uk_tenders(
    request: Request,
    q: Optional[str] = None,
    region: Optional[str] = None,
    updated_from: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 25,
    cursor: Optional[str] = None,
):
    """Search UK public tender notices from Find a Tender and Contracts Finder.

    $0.02 USDC per search, up to 25 results. Notice data is published under the
    Open Government Licence v3.0. Contact names, emails, and telephone numbers
    are omitted. Pass next_cursor back with the same q, region, updated_from,
    and stage to read the next page of a cached search.
    """
    auth_method, api_key = await get_auth_method(request)
    paid_x402 = getattr(request.state, "x402_payment_valid", False)

    key_info = None
    if auth_method == "api_key":
        key_info = validate_api_key(api_key)
        if not key_info:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if "error" in key_info:
            raise HTTPException(status_code=401, detail=key_info["error"])
        if key_info["credits_remaining"] < TENDER_CREDITS:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Insufficient credits",
                    "credits_remaining": key_info["credits_remaining"],
                    "credits_required": TENDER_CREDITS,
                },
            )
        _arm_credit_charge(request, api_key, key_info, TENDER_CREDITS)
    elif not paid_x402:
        return payment_required_response(
            TENDER_REQUIREMENTS, TENDER_RESOURCE, TENDER_BAZAAR_EXT
        )

    try:
        result = await tender_service.search(
            q=q,
            region=region,
            updated_from=updated_from,
            stage=stage,
            limit=limit,
            cursor=cursor,
        )
    except TenderQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TenderUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if auth_method == "api_key":
        result["auth_method"] = "api_key"
        return result

    result["auth_method"] = "x402"
    return result


@app.get("/v1/uk/grants")
async def uk_grants(
    q: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 25,
):
    """Free v1 placeholder. Innovate UK competition text is not republished."""
    payload = grants_stub()
    payload["query"] = {"q": q, "status": status, "limit": limit}
    return payload


@app.get("/health")
async def health():
    """Health check endpoint"""
    facilitators = [XPAY_FACILITATOR]
    if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
        facilitators.append(CDP_FACILITATOR)

    return {
        "status": "ok",
        "version": APP_VERSION,
        "facilitators": facilitators,
        "auth_methods": ["x402", "api_key"]
    }

@app.get("/")
async def root():
    """Root endpoint - redirects to docs"""
    return {
        "service": "TerraDeed Scrape API",
        "version": APP_VERSION,
        "documentation": "https://www.terradeed.co.uk/api",
        "endpoints": {
            "scrape": {"path": "/scrape", "method": "POST", "price": SCRAPE_PRICE, "auth": ["x402", "api_key"]},
            "extract": {"path": "/extract", "method": "POST", "price": EXTRACT_PRICE, "auth": ["x402", "api_key"]},
            "extract_property": {"path": "/extract/property", "method": "POST", "price": PROPERTY_PRICE, "auth": ["x402", "api_key"], "description": "UK commercial property intelligence. Attempts enrichment from government open data where available."},
            "uk_tenders": {"path": "/v1/uk/tenders", "method": "GET", "price": TENDER_PRICE, "auth": ["x402", "api_key"], "description": "UK public tender notices from Find a Tender and Contracts Finder. OGL v3.0. Up to 25 results."},
            "uk_grants": {"path": "/v1/uk/grants", "method": "GET", "price": "free", "auth": [], "description": "v1 placeholder. Innovate UK text is not republished yet."},
            "health": {"path": "/health", "method": "GET"}
        }
    }

# llms.txt — agent discovery (served at both /llms.txt and /.well-known/llms.txt)
LLMS_TXT = """# TerraDeed Scrape API

> x402-powered web data extraction for AI agents. Three endpoints that turn any URL into usable intelligence:
> 
> • `/scrape` — Clean, LLM-ready markdown from any URL. JavaScript rendering included. Feed web content directly into agent context without parsing HTML. $0.01 USDC.
> • `/extract` — Structured JSON from any webpage. Name the fields you want — company data, job listings, product specs, contact details, financial figures — and get them back. No scraper configuration needed. $0.05 USDC.
> • `/extract/property` — UK commercial property and land intelligence from any listing URL. Structured data: address, price, site area, use class, tenure, frontage, coordinates. Attempts enrichment from Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data where available. $0.10 USDC.
> • `/v1/uk/tenders` — UK public tender notices from the Find a Tender and Contracts Finder OCDS APIs. Keyword, region, stage, and updated-from filters. Up to 25 results. Open Government Licence v3.0. Contact names, emails, and phone numbers are omitted. $0.02 USDC.
> 
> No API keys, no accounts, no subscriptions — payment is per-request via the x402 protocol (HTTP 402) with USDC on Base mainnet. First byte to paid response in one retry cycle.

## What this API does

- **Scrape** (`POST /scrape`, $0.01 USDC) — Clean, LLM-ready markdown from any URL. JavaScript rendering included. Feed web content directly into agent context without parsing HTML.
- **Extract** (`POST /extract`, $0.05 USDC) — Structured JSON from any webpage. Name the fields you want — company data, job listings, product specs, contact details, financial figures — and get them back. No scraper configuration needed.
- **Extract Property** (`POST /extract/property`, $0.10 USDC) — UK commercial property and land intelligence from any listing URL. Attempts enrichment from Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data where available. Site acquisition teams use this for initial screening.

- You need the readable content of a web page as markdown for summarisation, RAG ingestion, or analysis → `POST /scrape` ($0.01)
- You need specific named fields from a page as machine-usable JSON (prices, titles, contact details, specs, team structures) → `POST /extract` ($0.05)
- You need structured UK commercial property intelligence with government data enrichment (flood risk, listed buildings, EPC) from any listing → `POST /extract/property` ($0.10)
- The page requires JavaScript rendering → add `"js_render": true` to any endpoint. If rendering fails, the API retries once and then falls back to a static fetch. The response sets `js_render_fallback` to true and includes `fallback_reason` instead of returning an empty HTTP 500.
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

Structured JSON from any webpage. Name the fields you want — company data, job listings, product specs, contact details, financial figures — and get them back. No scraper configuration needed.

Request body (JSON):

    {"url": "https://savills.co.uk/commercial-property-for-sale/unit-5-bristol-road-bs1-4na", "fields": ["company_name", "services", "team_size", "location", "phone", "website", "specialisms"]}

- `url` (string, required): public URL to extract from
- `fields` (array of strings, required, min 1): field names to extract. Use descriptive names — "price_per_month" beats "p1"
- `js_render` (boolean, default false). On renderer failure the call still returns 200 from the static page, with `js_render_fallback: true` and a short `fallback_reason`.

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

UK commercial property and land intelligence from any listing URL. Structured data: address, price, site area, use class, tenure, frontage, coordinates. Attempts enrichment from Environment Agency flood risk, Historic England listed buildings, and DLUHC EPC data where available.

Request body (JSON):

    {"url": "https://www.rightmove.co.uk/commercial-property-for-sale/property-12345.html"}

- `url` (string, required): public URL of a UK commercial property listing
- `js_render` (boolean, default true): property portals require JavaScript. Renderer failure falls back to a static fetch and sets `js_render_fallback: true`.

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

### GET /v1/uk/tenders — $0.02 USDC

UK public tender notices from Find a Tender and Contracts Finder. One search returns up to 25 notices.

Query parameters:

- `q` (string): case-insensitive words matched against title, buyer, and description. The description itself is not returned.
- `region` (string, default `uk`): `uk`, `england`, `scotland`, `wales`, `northern-ireland`, `north-west`, `north-east`, `yorkshire-and-the-humber`, `east-midlands`, `west-midlands`, `east-of-england`, `london`, `south-east`, `south-west`. Region hints are approximate (NUTS code, region name, or postcode area).
- `updated_from` (string): `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`. Defaults to 30 days before the search.
- `stage` (string): `planning`, `tender`, or `award`.
- `limit` (integer, default 25, max 25).
- `cursor` (string): `next_cursor` from the previous response. Repeat the same `q`, `region`, `updated_from`, and `stage`.

The response includes `licence` `OGL-UK-3.0` and the attribution line required for Open Government Licence reuse. Contact names, emails, and telephone numbers are removed.

`GET /v1/uk/grants` is free and currently returns no competition text.

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

def usd_amount_from_atomic(atomic: str) -> str:
    """Decimal USD for discovery documents. Runtime x402 amounts stay atomic."""
    minor = int(atomic)
    scale = 10 ** USDC_DECIMALS
    whole, frac = divmod(minor, scale)
    return f"{whole}.{frac:0{USDC_DECIMALS}d}"


def discovery_catalog() -> list[dict]:
    """Paid routes, prices, and 402 requirements. Discovery documents share this list."""
    return [
        {
            "method": "POST",
            "path": "/scrape",
            "price_label": SCRAPE_PRICE,
            "requirements": SCRAPE_REQUIREMENTS,
            "summary": "Clean markdown from a public URL",
            "price_note": f"**Price:** {SCRAPE_PRICE} USDC per call.",
            "output_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "url": {"type": "string"},
                    "status": {"type": "string"},
                    "word_count": {"type": "integer"},
                    "title": {"type": ["string", "null"]},
                    "js_rendered": {"type": "boolean"},
                    "auth_method": {"type": "string"},
                },
                "required": ["content", "url", "status"],
            },
        },
        {
            "method": "POST",
            "path": "/extract",
            "price_label": EXTRACT_PRICE,
            "requirements": EXTRACT_REQUIREMENTS,
            "summary": "Named JSON fields from a public URL",
            "price_note": f"**Price:** {EXTRACT_PRICE} USDC per call.",
            "output_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "status": {"type": "string"},
                    "data": {"type": "object"},
                    "fields_requested": {"type": "array", "items": {"type": "string"}},
                    "fields_extracted": {"type": "array", "items": {"type": "string"}},
                    "auth_method": {"type": "string"},
                },
                "required": ["url", "status", "data"],
            },
        },
        {
            "method": "POST",
            "path": "/extract/property",
            "price_label": PROPERTY_PRICE,
            "requirements": PROPERTY_REQUIREMENTS,
            "summary": "UK commercial property data from a listing URL",
            "price_note": f"**Price:** {PROPERTY_PRICE} USDC per call.",
            "output_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "status": {"type": "string"},
                    "listing_type": {"type": ["string", "null"]},
                    "property": {"type": "object"},
                    "vendor": {"type": "object"},
                    "auth_method": {"type": "string"},
                },
                "required": ["url", "status", "property"],
            },
        },
        {
            "method": "GET",
            "path": "/v1/uk/tenders",
            "price_label": TENDER_PRICE,
            "requirements": TENDER_REQUIREMENTS,
            "summary": "UK public tender notices, up to 25 results",
            "price_note": f"**Price:** {TENDER_PRICE} USDC per search (up to 25 results).",
            "output_schema": {
                "type": "object",
                "properties": {
                    "licence": {"type": "string"},
                    "attribution": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "next_cursor": {"type": ["string", "null"]},
                    "auth_method": {"type": "string"},
                },
                "required": ["licence", "results"],
            },
        },
    ]


def _payment_info(requirements: dict) -> dict:
    return {
        "price": {
            "mode": "fixed",
            "currency": "USD",
            "amount": usd_amount_from_atomic(requirements["amount"]),
        },
        "protocols": [{"x402": {}}],
    }


def _accepts_entry(requirements: dict) -> dict:
    """Same fields the 402 challenge puts in accepts[]. Amount stays atomic."""
    return {
        "scheme": requirements["scheme"],
        "network": requirements["network"],
        "asset": requirements["asset"],
        "amount": requirements["amount"],
        "payTo": requirements["payTo"],
        "maxTimeoutSeconds": requirements["maxTimeoutSeconds"],
        "extra": requirements["extra"],
    }


def well_known_x402_document() -> dict:
    """x402 v2 capability manifest for /.well-known/x402."""
    resources = []
    for route in discovery_catalog():
        resources.append({
            "url": f"{BASE_URL}{route['path']}",
            "path": route["path"],
            "method": route["method"],
            "description": route["summary"],
            "price": _payment_info(route["requirements"])["price"],
            "accepts": [_accepts_entry(route["requirements"])],
        })
    return {
        "x402Version": 2,
        "kind": "resource-server",
        "name": API_TITLE,
        "description": api_description(),
        "resources": resources,
        "openapi": f"{BASE_URL}/openapi.json",
        "docs": f"{BASE_URL}/docs",
        "contact": "contact@terradeed.co.uk",
    }


@app.get("/.well-known/x402")
async def well_known_x402():
    """x402 v2 capability manifest. Lists the same paid routes as /openapi.json."""
    return JSONResponse(
        content=well_known_x402_document(),
        headers={"Cache-Control": "public, max-age=300"},
    )


_OPENAPI_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}


def custom_openapi():
    """OpenAPI document for x402scan: payment info, 402 responses, free routes marked."""
    if app.openapi_schema:
        return app.openapi_schema
    app.description = api_description()
    openapi_schema = get_openapi(
        title=API_TITLE,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    info = openapi_schema.setdefault("info", {})
    info["title"] = API_TITLE
    info["description"] = app.description
    info["x-guidance"] = api_guidance()
    info["contact"] = {
        "name": "Terradeed",
        "email": "contact@terradeed.co.uk",
        "url": "https://terradeed.co.uk",
    }
    components = openapi_schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes["x402"] = {
        "type": "apiKey",
        "in": "header",
        "name": "PAYMENT-SIGNATURE",
        "description": "x402 v2 payment signature. USDC on Base mainnet (eip155:8453).",
    }
    schemes["apiKey"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "API key",
        "description": "Terradeed API key. Credits are deducted only after a usable success.",
    }

    catalog = {(route["method"], route["path"]): route for route in discovery_catalog()}
    for path, methods in openapi_schema.get("paths", {}).items():
        for method, operation in methods.items():
            if method not in _OPENAPI_METHODS or not isinstance(operation, dict):
                continue
            route = catalog.get((method.upper(), path))
            if route is None:
                operation["security"] = []
                continue
            operation["summary"] = route["summary"]
            operation["x-payment-info"] = _payment_info(route["requirements"])
            operation["security"] = [{"x402": []}, {"apiKey": []}]
            description = operation.get("description") or ""
            note = route["price_note"]
            if note not in description:
                operation["description"] = f"{description}\n\n{note}".strip()
            responses = operation.setdefault("responses", {})
            responses["402"] = {"description": "Payment Required"}
            success = responses.setdefault("200", {"description": "Successful response"})
            content = success.setdefault("content", {}).setdefault("application/json", {})
            schema = content.get("schema")
            if not schema or schema == {}:
                content["schema"] = route["output_schema"]

    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
