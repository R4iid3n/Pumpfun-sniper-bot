"""
Test different Pump.fun API endpoints to find which one works
"""
import requests
import json
import time

# Known pump.fun token for testing
TEST_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

# List of potential API endpoints
API_ENDPOINTS = [
    "https://frontend-api-v3.pump.fun/coins/",
    "https://frontend-api.pump.fun/coins/",
    "https://api.pump.fun/coins/",
    "https://client-api.pump.fun/coins/",
    "https://pumpportal.fun/api/data/",
    "https://api.pumpportal.fun/coins/",
    "https://pumpapi.fun/api/coins/",
]

print("=" * 70)
print("TESTING PUMP.FUN API ENDPOINTS")
print("=" * 70)
print(f"\nTest Token: {TEST_MINT}\n")

working_endpoints = []
failed_endpoints = []

for i, base_url in enumerate(API_ENDPOINTS, 1):
    url = f"{base_url}{TEST_MINT}"
    print(f"\n[{i}/{len(API_ENDPOINTS)}] Testing: {base_url}")
    print("-" * 70)

    try:
        # Try with different headers
        headers_list = [
            # Standard browser headers
            {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            # Minimal headers
            {
                'User-Agent': 'Mozilla/5.0',
            },
            # No headers
            {}
        ]

        success = False
        for header_idx, headers in enumerate(headers_list):
            try:
                response = requests.get(url, headers=headers, timeout=8)

                print(f"  Header variant {header_idx + 1}: Status {response.status_code}")

                if response.status_code == 200:
                    content_type = response.headers.get('content-type', '')
                    content_length = len(response.text)

                    print(f"    Content-Type: {content_type}")
                    print(f"    Content-Length: {content_length} bytes")

                    # Check if it's actually JSON with data
                    if content_length > 10:
                        try:
                            data = response.json()
                            print(f"    JSON Keys: {list(data.keys())[:5]}")

                            # Check if it has expected fields
                            if 'mint' in data or 'symbol' in data or 'name' in data:
                                print(f"    SUCCESS - Valid token data found!")
                                print(f"      Symbol: {data.get('symbol', 'N/A')}")
                                print(f"      Name: {data.get('name', 'N/A')}")
                                working_endpoints.append({
                                    'url': base_url,
                                    'headers': headers,
                                    'data_sample': data
                                })
                                success = True
                                break
                            else:
                                print(f"    PARTIAL - JSON but missing token fields")
                        except json.JSONDecodeError:
                            print(f"    FAILED - Not valid JSON")
                            if content_length < 500:
                                print(f"    Response: {response.text[:200]}")
                    else:
                        print(f"    FAILED - Empty response (Cloudflare block?)")

                elif response.status_code == 403:
                    print(f"    BLOCKED - 403 Forbidden (Cloudflare?)")
                elif response.status_code == 404:
                    print(f"    NOT FOUND - 404 (Wrong endpoint)")
                else:
                    print(f"    ERROR - Unexpected status code")

            except requests.exceptions.Timeout:
                print(f"  Header variant {header_idx + 1}: TIMEOUT")
            except requests.exceptions.ConnectionError:
                print(f"  Header variant {header_idx + 1}: CONNECTION ERROR")
            except Exception as e:
                print(f"  Header variant {header_idx + 1}: ERROR - {str(e)[:50]}")

        if not success:
            failed_endpoints.append(base_url)

    except Exception as e:
        print(f"  CRITICAL ERROR: {str(e)}")
        failed_endpoints.append(base_url)

    # Small delay to avoid rate limiting
    time.sleep(0.5)

# Summary
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

if working_endpoints:
    print(f"\nWORKING ENDPOINTS ({len(working_endpoints)}):")
    for endpoint in working_endpoints:
        print(f"\n  URL: {endpoint['url']}")
        print(f"  Headers needed: {len(endpoint['headers'])} keys")
        if endpoint['headers']:
            print(f"    {list(endpoint['headers'].keys())}")
        print(f"  Sample data keys: {list(endpoint['data_sample'].keys())[:10]}")
else:
    print("\nNO WORKING ENDPOINTS FOUND")
    print("\nPossible reasons:")
    print("  1. All pump.fun APIs are behind Cloudflare protection")
    print("  2. Rate limiting is active")
    print("  3. Test token doesn't exist anymore")
    print("  4. Network/firewall blocking requests")

print(f"\nFailed endpoints: {len(failed_endpoints)}/{len(API_ENDPOINTS)}")

if working_endpoints:
    print("\n" + "=" * 70)
    print("RECOMMENDED CONFIGURATION")
    print("=" * 70)
    best = working_endpoints[0]
    print(f"\nUpdate pumpfun_bot.py fetch_coin_data_direct() to use:")
    print(f"  URL: {best['url']}")
    if best['headers']:
        print(f"  Headers: {json.dumps(best['headers'], indent=2)}")
