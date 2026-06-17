# TerraDeed API — Quick Reference

## Test Key (Immediate Use)
```
td_sk_test_terradeed_2026
```

## Test Commands

### Scrape (1 credit)
```bash
curl -X POST http://localhost:8080/scrape \
  -H "Authorization: Bearer td_sk_test_terradeed_2026" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

### Extract (5 credits)
```bash
curl -X POST http://localhost:8080/extract \
  -H "Authorization: Bearer td_sk_test_terradeed_2026" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "fields": ["title", "description"]}'
```

### Check Health
```bash
curl http://localhost:8080/health
```

### Test Key Status
```bash
curl "http://localhost:8080/test-key?api_key=td_sk_test_terradeed_2026"
```

## Admin Commands (requires ADMIN_SECRET)

### Create New Key
```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"admin_secret": "terradeed-admin-2026", "credits": 500}'
```

### List All Keys
```bash
curl "http://localhost:8080/admin/keys?admin_secret=terradeed-admin-2026"
```

### Add Credits
```bash
curl -X POST "http://localhost:8080/admin/keys/td_sk_tes.../add-credits?credits=1000&admin_secret=terradeed-admin-2026"
```

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
