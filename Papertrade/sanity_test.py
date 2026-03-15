"""
sanity_test.py — One Buy + Immediate Sell (Live Sanity Check)
=============================================================
Detects new pump.fun tokens via pumpportal.fun WebSocket.

NOTE: logsSubscribe @ processed was tested but floods QuikNode with hundreds
of pump.fun transactions/sec, which throttles subsequent signatureSubscribe
calls and causes 10s+ confirmation times.  Use find_token_logs_ws() only if
you have a dedicated RPC node with no shared WS connection pressure.

Usage:
    python sanity_test.py
    python sanity_test.py --sol 0.001 --slippage 0.15 --wait 8
"""

import asyncio
import json
import sys
import time
import argparse
import requests
import websockets
from solders.pubkey import Pubkey
from live_trader import LiveTrader

# ── Constants ──────────────────────────────────────────────────────────────────
PUMPFUN_PROGRAM  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPPORTAL_WS    = "wss://pumpportal.fun/api/data"

# Default initial bonding curve values at token launch (pump.fun protocol constants)
INIT_VIRTUAL_SOL    = 30_000_000_000        # 30 SOL in lamports
INIT_VIRTUAL_TOKENS = 1_073_000_191_000_000 # ~1.073T tokens (6 decimals)
INIT_REAL_TOKENS    = 793_100_000_000_000

# ── Helpers ───────────────────────────────────────────────────────────────────
def sep(c="─", n=64): print(c * n)
def ok(msg):   print(f"  [OK]   {msg}")
def err(msg):  print(f"  [ERR]  {msg}")
def info(msg): print(f"  [---]  {msg}")


# ── QuikNode logsSubscribe (primary fast path) ────────────────────────────────
def _fetch_tx_accounts(sig: str, rpc_url: str) -> dict | None:
    """HTTP getTransaction to extract mint + creator from account keys.

    Tries processed first (available immediately on same RPC node that fired
    the logsNotification), falls back to confirmed if not yet indexed.
    """
    for commitment in ("processed", "confirmed"):
        try:
            r = requests.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [sig, {
                    "encoding": "jsonParsed",
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": 0,
                }],
            }, timeout=3)
            result = r.json().get("result")
            if not result:
                continue

            account_keys = (result.get("transaction", {})
                                  .get("message", {})
                                  .get("accountKeys", []))

            mint    = None
            creator = None
            for key_info in account_keys:
                pubkey = key_info.get("pubkey", "") if isinstance(key_info, dict) else str(key_info)
                if pubkey.endswith("pump") and mint is None:
                    mint = pubkey
                if (isinstance(key_info, dict)
                        and key_info.get("signer")
                        and key_info.get("writable")
                        and not pubkey.endswith("pump")
                        and creator is None):
                    creator = pubkey

            if mint:
                return {"mint": mint, "creator": creator}
        except Exception:
            pass
    return None


async def find_token_logs_ws(rpc_url: str) -> dict | None:
    """
    Detect new pump.fun tokens via standard Solana logsSubscribe on QuikNode.

    Subscribes at 'processed' commitment — fires in the same slot as token
    creation, ~400ms before pumpportal.fun re-broadcasts the event.
    Works with any standard RPC provider (QuikNode, Helius, Triton, etc.).
    """
    ws_url = rpc_url.replace("https://", "wss://").replace("http://", "ws://")

    sub_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [PUMPFUN_PROGRAM]},
            {"commitment": "processed"},
        ],
    })

    try:
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10,
                                      open_timeout=8) as ws:
            await ws.send(sub_msg)
            ok("Connected to QuikNode WebSocket (logsSubscribe @ processed)")

            seen = 0
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    err("No pump.fun transactions in 60s")
                    return None

                data = json.loads(raw)

                # Subscription confirmation
                if isinstance(data.get("result"), int):
                    ok(f"Subscribed to pump.fun logs (sub_id={data['result']})")
                    continue

                if data.get("method") != "logsNotification":
                    continue

                value = data.get("params", {}).get("result", {}).get("value", {})

                # Skip failed transactions
                if value.get("err") is not None:
                    continue

                logs = value.get("logs") or []

                # Must be a pump.fun Create, not a buy/sell/other
                if not any("Instruction: Create" in log for log in logs):
                    continue

                sig = value.get("signature")
                if not sig:
                    continue

                # Fetch full TX in a thread so we don't block the WS event loop
                tx_data = await asyncio.to_thread(_fetch_tx_accounts, sig, rpc_url)
                if not tx_data:
                    continue

                mint    = tx_data.get("mint")
                creator = tx_data.get("creator")
                if not mint:
                    continue

                seen += 1
                info(f"#{seen:3d}  mint: {mint[:8]}...  (logsSubscribe @ processed)")

                # Derive BC PDA locally — no extra RPC call needed
                bc_key = ""
                try:
                    bc_pda_pubkey, _ = Pubkey.find_program_address(
                        [b"bonding-curve", bytes(Pubkey.from_string(mint))],
                        Pubkey.from_string(PUMPFUN_PROGRAM)
                    )
                    bc_key = str(bc_pda_pubkey)
                except Exception:
                    pass

                prefetched_curve = None
                if creator and bc_key:
                    try:
                        prefetched_curve = {
                            "virtual_token_reserves": INIT_VIRTUAL_TOKENS,
                            "virtual_sol_reserves":   INIT_VIRTUAL_SOL,
                            "real_token_reserves":    INIT_REAL_TOKENS,
                            "real_sol_reserves":      0,
                            "bonding_curve_pda":      bc_key,
                            "creator":                Pubkey.from_string(creator),
                        }
                    except Exception:
                        pass

                ok(f"Selected: {mint[:8]}...  creator: {(creator or '?')[:8]}...")
                return {
                    "mint":             mint,
                    "symbol":           mint[:6],
                    "name":             mint[:6],
                    "vsr_sol":          30.0,
                    "prefetched_curve": prefetched_curve,
                    "source":           "logsSubscribe",
                }

    except Exception as e:
        err(f"QuikNode logsSubscribe failed ({type(e).__name__}: {e})")
        return None


# ── pumpportal fallback ────────────────────────────────────────────────────────
async def find_token_pumpportal() -> dict | None:
    """
    Fallback: subscribe to pumpportal.fun new-token events.
    Fires slightly later than transactionSubscribe (after their indexing delay).
    """
    print(f"\n  Connecting to pumpportal.fun WebSocket...")

    async with websockets.connect(PUMPPORTAL_WS,
                                  ping_interval=20, ping_timeout=10) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        ok("Connected to pumpportal.fun WebSocket")
        ok("Subscribed to new token events")

        seen = 0
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                err("No new tokens in 60s — is pump.fun active?")
                return None

            data  = json.loads(msg)
            mint  = data.get("mint")
            if not mint:
                continue

            seen += 1
            symbol = data.get("symbol", mint[:6])
            vsr    = data.get("virtualSolReserves") or data.get("vSolInBondingCurve") or 0
            vtr    = data.get("virtualTokenReserves") or data.get("vTokensInBondingCurve") or 0

            info(f"#{seen:3d}  {symbol:<12}  mint: {mint[:8]}...  (pumpportal fallback)")

            trader_key = data.get("traderPublicKey")
            bc_key     = data.get("bondingCurveKey")
            prefetched_curve = None
            if trader_key:
                try:
                    prefetched_curve = {
                        "virtual_token_reserves": int(float(vtr)) if vtr else INIT_VIRTUAL_TOKENS,
                        "virtual_sol_reserves":   int(float(vsr)) if vsr else INIT_VIRTUAL_SOL,
                        "real_token_reserves":    0,
                        "real_sol_reserves":      0,
                        "bonding_curve_pda":      bc_key or "",
                        "creator":                Pubkey.from_string(trader_key),
                    }
                except Exception:
                    pass

            ok(f"Selected: {symbol} ({mint[:8]}...)")
            return {
                "mint":             mint,
                "symbol":           symbol,
                "name":             data.get("name", symbol),
                "vsr_sol":          float(vsr) / 1e9 if vsr else 30.0,
                "prefetched_curve": prefetched_curve,
                "source":           "pumpportal",
            }


# ── Top-level find ─────────────────────────────────────────────────────────────
async def find_token(rpc_url: str) -> dict | None:
    # NOTE: logsSubscribe (find_token_logs_ws) floods QuikNode with all pump.fun
    # transactions — hundreds/sec — throttling signatureSubscribe and causing 10s+
    # confirmation times.  Pumpportal adds ~200-400ms detection latency but keeps
    # the WS connection clean.  Switch to find_token_logs_ws only on a dedicated RPC.
    print(f"\n  Waiting for the next new token on pump.fun...")
    print(f"  (press Ctrl+C to abort)\n")
    return await find_token_pumpportal()


# ── Main logic ────────────────────────────────────────────────────────────────
def run_sanity(sol: float, slippage: float, sell_wait: int, max_buy_ms: int | None = None):
    sep("═")
    print("  PUMP.FUN SANITY TEST — ONE BUY + IMMEDIATE SELL")
    sep("═")

    # 1. Load wallet
    print("\n[1/5]  Loading wallet...")
    try:
        trader = LiveTrader("wallet_config.json", verbose=True)
    except Exception as e:
        err(f"Wallet load failed: {e}")
        sys.exit(1)

    balance = trader.get_sol_balance()
    ok(f"Wallet  : {trader.wallet_address}")
    ok(f"Balance : {balance:.6f} SOL")
    ok(f"RPC     : {trader.rpc_url}")
    ok(f"MEV     : {'Jito ON' if trader.use_jito else 'OFF'}")

    min_needed = sol + trader.min_sol_balance
    if balance < min_needed:
        err(f"Need >= {min_needed:.4f} SOL, have {balance:.6f}")
        sys.exit(1)

    # 2. Find token
    sep()
    print("\n[2/5]  Listening for a fresh token...")
    try:
        token = asyncio.run(find_token(trader.rpc_url))
    except KeyboardInterrupt:
        print("\n  Aborted by user.")
        sys.exit(0)

    if not token:
        err("Could not find a suitable token. Try again in a few minutes.")
        sys.exit(1)

    mint             = token["mint"]
    symbol           = token["symbol"]
    source           = token.get("source", "?")
    prefetched_curve = token.get("prefetched_curve")

    sep()
    print(f"\n[3/5]  BUY — {sol} SOL → {symbol}")
    info(f"Mint     : {mint}")
    info(f"Source   : {source}")
    info(f"Slippage : {slippage*100:.0f}% (auto-escalates on error 6001/6003)")
    if prefetched_curve:
        info("Curve data ready — buying immediately (processed commitment, no wait)")
    else:
        info("No curve data — waiting 2s before buy...")
        time.sleep(2)

    t0      = time.time()
    buy_sig = trader.buy_token_pumpfun(mint, sol, max_slippage=slippage,
                                       prefetched_curve=prefetched_curve,
                                       max_buy_ms=max_buy_ms)
    buy_ms  = (time.time() - t0) * 1000

    if not buy_sig:
        err("BUY FAILED — see errors above")
        sys.exit(1)

    ok(f"Buy confirmed in {buy_ms:.0f} ms")
    print(f"\n  https://solscan.io/tx/{buy_sig}")

    # 4. Get token balance
    sep()
    print(f"\n[4/5]  Verifying token balance (waiting {sell_wait}s)...")
    time.sleep(sell_wait)

    token_balance = trader.get_token_balance(mint, verbose=True)

    if token_balance <= 0:
        info("Balance query returned 0 — buy was confirmed, attempting sell anyway...")
        token_balance = -1
    else:
        ok(f"Tokens received: {token_balance:,.4f}")

    # 5. Sell immediately
    sell_amount = token_balance if token_balance > 0 else 0.0
    sep()
    print(f"\n[5/5]  SELL — {symbol} tokens (immediate)")
    info("Last attempt will accept any price (emergency exit guarantee)")

    t0       = time.time()
    sell_sig = trader.sell_token_pumpfun(mint, sell_amount, max_slippage=slippage)
    sell_ms  = (time.time() - t0) * 1000

    if not sell_sig:
        err("SELL FAILED — see errors above")
        print(f"\n  Your {token_balance:,.4f} {symbol} tokens are still in your wallet.")
        print(f"  Sell manually: https://pump.fun/coin/{mint}")
        sys.exit(1)

    ok(f"Sell confirmed in {sell_ms:.0f} ms")
    print(f"\n  https://solscan.io/tx/{sell_sig}")

    # Summary
    sep()
    print("\n[DONE]  Final balance check...")
    time.sleep(3)
    balance_after = trader.get_sol_balance()
    net_cost      = balance - balance_after

    sep("═")
    print("  RESULT")
    sep("═")
    ok(f"SOL before  : {balance:.6f}")
    ok(f"SOL after   : {balance_after:.6f}")
    ok(f"Net cost    : {net_cost:.6f} SOL  (fees + spread)")
    print()
    print(f"  Buy  → https://solscan.io/tx/{buy_sig}")
    print(f"  Sell → https://solscan.io/tx/{sell_sig}")
    sep("═")
    print("\n  BOTH TRANSACTIONS EXECUTED SUCCESSFULLY")
    print("  The live pipeline is working.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="One buy + sell sanity test on a live pump.fun token")
    ap.add_argument("--sol",         type=float, default=0.002,
                    help="SOL to spend (default 0.002)")
    ap.add_argument("--slippage",    type=float, default=0.15,
                    help="Starting slippage 0-1 (default 0.15 = 15%%, escalates on error 6001/6003)")
    ap.add_argument("--wait",        type=int,   default=5,
                    help="Seconds to wait after buy before selling (default 5)")
    ap.add_argument("--max-buy-ms",  type=int,   default=None,
                    help="Abort buy if not confirmed within this many ms (default: no limit)")
    args = ap.parse_args()

    run_sanity(args.sol, args.slippage, args.wait, args.max_buy_ms)
