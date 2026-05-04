"""
test_payment.py — make a real x402 payment to POST /scrape

Reads your private key from the PRIVATE_KEY environment variable.
Never paste your key into this file.

Run:
    python test_payment.py
"""

import asyncio
import os

import httpx
from eth_account import Account

from x402.client import x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm.exact import register_exact_evm_client

# ── Config ────────────────────────────────────────────────────────────────────

SERVER_URL = "http://localhost:4021"
TARGET_URL = "https://example.com"        # The URL we're asking the server to scrape

# Read private key from environment — never hardcode this
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
if not PRIVATE_KEY:
    raise SystemExit("❌  Set your private key first:\n    export PRIVATE_KEY=0x...")

# ── Payment client setup ──────────────────────────────────────────────────────

# Create a LocalAccount from your private key
account = Account.from_key(PRIVATE_KEY)
print(f"💳  Paying from: {account.address}")

# Build x402 client and register the EVM exact scheme
# register_exact_evm_client accepts a LocalAccount directly
x402_client = x402Client()
register_exact_evm_client(x402_client, account)

# ── Make the paid request ─────────────────────────────────────────────────────

async def main():
    # x402AsyncTransport wraps httpx and handles the full payment flow:
    # 1. Makes initial request → receives 402
    # 2. Signs the payment authorization (EIP-3009)
    # 3. Retries with X-Payment header
    # 4. Returns the real 200 response
    transport = x402AsyncTransport(x402_client)

    async with httpx.AsyncClient(transport=transport) as client:
        print(f"\n🚀  Sending POST {SERVER_URL}/scrape ...")
        print(f"    url = {TARGET_URL}\n")

        response = await client.post(
            f"{SERVER_URL}/scrape",
            json={"url": TARGET_URL},
        )

        print(f"✅  Status: {response.status_code}")
        print(f"\n📄  Response:")
        print(response.json())

asyncio.run(main())
