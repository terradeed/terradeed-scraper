# TerraDeed API — Quick Reference

## Test Key
Set `TERRADEED_TEST_KEY` environment variable to a key created via the admin endpoint.

## Test Commands

### Scrape (1 credit)
```bash
curl -X POST http://localhost:8080/scrape \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

### Extract (5 credits)
```bash
curl -X POST http://localhost:8080/extract \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "fields": ["title", "description"]}'
```

### Check Health
```bash
curl http://localhost:8080/health
```

## Admin Commands (requires ADMIN_SECRET)

### Create New Key
```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Content-Type: application/json" \
  -H "x-admin-secret: <ADMIN_SECRET>" \
  -d '{"credits": 500}'
```

### Get Key Status
```bash
curl -H "x-admin-secret: <ADMIN_SECRET>" \
  "http://localhost:8080/admin/keys/<KEY_PREFIX>"
```

> **Note:** `add-credits`, `revoke`, and `list-all` endpoints are not yet implemented.

## Deploy to Railway

```bash
./deploy.sh
```

Or manually:
```bash
railway variables set ADMIN_SECRET="$(openssl rand -hex 32)"
railway variables set DATABASE_URL="sqlite:///app/terradeed.db"
railway up
```

## Pricing Quick Reference

| Call Type | Credits | Value |
|-----------|---------|-------|
| /scrape | 1 | ~$0.01 |
| /extract | 5 | ~$0.05 |

| Pack | Credits | Price | Discount |
|------|---------|-------|----------|
| Starter | 2,000 | $20 | — |
| Pro | 6,000 | $50 | 17% |

---

**Version:** 0.7.0  
**API Base:** https://api.terradeed.co.uk
