"""
Quick test script for API key functionality.
Run this after starting the server to verify everything works.
"""

import requests
import json

BASE_URL = "http://localhost:8080"
TEST_KEY = "td_sk_test_terradeed_2026"
ADMIN_SECRET = "terradeed-admin-2026"

def test_health():
    """Test the health endpoint."""
    r = requests.get(f"{BASE_URL}/health")
    print(f"Health: {r.status_code}")
    print(json.dumps(r.json(), indent=2))
    return r.status_code == 200

def test_key_validation():
    """Test the test-key endpoint."""
    r = requests.get(f"{BASE_URL}/test-key", params={"api_key": TEST_KEY})
    print(f"\nKey validation: {r.status_code}")
    print(json.dumps(r.json(), indent=2))
    return r.json().get("valid") == True

def test_scrape_with_key():
    """Test scraping with API key."""
    payload = {"url": "https://example.com", "js_render": False}
    headers = {"Authorization": f"Bearer {TEST_KEY}"}
    
    r = requests.post(f"{BASE_URL}/scrape", json=payload, headers=headers)
    print(f"\nScrape with API key: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Title: {data.get('title')}")
        print(f"  Word count: {data.get('word_count')}")
        print(f"  Auth method: {data.get('auth_method')}")
        print(f"  Credits remaining: {data.get('credits_remaining')}")
        return True
    else:
        print(f"  Error: {r.text}")
        return False

def test_scrape_without_auth():
    """Test scraping without auth (should get 402 with x402 info)."""
    payload = {"url": "https://example.com", "js_render": False}
    
    r = requests.post(f"{BASE_URL}/scrape", json=payload)
    print(f"\nScrape without auth: {r.status_code}")
    if r.status_code == 402:
        data = r.json()
        print(f"  x402 version: {data.get('x402Version')}")
        print(f"  Accepts count: {len(data.get('accepts', []))}")
        return True
    else:
        print(f"  Response: {r.text[:200]}")
        return False

def test_create_key():
    """Test creating a new API key."""
    payload = {
        "admin_secret": ADMIN_SECRET,
        "credits": 500,
        "rate_limit": 120
    }
    
    r = requests.post(f"{BASE_URL}/admin/keys", json=payload)
    print(f"\nCreate new key: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Key: {data.get('api_key')[:30]}...")
        print(f"  Credits: {data.get('credits')}")
        return data.get('api_key')
    else:
        print(f"  Error: {r.text}")
        return None

def test_list_keys():
    """Test listing all keys."""
    r = requests.get(f"{BASE_URL}/admin/keys", params={"admin_secret": ADMIN_SECRET})
    print(f"\nList keys: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Total keys: {data.get('count')}")
        for key in data.get('keys', [])[:3]:
            print(f"    {key['key_prefix']}: {key['credits_remaining']} credits, {key['total_calls']} calls")
        return True
    else:
        print(f"  Error: {r.text}")
        return False

def test_extract_with_key():
    """Test extraction with API key."""
    payload = {
        "url": "https://example.com",
        "fields": ["title", "description"],
        "js_render": False
    }
    headers = {"Authorization": f"Bearer {TEST_KEY}"}
    
    r = requests.post(f"{BASE_URL}/extract", json=payload, headers=headers)
    print(f"\nExtract with API key: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Fields extracted: {data.get('fields_extracted')}")
        print(f"  Data: {json.dumps(data.get('data'), indent=2)[:200]}...")
        print(f"  Credits remaining: {data.get('credits_remaining')}")
        return True
    else:
        print(f"  Error: {r.text[:300]}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("TerraDeed API Key Test Suite")
    print("=" * 60)
    
    try:
        test_health()
        test_key_validation()
        test_scrape_without_auth()
        test_scrape_with_key()
        test_extract_with_key()
        test_list_keys()
        new_key = test_create_key()
        
        if new_key:
            print(f"\n\nNew key created! Test it with:")
            print(f'  curl -H "Authorization: Bearer {new_key}" \\\\')
            print(f'    -X POST {BASE_URL}/scrape \\\\')
            print(f'    -d \'{{"url": "https://example.com"}}\'')
        
        print("\n" + "=" * 60)
        print("All tests completed!")
        print("=" * 60)
        
    except requests.ConnectionError:
        print(f"\n\nERROR: Could not connect to {BASE_URL}")
        print("Make sure the server is running: python main.py")
