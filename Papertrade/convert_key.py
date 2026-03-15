"""
Wallet Private Key Converter
Converts Phantom/Base58 private keys to wallet_config.json format
"""

import json
import sys

try:
    import base58
except ImportError:
    print("❌ base58 library not installed")
    print("Installing base58...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "base58"])
    import base58

def convert_base58_key():
    """Convert Base58 private key (from Phantom) to wallet config"""
    print("=" * 60)
    print("🔑 Phantom/Base58 Private Key Converter")
    print("=" * 60)
    print()
    print("This tool converts your Phantom wallet private key to the")
    print("format needed by the trading bot.")
    print()
    print("⚠️  SECURITY WARNING:")
    print("   - Your private key controls your wallet")
    print("   - Never share it with anyone")
    print("   - Keep wallet_config.json secret")
    print()

    # Get private key from user
    print("📝 Paste your Base58 private key from Phantom:")
    print("   (It looks like: 5x7HjK9mN...)")
    print()
    private_key_base58 = input("Private Key: ").strip()

    if not private_key_base58:
        print("❌ No private key provided")
        return

    try:
        # Convert Base58 to bytes
        print("\n🔄 Converting...")
        private_key_bytes = base58.b58decode(private_key_base58)

        # Verify length (should be 64 bytes)
        if len(private_key_bytes) != 64:
            print(f"❌ Invalid key length: {len(private_key_bytes)} (expected 64)")
            print("   Make sure you copied the FULL private key")
            return

        # Convert to array
        private_key_array = list(private_key_bytes)

        # Create wallet config
        config = {
            "private_key": private_key_array,
            "rpc_url": "https://api.mainnet-beta.solana.com",
            "max_slippage_bps": 500,
            "priority_fee_lamports": 100000,
            "compute_unit_limit": 200000
        }

        # Save to file
        with open('wallet_config.json', 'w') as f:
            json.dump(config, f, indent=2)

        print("\n✅ Success!")
        print(f"📁 Created: wallet_config.json")
        print()
        print("🔍 Verify your wallet address:")
        print("   Run: python live_trader.py")
        print()
        print("⚠️  Next steps:")
        print("   1. Make sure wallet_config.json is in .gitignore")
        print("   2. NEVER share this file")
        print("   3. Test with: python live_trader.py")
        print()

    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")
        print("\nPossible issues:")
        print("   - Invalid Base58 format")
        print("   - Incomplete private key")
        print("   - Wrong key type")

def convert_solana_keypair():
    """Convert Solana CLI keypair file to wallet config"""
    print("=" * 60)
    print("🔑 Solana CLI Keypair Converter")
    print("=" * 60)
    print()
    print("📝 Enter path to your Solana keypair file:")
    print("   (e.g., ~/trading-wallet.json or C:\\Users\\...\\wallet.json)")
    print()
    keypair_path = input("Path: ").strip()

    if not keypair_path:
        print("❌ No path provided")
        return

    try:
        # Load keypair file
        with open(keypair_path, 'r') as f:
            private_key = json.load(f)

        # Verify it's an array of 64 numbers
        if not isinstance(private_key, list) or len(private_key) != 64:
            print(f"❌ Invalid keypair file (expected array of 64 numbers)")
            return

        # Create wallet config
        config = {
            "private_key": private_key,
            "rpc_url": "https://api.mainnet-beta.solana.com",
            "max_slippage_bps": 500,
            "priority_fee_lamports": 100000,
            "compute_unit_limit": 200000
        }

        # Save to file
        with open('wallet_config.json', 'w') as f:
            json.dump(config, f, indent=2)

        print("\n✅ Success!")
        print(f"📁 Created: wallet_config.json")
        print()
        print("🔍 Verify your wallet address:")
        print("   Run: python live_trader.py")
        print()

    except FileNotFoundError:
        print(f"❌ File not found: {keypair_path}")
    except json.JSONDecodeError:
        print("❌ Invalid JSON format in keypair file")
    except Exception as e:
        print(f"❌ Conversion failed: {str(e)}")

def main():
    print()
    print("╔════════════════════════════════════════════════════════╗")
    print("║     Wallet Private Key Converter for Live Trading      ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()
    print("Choose your wallet type:")
    print()
    print("  1. Phantom Wallet (Base58 private key)")
    print("  2. Solana CLI (keypair JSON file)")
    print("  3. Exit")
    print()

    choice = input("Enter choice (1-3): ").strip()

    if choice == '1':
        convert_base58_key()
    elif choice == '2':
        convert_solana_keypair()
    elif choice == '3':
        print("👋 Goodbye!")
    else:
        print("❌ Invalid choice")

if __name__ == "__main__":
    main()
