#!/bin/bash
#
# Deploy TerraDeed Scrape API with API key support to Railway
#

set -e

echo "🚀 TerraDeed Scrape API Deployment Script"
echo "=========================================="

# Check for Railway CLI
if ! command -v railway &> /dev/null; then
    echo "❌ Railway CLI not found. Install with: npm install -g @railway/cli"
    exit 1
fi

# Check login status
if ! railway whoami &> /dev/null; then
    echo "❌ Not logged into Railway. Run: railway login"
    exit 1
fi

echo ""
echo "📝 Setting environment variables..."

# Generate a secure admin secret if not set
ADMIN_SECRET=$(openssl rand -hex 32 2>/dev/null || head /dev/urandom | tr -dc A-Za-z0-9 | head -c 64)

# Set required environment variables
echo "Setting ADMIN_SECRET..."
railway variables set ADMIN_SECRET="$ADMIN_SECRET"

echo "Setting DATABASE_URL..."
railway variables set DATABASE_URL="sqlite:///app/terradeed.db"

echo ""
echo "🔄 Current environment variables:"
railway variables

echo ""
echo "📦 Deploying to Railway..."
railway up

echo ""
echo "✅ Deployment complete!"
echo ""
echo "🔑 Admin Secret (save this): $ADMIN_SECRET"
echo ""
echo "🧪 Test the deployment:"
echo "  curl https://api.terradeed.co.uk/health"
echo ""
echo "📋 Next steps:"
echo "  1. Test the API: python test_api_keys.py"
echo "  2. Create a production key: POST /admin/keys"
echo "  3. Update the landing page with API key pricing"
echo ""
