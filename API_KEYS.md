# TerraDeed Scrape API — API Key Authentication

**New in v0.7.0**: You can now use prepaid API keys to access `/scrape` and `/extract` endpoints without setting up USDC payments.

---

## Quick Start

### 1. Get an API Key

Contact [contact@terradeed.co.uk](mailto:contact@terradeed.co.uk) to purchase prepaid credits, or use the test key for development:

```
td_sk_test_terradeed_2026
```

*(Test key has 1000 credits, rate limited to 60/min)*

### 2. Make Requests

#### Scraping (1 credit)

```bash
curl -X POST https://api.terradeed.co.uk/scrape \
  -H "Authorization: Bearer td_sk_test_terradeed_2026" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "js_render": false}'
```

**Response:**
```json
{
  "content": "# Example Domain\n\nThis domain is for use in examples...",
  "url": "https://example.com",
  "status": "success",
  "word_count": 42,
  "title": "Example Domain",
  "js_rendered": false,
  "auth_method": "api_key",
  "credits_remaining": 999
}
```

#### Structured Extraction (5 credits)

```bash
curl -X POST https://api.terradeed.co.uk/extract \
  -H "Authorization: Bearer td_sk_test_terradeed_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/product",
    "fields": ["title", "price", "availability"],
    "js_render": false
  }'
```

**Response:**
```json
{
  "url": "https://example.com/product",
  "status": "success",
  "data": {
    "title": "Example Product",
    "price": "$19.99",
    "availability": "In stock"
  },
  "fields_requested": ["title", "price", "availability"],
  "fields_extracted": ["title", "price", "availability"],
  "js_rendered": false,
  "model": "claude-sonnet-4-20250514",
  "auth_method": "api_key",
  "credits_remaining": 994
}
```

---

## Pricing

| Endpoint | Credits | Approximate Value |
|----------|---------|-------------------|
| `/scrape` | 1 | ~$0.01 |
| `/extract` | 5 | ~$0.05 |

**Credit Packs:**
- $20 = 2,000 credits
- $50 = 6,000 credits (20% bonus)
- Custom enterprise tiers available

---

## Authentication Methods (Parallel)

Both methods work on the same endpoints:

### Option 1: API Key (Prepaid Credits)
```
Authorization: Bearer <your-api-key>
```

### Option 2: x402 USDC Micropayment
```
Payment-Signature: <base64-encoded-x402-payment>
```

If both headers are present, API key takes precedence.

---

## Response Codes

| Code | Meaning |
|------|---------|
| 200 | Success — request processed, credits deducted |
| 401 | Invalid API key |
| 402 | Insufficient credits (response includes `credits_required` and `top_up_url`) |
| 403 | Key revoked |
| 429 | Rate limit exceeded |

---

## Rate Limits

Default: 60 requests/minute per key

Contact us for higher limits on enterprise tiers.

---

## Testing

Use the test key for development:

```python
import requests

API_KEY = "td_sk_test_terradeed_2026"
response = requests.post(
    "https://api.terradeed.co.uk/scrape",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={"url": "https://example.com"}
)
print(response.json())
```

---

## JavaScript Example

```javascript
const response = await fetch('https://api.terradeed.co.uk/scrape', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer td_sk_test_terradeed_2026',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    url: 'https://example.com',
    js_render: false
  })
});

const data = await response.json();
console.log(`Credits remaining: ${data.credits_remaining}`);
```

---

## Python Example

```python
import requests

API_KEY = "td_sk_test_terradeed_2026"

def scrape(url, js_render=False):
    response = requests.post(
        "https://api.terradeed.co.uk/scrape",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"url": url, "js_render": js_render}
    )
    response.raise_for_status()
    return response.json()

result = scrape("https://example.com")
print(result["content"])
```

---

## Migrating from x402 to API Keys

If you're already using x402, you can switch to API keys by simply:

1. Replacing the `Payment-Signature` header with `Authorization: Bearer *** 2. Removing the payment negotiation flow

The endpoints, request bodies, and response formats are identical.

---

## Support

- Email: [contact@terradeed.co.uk](mailto:contact@terradeed.co.uk)
- Status: [api.terradeed.co.uk/health](https://api.terradeed.co.uk/health)

---

**Version:** 0.7.0  
**Last Updated:** 17 June 2026
