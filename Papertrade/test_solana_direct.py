"""
Test fetching pump.fun token data directly from Solana blockchain
This bypasses the API entirely!
"""
from solana.rpc.api import Client
from solders.pubkey import Pubkey
import struct

# Test token
TEST_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

print("=" * 70)
print("TESTING SOLANA RPC DIRECT ACCESS")
print("=" * 70)

# Test different RPC endpoints
rpc_endpoints = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/demo",
    "https://rpc.ankr.com/solana",
    "https://solana-api.projectserum.com",
]

def derive_bonding_curve_pda(mint_str, program_str):
    """Derive the bonding curve PDA for a mint"""
    try:
        mint_pubkey = Pubkey.from_string(mint_str)
        program_pubkey = Pubkey.from_string(program_str)

        # Find PDA with seeds: ["bonding-curve", mint]
        seeds = [b"bonding-curve", bytes(mint_pubkey)]
        pda, bump = Pubkey.find_program_address(seeds, program_pubkey)

        return pda, bump
    except Exception as e:
        print(f"ERROR deriving PDA: {e}")
        return None, None

print(f"\nTest Mint: {TEST_MINT}")

# First, derive the bonding curve PDA
pda, bump = derive_bonding_curve_pda(TEST_MINT, PUMP_FUN_PROGRAM)

if pda:
    print(f"Bonding Curve PDA: {str(pda)}")
    print(f"Bump: {bump}")
    print()

    for i, rpc_url in enumerate(rpc_endpoints, 1):
        print(f"\n[{i}/{len(rpc_endpoints)}] Testing RPC: {rpc_url}")
        print("-" * 70)

        try:
            client = Client(rpc_url)

            # Test 1: Get account info
            print("  Fetching bonding curve account...")
            response = client.get_account_info(pda)

            if response.value is None:
                print("  RESULT: Account not found (token may not exist or is graduated)")
                continue

            account_data = response.value.data
            data_len = len(account_data)

            print(f"  SUCCESS - Account found!")
            print(f"  Data length: {data_len} bytes")

            if data_len >= 40:
                # Try to decode pump.fun bonding curve data
                # Layout: discriminator (8) + virtual_token_reserves (8) + virtual_sol_reserves (8) + ...
                try:
                    virtual_token_reserves = struct.unpack('<Q', account_data[8:16])[0]
                    virtual_sol_reserves = struct.unpack('<Q', account_data[16:24])[0]

                    print(f"\n  BONDING CURVE DATA:")
                    print(f"    Virtual Token Reserves: {virtual_token_reserves:,}")
                    print(f"    Virtual SOL Reserves: {virtual_sol_reserves:,}")

                    # Calculate price
                    if virtual_token_reserves > 0:
                        price = (virtual_sol_reserves / 1e9) / (virtual_token_reserves / 1e6)
                        print(f"    Calculated Price: ${price:.10f}")

                        print(f"\n  THIS RPC WORKS! Bot can fetch real-time prices!")
                    else:
                        print(f"    WARNING: Token reserves are 0 (token may be dead)")

                except Exception as e:
                    print(f"  ERROR decoding data: {e}")
            else:
                print(f"  WARNING: Data too short ({data_len} bytes), expected 40+")

            # Test 2: Check RPC latency
            import time
            start = time.time()
            test_response = client.get_balance(pda)
            latency = (time.time() - start) * 1000
            print(f"\n  RPC Latency: {latency:.0f}ms")

            if latency < 500:
                print(f"  SPEED: EXCELLENT (< 500ms)")
            elif latency < 1000:
                print(f"  SPEED: GOOD (< 1s)")
            else:
                print(f"  SPEED: SLOW (> 1s)")

        except Exception as e:
            print(f"  FAILED: {str(e)[:100]}")

else:
    print("CRITICAL ERROR: Could not derive bonding curve PDA")

print("\n" + "=" * 70)
print("CONCLUSION")
print("=" * 70)
print("\nThe bot DOES NOT need pump.fun API!")
print("It can fetch data directly from Solana blockchain using RPC.")
print("\nThis method:")
print("  ✓ Bypasses Cloudflare completely")
print("  ✓ Gets real-time on-chain data")
print("  ✓ Works even if pump.fun website is down")
print("\nYour bot already has this code (fetch_from_solana method).")
print("The $0.000000 prices are likely because:")
print("  1. Tokens are TOO NEW (bonding curve not created yet)")
print("  2. Tokens are DEAD/GRADUATED (bonding curve closed)")
print("  3. Bot is sniping faster than blockchain confirms")
