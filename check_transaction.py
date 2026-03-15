"""
Quick script to check a transaction on Solana
"""
import sys
from solana.rpc.api import Client

if len(sys.argv) < 2:
    print("Usage: python check_transaction.py <signature>")
    print("Example: python check_transaction.py 5wHu7...xyz")
    sys.exit(1)

signature = sys.argv[1]

print("=" * 70)
print(f"CHECKING TRANSACTION: {signature}")
print("=" * 70)

client = Client("https://api.mainnet-beta.solana.com")

try:
    # Get transaction details
    print("\nFetching transaction from blockchain...")
    result = client.get_transaction(signature)

    if result.value is None:
        print("\n❌ Transaction not found on blockchain")
        print("\nPossible reasons:")
        print("  1. Transaction failed/rejected")
        print("  2. Signature is incorrect")
        print("  3. Transaction not confirmed yet (wait a few seconds)")
        print(f"\n🔗 Check on Solscan: https://solscan.io/tx/{signature}")
    else:
        tx = result.value
        print("\n✅ Transaction found!")

        # Check if successful
        if tx.transaction.meta.err is None:
            print("✅ Status: SUCCESS")
        else:
            print(f"❌ Status: FAILED")
            print(f"   Error: {tx.transaction.meta.err}")

        print(f"\n📊 Details:")
        print(f"  Slot: {tx.slot}")
        print(f"  Block Time: {tx.block_time}")
        print(f"  Fee: {tx.transaction.meta.fee / 1e9:.6f} SOL")

        # Balance changes
        pre = tx.transaction.meta.pre_balances
        post = tx.transaction.meta.post_balances

        print(f"\n💰 Balance Changes:")
        for i, (before, after) in enumerate(zip(pre, post)):
            change = (after - before) / 1e9
            if change != 0:
                sign = "+" if change > 0 else ""
                print(f"  Account {i}: {sign}{change:.6f} SOL")

        # Logs
        if tx.transaction.meta.log_messages:
            print(f"\n📝 Program Logs:")
            for log in tx.transaction.meta.log_messages[:10]:
                print(f"  {log}")

        print(f"\n🔗 View on Solscan: https://solscan.io/tx/{signature}")

except Exception as e:
    print(f"\n❌ Error fetching transaction: {str(e)}")
    print(f"\n🔗 Try checking on Solscan: https://solscan.io/tx/{signature}")

print("\n" + "=" * 70)
