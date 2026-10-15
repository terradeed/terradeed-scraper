# Design Note: Conditional Settlement for /extract/property

## Problem

/extract/property performs two expensive operations before enrichment is attempted:

1. Scraping the target listing URL (often requiring JavaScript rendering)
2. Structured extraction via Anthropic Claude

If all enrichment sources subsequently fail (flood risk API down, EPC key missing, Historic England returning empty geometries), the user has still received the core scraped and extracted property data. The question is whether to settle the x402 payment.

## Recommended Approach

**Do not settle the x402 payment when all enrichment sources fail.**

The x402 Python SDK provides `verify_payment` and `settle_payment` hooks on the resource server. Use the **`settle_payment`** hook as the conditional gate:

1. Call `x402_server.verify_payment(payload, requirements)` in middleware (as currently done).
2. After `_extract_property()` completes, inspect `result["enrichment"]["enrichment_status"]`.
3. Only if `enrichment_status` is `"complete"` or `"partial"` (i.e. at least one enrichment source succeeded), call `x402_server.settle_payment(payload, requirements)`.
4. If `enrichment_status` is `"failed"` or `"error"`, skip settlement and return the result with a `partial_refund_eligible` flag. The payment authorisation remains valid but unspent; the client can retry or request a refund via facilitator support.

## Why settle_payment is the right hook

- `verify_payment` validates the payment authorisation and checks the payer has sufficient funds. It must run before any expensive work so the client cannot spam unpaid requests.
- `settle_payment` moves funds on-chain. This is the natural place to gate on service-quality criteria because it runs *after* the work is complete.

## SDK Reference

- x402 Python SDK source: https://github.com/coinbase/x402/blob/main/python/packages/x402/x402/server/server.py
- `settle_payment` signature:
  ```python
  async def settle_payment(
      self,
      payload: PaymentPayload,
      requirements: PaymentRequirements,
  ) -> dict:
  ```

## API-Key Variant

For API-key auth, deduct the full credit amount only when enrichment succeeds. When all sources fail, deduct a reduced "base scrape" amount (e.g. 5 credits instead of 10) and log the partial charge for review.

## Monitoring

Alert when the partial-refund / no-settle rate exceeds 5% in any 24-hour window. Sustained high rates indicate an upstream API outage that needs operator attention.
