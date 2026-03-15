"""
Test PumpPortal API endpoints
"""
import requests
import json

TEST_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

# PumpPortal potential endpoints
endpoints = [
    f"https://pumpportal.fun/api/data/{TEST_MINT}",
    f"https://pumpportal.fun/api/token/{TEST_MINT}",
    f"https://pumpportal.fun/api/coins/{TEST_MINT}",
    f"https://api.pumpportal.fun/data/{TEST_MINT}",
    f"https://api.pumpportal.fun/token/{TEST_MINT}",
    f"https://api.pumpportal.fun/coins/{TEST_MINT}",
]

print("=" * 70)
print("TESTING PUMPPORTAL API")
print("=" * 70)

for url in endpoints:
    print(f"\nTesting: {url}")
    print("-" * 70)

    try:
        response = requests.get(url, timeout=10)
        print(f"Status: {response.status_code}")

        if response.status_code == 200:
            content_type = response.headers.get('content-type', '')
            print(f"Content-Type: {content_type}")
            print(f"Length: {len(response.text)} bytes")

            if len(response.text) > 10:
                try:
                    data = json.loads(response.text)
                    print(f"SUCCESS - Got JSON data!")
                    print(f"Keys: {list(data.keys())[:10]}")
                    if 'symbol' in data:
                        print(f"Symbol: {data['symbol']}")
                    if 'name' in data:
                        print(f"Name: {data['name']}")
                except:
                    print(f"Not JSON - First 200 chars: {response.text[:200]}")
            else:
                print("Empty response")
        else:
            print(f"Failed with status {response.status_code}")

    except requests.exceptions.Timeout:
        print("TIMEOUT")
    except requests.exceptions.ConnectionError:
        print("CONNECTION ERROR")
    except Exception as e:
        print(f"ERROR: {str(e)[:100]}")

print("\n" + "=" * 70)
print("Now testing WebSocket connection...")
print("=" * 70)

# Test WebSocket
import asyncio
import websockets

async def test_websocket():
    try:
        print("\nConnecting to wss://pumpportal.fun/api/data")
        async with websockets.connect('wss://pumpportal.fun/api/data', ping_interval=20) as ws:
            print("CONNECTED!")

            # Subscribe to new tokens
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            print("Subscribed to new tokens")

            print("\nWaiting for new token events (20s timeout)...")
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=20.0)
                data = json.loads(message)
                print(f"\nGOT EVENT!")
                print(f"Keys: {list(data.keys())}")
                print(f"Mint: {data.get('mint', 'N/A')[:20]}...")
                print(f"Symbol: {data.get('symbol', 'N/A')}")
                print(f"Name: {data.get('name', 'N/A')}")
                print("\nWebSocket is WORKING!")
            except asyncio.TimeoutError:
                print("\nNo new tokens in 20s (normal - depends on market activity)")
                print("WebSocket connection is WORKING but no new tokens launched")

    except Exception as e:
        print(f"WebSocket ERROR: {str(e)}")

# Run WebSocket test
try:
    asyncio.run(test_websocket())
except Exception as e:
    print(f"Failed to run WebSocket test: {e}")

print("\n" + "=" * 70)
print("CONCLUSION")
print("=" * 70)
print("\nYour bot already uses wss://pumpportal.fun/api/data for new tokens.")
print("This WebSocket gives you: mint, symbol, name")
print("Then the bot fetches price from Solana RPC directly.")
print("\nThis is the BEST approach - no API needed!")
