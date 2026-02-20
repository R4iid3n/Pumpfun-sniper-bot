"""Check wallet balance"""
from solana.rpc.api import Client
from solders.keypair import Keypair
import json

# Load config
config = json.load(open('wallet_config.json'))
kp = Keypair.from_bytes(bytes(config['private_key']))

print("=" * 60)
print("WALLET BALANCE CHECK")
print("=" * 60)
print(f"\nWallet Address: {str(kp.pubkey())}")

# Get balance
client = Client('https://api.mainnet-beta.solana.com')
balance_lamports = client.get_balance(kp.pubkey()).value
balance_sol = balance_lamports / 1e9

print(f"\nSOL Balance: {balance_sol:.6f} SOL")
print(f"USD Value: ~${balance_sol * 150:.2f} (at $150/SOL)")
print(f"Raw lamports: {balance_lamports}")

# Budget analysis
print("\n" + "=" * 60)
print("BUDGET ANALYSIS FOR TRADING")
print("=" * 60)

max_position = config.get('max_position_sol', 0.01)
priority_fee = config.get('priority_fee_lamports', 50000) / 1e9

print(f"\nMax position size: {max_position} SOL (~${max_position * 150:.2f})")
print(f"Priority fee per tx: {priority_fee:.6f} SOL (~${priority_fee * 150:.4f})")
print(f"Slippage tolerance: {config.get('max_slippage_bps', 500) / 100}%")

# Calculate number of trades possible
reserve_for_fees = 0.005  # Reserve 0.005 SOL for fees
trading_capital = max(0, balance_sol - reserve_for_fees)
trades_possible = int(trading_capital / max_position)

print(f"\nReserve for fees: {reserve_for_fees} SOL (~${reserve_for_fees * 150:.2f})")
print(f"Trading capital: {trading_capital:.4f} SOL (~${trading_capital * 150:.2f})")
print(f"Trades possible: ~{trades_possible} trades")

if balance_sol < 0.02:
    print("\n⚠️  WARNING: Very low balance!")
    print("   Recommended minimum: 0.05 SOL (~$7.5)")
    print("   Optimal for testing: 0.1-0.2 SOL ($15-30)")
elif balance_sol < 0.05:
    print("\n⚠️  WARNING: Low balance")
    print("   You can test but have very limited trades")
    print("   Consider adding more SOL for better testing")
else:
    print("\n✅ Balance sufficient for testing")

print("\n" + "=" * 60)
