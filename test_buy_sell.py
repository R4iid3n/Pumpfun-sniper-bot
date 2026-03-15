"""
Buy/Sell Execution Test
=======================
Executes one real buy followed by an immediate sell on a Pump.fun token.
Retry logic (slippage escalation, fresh quotes) is handled inside live_trader.py.

Usage:
    python test_buy_sell.py <MINT_ADDRESS>
    python test_buy_sell.py <MINT_ADDRESS> --sol 0.005 --slippage 0.15

Find an active token still on bonding curve at https://pump.fun
Copy the mint address from the URL: pump.fun/coin/<MINT_ADDRESS>

WARNING: Uses REAL SOL. Default is 0.002 SOL.
"""

import sys
import time
import argparse
from live_trader import LiveTrader

DEFAULT_SOL      = 0.002
DEFAULT_SLIPPAGE = 0.10   # 10% — good starting point for new tokens
SELL_WAIT        = 5      # seconds between buy confirm and sell attempt

def sep(char="─", n=62): print(char * n)
def ok(label, value=""):  print(f"  ✅  {label}" + (f": {value}" if value else ""))
def fail(label, value=""): print(f"  ❌  {label}" + (f": {value}" if value else ""))
def info(label, value=""): print(f"  ℹ️   {label}" + (f": {value}" if value else ""))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mint",          help="Token mint address (still on bonding curve)")
    parser.add_argument("--sol",         type=float, default=DEFAULT_SOL,
                        help=f"SOL to spend (default {DEFAULT_SOL})")
    parser.add_argument("--slippage",    type=float, default=DEFAULT_SLIPPAGE,
                        help=f"Starting slippage 0–1 (default {DEFAULT_SLIPPAGE})")
    parser.add_argument("--config",      default="wallet_config.json")
    args = parser.parse_args()

    mint     = args.mint
    sol_in   = args.sol
    slippage = args.slippage

    sep("═")
    print("  PUMP.FUN — BUY / SELL EXECUTION TEST")
    sep("═")
    print(f"  Mint      : {mint}")
    print(f"  SOL in    : {sol_in}")
    print(f"  Slippage  : {slippage*100:.0f}% initial (auto-escalates on error 6001)")
    sep()

    # ── 1. Load wallet ───────────────────────────────────────────────────
    print("\n[1/6]  Loading wallet...")
    try:
        trader = LiveTrader(args.config, verbose=True)
    except Exception as e:
        fail("Wallet load failed", str(e))
        sys.exit(1)

    sol_before = trader.get_sol_balance()
    ok("Wallet",    trader.wallet_address)
    ok("Balance",   f"{sol_before:.6f} SOL")
    ok("RPC",       trader.rpc_url)
    ok("MEV",       "Jito ON" if trader.use_jito else "off")
    ok("Preflight", "SKIPPED (skip_preflight=True)")

    min_needed = sol_in + trader.min_sol_balance
    if sol_before < min_needed:
        fail("Insufficient SOL", f"need ≥{min_needed:.4f}, have {sol_before:.6f}")
        sys.exit(1)

    # ── 2. Bonding curve sanity check ────────────────────────────────────
    sep()
    print("\n[2/6]  Checking bonding curve...")
    curve = trader.get_bonding_curve_state(mint)
    if not curve:
        fail("Bonding curve not found",
             "token is migrated to Raydium or the mint address is wrong")
        sys.exit(1)

    vsr   = curve['virtual_sol_reserves'] / 1e9
    vtr   = curve['virtual_token_reserves'] / 1e6
    price = vsr / vtr if vtr else 0
    expected = trader.calculate_buy_amount(curve, sol_in)

    ok("Virtual SOL reserves",   f"{vsr:.4f} SOL")
    ok("Virtual token reserves", f"{vtr:,.0f}")
    ok("Implied token price",    f"${price:.10f}")
    ok("Expected tokens out",    f"{expected:,.2f}")

    # ── 3. BUY ───────────────────────────────────────────────────────────
    sep()
    print(f"\n[3/6]  BUY — {sol_in} SOL")
    info("Retry policy", "auto-escalates slippage on error 6001 (up to 50%)")
    t0 = time.time()
    buy_sig = trader.buy_token_pumpfun(mint, sol_in, max_slippage=slippage)
    buy_ms  = (time.time() - t0) * 1000

    if not buy_sig:
        fail("BUY FAILED after all retry attempts")
        print("\n  Possible causes:")
        print("  • Token already migrated to Raydium (bonding curve full)")
        print("  • Repeated slippage errors — token price spiked between each attempt")
        print("  • RPC rate limit / network issue")
        sys.exit(1)

    ok(f"Buy confirmed in {buy_ms:.0f} ms", buy_sig)
    print(f"\n  🔗  https://solscan.io/tx/{buy_sig}")

    # ── 4. Verify token balance ──────────────────────────────────────────
    sep()
    print(f"\n[4/6]  Verifying token balance (waiting {SELL_WAIT}s)...")
    time.sleep(SELL_WAIT)

    token_balance = 0.0
    for attempt in range(1, 6):
        token_balance = trader.get_token_balance(mint)
        if token_balance > 0:
            break
        print(f"  ⏳  Attempt {attempt}/5 — balance still 0, waiting 2s...")
        time.sleep(2)

    if token_balance <= 0:
        fail("Token balance is 0 after confirmed buy")
        print("\n  Check the buy TX on Solscan — the tx may have succeeded on-chain")
        print("  but the token already rugged before we could verify balance.")
        print(f"\n  Buy TX: https://solscan.io/tx/{buy_sig}")
        sys.exit(1)

    ok("Tokens received", f"{token_balance:,.4f}")

    # ── 5. SELL ──────────────────────────────────────────────────────────
    sep()
    print(f"\n[5/6]  SELL — {token_balance:,.4f} tokens")
    info("Retry policy", "doubles slippage each attempt, last attempt accepts any price")
    t0 = time.time()
    sell_sig = trader.sell_token_pumpfun(mint, token_balance, max_slippage=slippage)
    sell_ms  = (time.time() - t0) * 1000

    if not sell_sig:
        fail("SELL FAILED after all retry attempts")
        print(f"\n  Your tokens ({token_balance:,.4f}) are still in your wallet.")
        print(f"  Sell manually at: https://pump.fun/coin/{mint}")
        sys.exit(1)

    ok(f"Sell confirmed in {sell_ms:.0f} ms", sell_sig)
    print(f"\n  🔗  https://solscan.io/tx/{sell_sig}")

    # ── 6. Summary ───────────────────────────────────────────────────────
    sep()
    print("\n[6/6]  Final balance check...")
    time.sleep(3)
    sol_after = trader.get_sol_balance()
    net_cost  = sol_before - sol_after

    sep("═")
    print("  RESULT")
    sep("═")
    ok("SOL before", f"{sol_before:.6f}")
    ok("SOL after",  f"{sol_after:.6f}")
    ok("Net cost",   f"{net_cost:.6f} SOL  (fees + spread)")
    print()
    print(f"  Buy  → https://solscan.io/tx/{buy_sig}")
    print(f"  Sell → https://solscan.io/tx/{sell_sig}")
    sep("═")
    print("\n  ✅  BOTH TRANSACTIONS EXECUTED SUCCESSFULLY\n")
    print("  The live bot is ready to trade.\n")


if __name__ == "__main__":
    main()
