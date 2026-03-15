"""
convert_hex_key.py — Convert hex private key to wallet_config.json
"""
import json, sys, getpass

print("=" * 56)
print("  Hex Private Key → wallet_config.json Converter")
print("=" * 56)
print()
print("  Paste your hex private key below.")
print("  Input is hidden — it won't show on screen.")
print()

try:
    hex_key = getpass.getpass("  Hex key: ").strip()
except Exception:
    hex_key = input("  Hex key: ").strip()

# Strip common prefixes if present
hex_key = hex_key.removeprefix("0x").removeprefix("0X")

try:
    key_bytes = bytes.fromhex(hex_key)
except ValueError:
    print("\n  [ERR] Invalid hex string — make sure it contains only 0-9 and a-f.")
    sys.exit(1)

if len(key_bytes) != 64:
    print(f"\n  [ERR] Expected 64 bytes, got {len(key_bytes)}.")
    print("  A Solana private key is always 128 hex characters (64 bytes).")
    sys.exit(1)

# Load existing config so we don't overwrite RPC / fee settings
try:
    with open("wallet_config.json", "r") as f:
        config = json.load(f)
except Exception:
    config = {
        "rpc_url": "https://api.mainnet-beta.solana.com",
        "max_slippage_bps": 1000,
        "priority_fee_lamports": 10000,
        "compute_unit_limit": 150000,
        "use_jito_mev_protection": False,
        "jito_tip_lamports": 5000,
        "jito_block_engine_url": "https://mainnet.block-engine.jito.wtf",
    }

config["private_key"] = list(key_bytes)

with open("wallet_config.json", "w") as f:
    json.dump(config, f, indent=2)

print()
print("  [OK] wallet_config.json updated.")
print()

# Show wallet address for verification
try:
    from solders.keypair import Keypair
    kp = Keypair.from_bytes(key_bytes)
    print(f"  Wallet address : {kp.pubkey()}")
    print(f"  Check balance  : https://solscan.io/account/{kp.pubkey()}")
except Exception:
    print("  (Install dependencies to see wallet address: pip install solders)")

print()
