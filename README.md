# TerraDeed Scrape API

Pay-per-use web scraping and structured data extraction for AI agents. No API keys. No subscriptions. Agents pay and go.

**Base mainnet · x402 · USDC**

---

## Endpoints

| Endpoint | Price | Output |
|----------|-------|--------|
| `POST /scrape` | $0.01 USDC | Clean LLM-ready markdown from any URL |
| `POST /extract` | $0.05 USDC | Schema-driven structured JSON — pass fields, receive typed values |

Both endpoints support static scraping and JS rendering via Playwright for SPAs and dynamic sites.

---

## Payment

This API uses the [x402 protocol](https://x402.org) for autonomous micropayments. Your client sends a standard HTTP request, receives a `402 Payment Required` response with payment details, pays $0.01–$0.05 USDC on Base mainnet, and retries. The x402 SDK handles this automatically.

- **Network:** Base mainnet (`eip155:8453`)
- **Asset:** USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- **Facilitator:** `https://facilitator.xpay.sh`
- **Payment wallet:** `0x4E024e356bd01853654b7B5196F2B85F67Cc39EC`

---

## Quick Start — Python

Install the x402 SDK:

```bash
pip install "x402[evm]"
pip install eth-account
```

Make a paid scrape call:

```python
import asyncio
from eth_account import Account
from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client

PRIVATE_KEY = "your_wallet_private_key"  # Wallet funded with USDC on Base mainnet

async def main():
    account = Account.from_key(PRIVATE_KEY)
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account))

    async with x402HttpxClient(client) as http:
        # Scrape — returns LLM-ready markdown
        response = await http.post(
            "https://api.terradeed.co.uk/scrape",
            json={"url": "https://example.com", "js_render": False}
        )
        print(response.json())

asyncio.run(main())
```

Example response:

```json
{
  "content": "## Example Domain\n\nThis domain is for use in documentation examples...",
  "url": "https://example.com",
  "status": "success",
  "word_count": 17,
  "title": "Example Domain",
  "js_rendered": false
}
```

---

## Structured Extraction

Extract specific fields from any URL as clean JSON:

```python
async with x402HttpxClient(client) as http:
    response = await http.post(
        "https://api.terradeed.co.uk/extract",
        json={
            "url": "https://example.com/product",
            "fields": ["price", "title", "availability", "description"],
            "js_render": False
        }
    )
    print(response.json())
```

Example response:

```json
{
  "url": "https://example.com/product",
  "status": "success",
  "data": {
    "price": "$29.99",
    "title": "Example Product",
    "availability": "In stock",
    "description": "A great product for everyday use."
  },
  "fields_requested": ["price", "title", "availability", "description"],
  "fields_extracted": ["price", "title", "availability", "description"],
  "js_rendered": false,
  "model": "claude-sonnet-4-20250514"
}
```

---

## Request Reference

### POST /scrape

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | ✓ | URL to scrape |
| `js_render` | boolean | | Use Playwright for JS-rendered sites. Default: `false` |

### POST /extract

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | ✓ | URL to extract from |
| `fields` | list[string] | ✓ | Field names to extract. Max 20. |
| `js_render` | boolean | | Use Playwright for JS-rendered sites. Default: `false` |

---

## JS Rendering

Set `js_render: true` for:
- Single-page applications (React, Vue, Svelte, Angular)
- Sites with lazy-loaded content or infinite scroll
- AJAX-driven feeds and dynamic grids
- Web3 interfaces with wallet detection

The API automatically falls back to Playwright if static extraction returns empty content.

---

## Discovery

```
GET https://api.terradeed.co.uk/.well-known/x402   # x402 v2 discovery
GET https://api.terradeed.co.uk/bazaar.json         # Agentic Market manifest
GET https://api.terradeed.co.uk/health              # Health and capabilities
GET https://api.terradeed.co.uk/docs                # Interactive API docs
```

Indexed in the [Agentic Market catalogue](https://onyx-actions.onrender.com/bazaar) — ID 58838. Discoverable via `mapper-mcp search_endpoints` with `terradeed` from any MCP client.

---

## Validation

- Strict-v2 spec-valid (verified by [mapper-mcp](https://github.com/TomSmart-ai))
- 402 response includes full `accepts` array per x402 v2 spec
- Catalogue: 6/6 transport, 8/8 payment, 5/5 extension score

---

## Stack

- Python 3.11 / FastAPI
- x402 SDK 2.9.0
- Playwright / Chromium (JS rendering)
- trafilatura (content extraction)
- Claude Sonnet (structured extraction)
- Hosted on Railway — Base mainnet

---

## Links

- **API:** https://api.terradeed.co.uk
- **Docs:** https://api.terradeed.co.uk/docs
- **Website:** https://terradeed.co.uk
- **X:** [@TerraDeed](https://x.com/TerraDeed)
- **Contact:** contact@terradeed.co.uk
# Force rebuild
