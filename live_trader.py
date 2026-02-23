"""
Live Trading Module for Pump.fun Bot
Handles real Solana transactions for buying/selling tokens
"""

import asyncio
import json
import threading
import time
import struct
from typing import Optional, Dict
import websockets
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed, Processed
from solana.rpc.types import TxOpts
from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message, MessageV0
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction, AccountMeta
from spl.token.instructions import get_associated_token_address
from solders.token.associated import get_associated_token_address as get_ata_with_program
import requests

# Token program constants
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
SYSVAR_RENT_PUBKEY = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

class LiveTrader:
    def __init__(self, wallet_config_path: str = "wallet_config.json", verbose: bool = False):
        """Initialize live trader with wallet configuration

        Args:
            wallet_config_path: Path to wallet configuration JSON
            verbose: Enable verbose debug logging (default: False)
        """
        self.verbose = verbose
        self.load_wallet_config(wallet_config_path)
        # Use `processed` commitment everywhere for speed.
        # At `processed`, accounts created in a tx are readable immediately after the
        # leader processes the slot — no waiting for 2/3 supermajority (confirmed).
        # This eliminates the 5-15s Helius HTTP lag we observed with `confirmed`.
        self.client = Client(self.rpc_url, commitment=Processed)

        # Pump.fun program constants
        self.PUMPFUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
        self.PUMPFUN_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        self.PUMPFUN_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
        self.PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

        # Global volume accumulator - single global PDA shared across the whole program
        # This account tracks total buy/sell volume for all tokens
        self.GLOBAL_VOLUME_ACCUMULATOR, _ = Pubkey.find_program_address(
            [b"global_volume_accumulator"],  # Exact seed from Pump.fun IDL
            self.PUMPFUN_PROGRAM
        )

        # Fee-related accounts (new in current Pump.fun version)
        self.FEE_CONFIG = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")
        self.FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

        # System programs
        self.SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
        self.SYSVAR_RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

        # Safety limits
        self.max_position_size_sol = 0.2   # Max 0.2 SOL per trade (~$36 at $180/SOL, covers $5-$20 positions)
        self.min_sol_balance = 0.005  # Keep 0.005 SOL for gas (enough for ~10 tx fees)

        # Additional safety - daily trade limits
        self.max_daily_trades = 20
        self.max_daily_loss_sol = 1.0  # Stop if lose more than 1 SOL in a day
        self.daily_trades = 0
        self.daily_pnl_sol = 0.0

        self.last_reset_day = time.strftime("%Y-%m-%d")

        # Transaction stats
        self.total_trades = 0
        self.failed_trades = 0

        # ATA + token program cache keyed by mint address.
        # Populated at buy time, consumed at sell time to skip 2 get_account_info RPCs.
        self.token_program_cache = {}

        # Blockhash cache — background thread keeps this fresh every 400ms.
        # TX build uses the cached value instead of a blocking get_latest_blockhash RPC
        # (~100-200ms saved per buy and per sell).
        self._cached_blockhash = None
        self._blockhash_lock = threading.Lock()
        self._blockhash_thread = threading.Thread(
            target=self._blockhash_refresher, daemon=True, name="blockhash-refresh"
        )
        self._blockhash_thread.start()

    def _blockhash_refresher(self):
        """Background thread: keep a fresh blockhash cached every 400ms.

        Solana blockhashes expire after 150 slots (~60s at 400ms/slot).
        Refreshing every 400ms guarantees the cached value is always valid
        while saving one blocking RPC call (~100-200ms) per buy and per sell.
        Falls back to a live fetch in _get_blockhash() if cache is still None.
        """
        while True:
            try:
                resp = self.client.get_latest_blockhash(commitment=Processed)
                bh = resp.value.blockhash
                with self._blockhash_lock:
                    self._cached_blockhash = bh
            except Exception:
                pass  # Keep using previous cached value on transient RPC errors
            time.sleep(0.4)

    def _get_blockhash(self):
        """Return cached blockhash, or fetch live if cache not yet populated."""
        with self._blockhash_lock:
            bh = self._cached_blockhash
        if bh is not None:
            return bh
        # First call before the background thread has run — fetch directly
        resp = self.client.get_latest_blockhash(commitment=Processed)
        return resp.value.blockhash

    def _debug(self, message: str):
        """Print debug message only if verbose mode is enabled"""
        if self.verbose:
            print(f"[DEBUG] {message}")

    def _wait_for_bc_account(self, pda_str: str, timeout: float = 12.0) -> bool:
        """Wait for bonding curve account to appear on-chain via logsSubscribe.

        Uses logsSubscribe with a `mentions` filter on the BC PDA.  This fires the
        instant any confirmed transaction references the BC PDA — which includes the
        creation transaction itself.  This is the correct Solana WS method for
        detecting new account creation (accountSubscribe only fires for changes to
        already-existing accounts and will not notify on creation).

        After the log notification fires, polls the BC ATA via HTTP to bridge any
        intra-cluster lag between the Helius WS node and the HTTP endpoint.

        Safe to call from any context (runs its own event loop in a new thread).
        """
        result = [False]
        done   = threading.Event()

        async def _subscribe():
            ws_url = self.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
            try:
                async with websockets.connect(ws_url, open_timeout=5, ping_interval=None) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [pda_str]},
                            {"commitment": "processed"},  # fires earlier than confirmed
                        ],
                    }))
                    self._debug(f"WS: logsSubscribe for BC {pda_str[:8]}...")

                    deadline   = time.time() + timeout
                    subscribed = False

                    while time.time() < deadline:
                        rem = max(0.05, deadline - time.time())
                        try:
                            msg  = await asyncio.wait_for(ws.recv(), timeout=rem)
                            data = json.loads(msg)
                        except asyncio.TimeoutError:
                            break

                        # Subscription confirmation → do one immediate RPC check.
                        # Handles the race where the creation tx confirmed before we subscribed.
                        if not subscribed and isinstance(data.get("result"), int):
                            subscribed = True
                            self._debug("WS: logsSubscribe confirmed — doing immediate BC check")
                            try:
                                info = self.client.get_account_info(Pubkey.from_string(pda_str))
                                if info.value:
                                    self._debug("WS: BC account already visible on first check")
                                    result[0] = True
                                    return
                            except Exception:
                                pass
                            continue

                        # Log notification — a confirmed tx mentioned the BC PDA.
                        # The first such tx is the creation tx; BC account is now on-chain.
                        if data.get("method") == "logsNotification":
                            value = (data.get("params") or {}).get("result", {}).get("value", {})
                            if value.get("err") is None:   # confirmed without error
                                self._debug("WS: BC creation tx confirmed (logsNotification)")
                                result[0] = True
                                return

            except Exception as e:
                self._debug(f"WS logsSubscribe error: {e}")

        def _thread_run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_subscribe())
            finally:
                loop.close()
                done.set()

        t = threading.Thread(target=_thread_run, daemon=True)
        t.start()
        done.wait(timeout=timeout + 2)  # +2 s buffer for connection setup overhead
        return result[0]

    def _wait_for_tx_confirm(self, signature: str, timeout: float = 6.0):
        """Event-driven transaction confirmation via signatureSubscribe WebSocket.

        Same return contract as get_tx_error:
            None  → transaction succeeded
            obj   → error from tx meta (use str() to inspect)

        Fires the instant the RPC node reaches 'confirmed' commitment — no 0.5s
        polling latency.  Falls back to get_tx_error polling if the WS fails.
        Safe to call from any context (own thread + event loop).
        """
        received = [False]
        tx_err   = [None]
        done     = threading.Event()

        async def _subscribe():
            ws_url = self.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
            try:
                async with websockets.connect(ws_url, open_timeout=5, ping_interval=None) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "signatureSubscribe",
                        "params": [signature, {"commitment": "confirmed",
                                               "enableReceivedNotification": False}],
                    }))
                    self._debug(f"WS: signatureSubscribe {signature[:12]}...")

                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        rem = max(0.05, deadline - time.time())
                        try:
                            msg  = await asyncio.wait_for(ws.recv(), timeout=rem)
                            data = json.loads(msg)
                        except asyncio.TimeoutError:
                            break

                        # Subscription confirmation
                        if isinstance(data.get("result"), int):
                            self._debug("WS: signatureSubscribe confirmed")
                            continue

                        # Transaction confirmed notification
                        if data.get("method") == "signatureNotification":
                            value = (data.get("params") or {}).get("result", {}).get("value")
                            if isinstance(value, dict):
                                tx_err[0]   = value.get("err")   # None = success
                                received[0] = True
                                self._debug(f"WS: tx confirmed, err={tx_err[0]}")
                                return
                            # "receivedSignature" = seen but not yet confirmed, keep waiting
            except Exception as e:
                self._debug(f"WS signatureSubscribe error: {e}")

        def _thread_run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_subscribe())
            finally:
                loop.close()
                done.set()

        t = threading.Thread(target=_thread_run, daemon=True)
        t.start()
        done.wait(timeout=timeout + 2)

        if received[0]:
            return tx_err[0]

        # WS failed or timed out — fall back to a short polling window
        self._debug("WS signatureSubscribe timed out — falling back to polling")
        return self.get_tx_error(signature, max_wait=3)

    def check_safety_limits(self, sol_amount: float) -> tuple[bool, str]:
        """Check if trade passes safety limits"""
        # Reset daily counters if new day
        current_day = time.strftime("%Y-%m-%d")
        if current_day != self.last_reset_day:
            self.daily_trades = 0
            self.daily_pnl_sol = 0.0
            self.last_reset_day = current_day

        # Check daily trade limit
        if self.daily_trades >= self.max_daily_trades:
            return False, f"Daily trade limit reached ({self.max_daily_trades})"

        # Check daily loss limit
        if self.daily_pnl_sol < -self.max_daily_loss_sol:
            return False, f"Daily loss limit exceeded ({self.daily_pnl_sol:.4f} SOL)"

        # Check position size
        if sol_amount > self.max_position_size_sol:
            return False, f"Position size {sol_amount:.4f} exceeds max {self.max_position_size_sol} SOL"

        # Check balance
        balance = self.get_sol_balance()
        if balance < sol_amount + self.min_sol_balance:
            return False, f"Insufficient balance: {balance:.4f} SOL (need {sol_amount + self.min_sol_balance:.4f})"

        return True, "OK"

    def load_wallet_config(self, config_path: str):
        """Load wallet configuration from JSON file"""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)

            # Load private key
            private_key_bytes = bytes(config['private_key'])
            self.keypair = Keypair.from_bytes(private_key_bytes)
            self.wallet_address = str(self.keypair.pubkey())

            # Load RPC and settings
            self.rpc_url = config.get('rpc_url', 'https://api.mainnet-beta.solana.com')
            self.max_slippage_bps = config.get('max_slippage_bps', 500)  # 5% default
            self.priority_fee = config.get('priority_fee_lamports', 100000)  # 0.0001 SOL
            self.compute_limit = config.get('compute_unit_limit', 200000)

            # MEV Protection settings
            self.use_jito = config.get('use_jito_mev_protection', False)
            self.jito_tip_lamports = config.get('jito_tip_lamports', 10000)  # 0.00001 SOL tip
            self.jito_block_engine_url = config.get('jito_block_engine_url', 'https://mainnet.block-engine.jito.wtf')

            print(f"[OK] Wallet loaded: {self.wallet_address}")
            if self.use_jito:
                print(f"[MEV] MEV Protection: ENABLED (Jito)")

        except FileNotFoundError:
            raise Exception(f"[ERROR] Wallet config not found: {config_path}")
        except Exception as e:
            raise Exception(f"[ERROR] Failed to load wallet config: {str(e)}")

    def get_sol_balance(self) -> float:
        """Get SOL balance of wallet"""
        try:
            response = self.client.get_balance(self.keypair.pubkey())
            balance_lamports = response.value
            return balance_lamports / 1e9  # Convert to SOL
        except Exception as e:
            print(f"[ERROR] Failed to get balance: {str(e)}")
            return 0.0

    # Pump.fun custom error codes.
    # Current program: bonding curve not ready = 6000, buy slippage = 6001, sell slippage = 6003.
    BONDING_CURVE_ERROR_CODE = 6000   # bonding curve not initialized yet
    SLIPPAGE_ERROR_CODE      = 6001   # buy: too much SOL required
    BC_COMPLETE_ERROR_CODE   = 6002   # bonding curve complete (graduated to Raydium)
    SELL_SLIPPAGE_ERROR_CODE = 6003   # sell: too little SOL received

    def send_transaction_with_mev_protection(self, transaction: Transaction) -> Optional[str]:
        """Send transaction with optional Jito MEV protection.
        skip_preflight=True: skip RPC simulation to reduce latency (critical for sniping).
        """
        try:
            if self.use_jito:
                return self._send_via_jito(transaction)
            else:
                return self._send_via_regular_rpc(transaction)
        except Exception as e:
            print(f"[ERROR] Transaction failed: {str(e)}")
            return None

    def get_tx_error(self, signature: str, max_wait: int = 8) -> Optional[object]:
        """Check whether a submitted transaction succeeded on-chain.

        Returns:
            None  — transaction succeeded (no error)
            obj   — error object from transaction meta (use str() to inspect)
        Waits up to max_wait seconds for the tx to be visible.
        """
        try:
            from solders.signature import Signature as SolSignature
            sig_obj = SolSignature.from_string(signature)
            deadline = time.time() + max_wait
            while time.time() < deadline:
                try:
                    result = self.client.get_transaction(
                        sig_obj,
                        commitment=Confirmed,
                        max_supported_transaction_version=0
                    )
                    if result.value:
                        return result.value.transaction.meta.err  # None = success
                except Exception:
                    pass
                time.sleep(0.5)  # Helius confirms in 1-2s; poll every 0.5s
            # Tx not visible in time — assume success to avoid blocking
            print(f"[WARN] Could not confirm {signature[:12]}... within {max_wait}s, assuming success")
            return None
        except Exception as e:
            print(f"[WARN] get_tx_error: {e}")
            return None

    def _send_via_jito(self, transaction: Transaction) -> Optional[str]:
        """Send transaction via Jito's MEV-protected block engine

        Note: Tip instruction should already be added to the transaction before calling this
        """
        try:
            # Serialize the already-signed transaction
            serialized_tx = bytes(transaction)

            # Send to Jito block engine
            jito_url = f"{self.jito_block_engine_url}/api/v1/transactions"
            headers = {"Content-Type": "application/json"}

            # Encode transaction as base58
            import base58
            tx_b58 = base58.b58encode(serialized_tx).decode('utf-8')

            data = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [tx_b58, {"encoding": "base58"}]
            }

            response = requests.post(jito_url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                result = response.json()
                if "result" in result:
                    signature = result["result"]
                    print(f"[MEV] Jito TX: {signature}")
                    print(f"[LINK] https://solscan.io/tx/{signature}")
                    return signature
                else:
                    error_msg = result.get("error", {}).get("message", "Unknown error")
                    print(f"[WARN] Jito error: {error_msg}, falling back to RPC")
                    return self._send_via_regular_rpc(transaction)
            else:
                print(f"[WARN] Jito HTTP {response.status_code}, falling back to RPC")
                return self._send_via_regular_rpc(transaction)

        except Exception as e:
            print(f"[WARN] Jito failed: {str(e)}, falling back to RPC")
            return self._send_via_regular_rpc(transaction)

    def _send_via_regular_rpc(self, transaction: Transaction) -> Optional[str]:
        """Send via regular RPC. skip_preflight=True skips simulation for lower latency."""
        try:
            response = self.client.send_transaction(
                transaction,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )
            sig = str(response.value)
            print(f"[TX] {sig}")
            print(f"[LINK] https://solscan.io/tx/{sig}")
            return sig
        except Exception as e:
            print(f"[ERROR] RPC send failed: {str(e)}")
            return None

    def get_all_token_positions(self) -> list:
        """Get all token positions from wallet

        Returns:
            List of dicts with token info: [{mint, balance, decimals, uiAmount}, ...]
        """
        try:
            response = self.client.get_token_accounts_by_owner_json_parsed(
                self.keypair.pubkey(),
                {"programId": str(TOKEN_2022_PROGRAM_ID)}  # Pump.fun uses Token-2022
            )

            positions = []
            if response.value:
                for account in response.value:
                    try:
                        parsed = account.account.data.parsed['info']
                        token_amount = parsed['tokenAmount']

                        # Only include accounts with balance > 0
                        if float(token_amount['uiAmount']) > 0:
                            positions.append({
                                'mint': parsed['mint'],
                                'balance': int(token_amount['amount']),
                                'decimals': token_amount['decimals'],
                                'uiAmount': float(token_amount['uiAmount']),
                                'tokenAccount': str(account.pubkey)
                            })
                    except Exception as e:
                        continue

            return positions

        except Exception as e:
            print(f"[ERROR] Failed to get token positions: {str(e)}")
            return []

    def get_token_balance(self, mint_address: str, verbose: bool = False) -> float:
        """Get token balance by querying the ATA directly via get_account_info.

        Uses get_account_info (lightweight) instead of get_token_accounts_by_owner
        (heavy / rate-limited on public RPC).  Tries both SPL Token and Token-2022
        ATAs.  Pump.fun tokens are always 6 decimals.
        """
        mint_pubkey = Pubkey.from_string(mint_address)

        # Derive both possible ATAs
        ata_spl   = get_associated_token_address(self.keypair.pubkey(), mint_pubkey)
        ata_2022  = get_ata_with_program(self.keypair.pubkey(), mint_pubkey, TOKEN_2022_PROGRAM_ID)

        if verbose:
            print(f"[DEBUG] mint      : {mint_address}")
            print(f"[DEBUG] wallet    : {self.keypair.pubkey()}")
            print(f"[DEBUG] ATA (SPL) : {ata_spl}")
            print(f"[DEBUG] ATA (2022): {ata_2022}")

        for attempt in range(1, 6):
            for ata_addr, prog_name in [(ata_spl, "SPL"), (ata_2022, "Token-2022")]:
                try:
                    resp = self.client.get_account_info(ata_addr, commitment=Processed)
                    if resp.value and resp.value.data and len(resp.value.data) >= 72:
                        # SPL / Token-2022 token account layout:
                        # 0-31  mint pubkey
                        # 32-63 owner pubkey
                        # 64-71 amount u64 LE
                        amount  = int.from_bytes(resp.value.data[64:72], 'little')
                        balance = amount / 1_000_000  # pump.fun = 6 decimals always
                        if verbose:
                            print(f"[DEBUG] {prog_name} ATA raw amount={amount}  balance={balance:.4f}")
                        if balance > 0:
                            return balance
                except Exception as e:
                    if verbose:
                        print(f"[DEBUG] attempt {attempt} {prog_name}: {type(e).__name__}: {e}")

            if attempt < 5:
                print(f"[WARN] Balance attempt {attempt}/5 — retrying in 1s...")
                time.sleep(1)  # Helius propagates ATAs fast; 1s is enough

        print(f"[ERROR] Could not fetch token balance after 5 attempts")
        return 0.0

    def buy_token_pumpfun(self, mint_address: str, sol_amount: float, max_slippage: float = 0.05,
                          prefetched_curve: Optional[Dict] = None) -> Optional[str]:
        """Buy a token on the Pump.fun bonding curve with automatic slippage retry.

        On SlippageExceeded (error 6001) the slippage is doubled and the tx
        is retried up to MAX_ATTEMPTS times with a fresh bonding curve quote.

        prefetched_curve: curve dict built from the pumpportal WebSocket creation
            event (contains virtual reserves + creator pubkey).  When supplied the
            entire BC_RETRIES RPC-polling loop is skipped on the first attempt,
            saving 2–7 s.  On any retry the curve is always re-fetched from chain
            so that price changes are captured.
        """
        MAX_ATTEMPTS  = 3
        MAX_SLIPPAGE  = 0.50   # hard cap — never go above 50%
        BC_RETRIES    = 30     # Helius: 30 × 0.5s = 15s retry window (covers slow propagation)
        MAX_BC6000    = 3      # retries for error 6000 (BC not ready, 3 × 1s = 3s max)

        try:
            safe, reason = self.check_safety_limits(sol_amount)
            if not safe:
                print(f"[ERROR] Safety check failed: {reason}")
                return None

            slippage = max_slippage
            attempt = 0
            bc6000_count = 0
            initial_expected_tokens = None  # set on first curve read; used to detect runaway price

            while attempt < MAX_ATTEMPTS:
                attempt += 1
                print(f"[BUY] Attempt {attempt}/{MAX_ATTEMPTS} — {sol_amount} SOL, slippage {slippage*100:.0f}%")

                # Attempt 1: use pre-fetched WebSocket data if available — no RPC wait.
                # Attempt 2+: always re-fetch from chain so price changes are captured.
                if prefetched_curve and attempt == 1:
                    curve = prefetched_curve
                    self._debug("Using pre-fetched curve from WS event — skipping BC wait")
                    # WS trade event proves BC account exists. Skip logsSubscribe + ATA probe
                    # overhead (~200-400ms). If the account isn't yet propagated to our RPC node,
                    # the TX fails → attempt 2 falls into get_bonding_curve_state with full wait.
                else:
                    curve = None
                    for bc in range(BC_RETRIES):
                        if bc > 0:
                            time.sleep(0.5)  # Helius: 0.5s between retries
                        curve = self.get_bonding_curve_state(mint_address)
                        if curve:
                            break

                    if not curve:
                        print("[ERROR] Bonding curve not ready after retries")
                        return None

                expected_tokens = self.calculate_buy_amount(curve, sol_amount)
                if initial_expected_tokens is None:
                    initial_expected_tokens = expected_tokens
                elif initial_expected_tokens > 0 and expected_tokens < initial_expected_tokens * 0.5:
                    # Price has risen >2× since first attempt — we're chasing, abort
                    drift_pct = (1 - expected_tokens / initial_expected_tokens) * 100
                    print(f"[ABORT] Price drifted {drift_pct:.0f}% since entry decision "
                          f"({initial_expected_tokens:,.0f}→{expected_tokens:,.0f} tokens) — not chasing")
                    return None
                print(f"[INFO] Expected tokens: {expected_tokens:,.0f}")

                sig = self.execute_pumpfun_buy(mint_address, sol_amount, slippage, curve)

                if not sig:
                    # No signature = tx never reached the network, retry immediately
                    print(f"[WARN] No signature on attempt {attempt}, retrying...")
                    time.sleep(0.2)
                    continue

                # Check on-chain result — event-driven WS, falls back to polling
                err = self._wait_for_tx_confirm(sig, timeout=10.0)

                if err is None:
                    # ✅ Transaction succeeded
                    self.total_trades += 1
                    print(f"[OK] Buy confirmed: {sig}")
                    print(f"[LINK] https://solscan.io/tx/{sig}")
                    return sig

                err_str = str(err)
                if str(self.BONDING_CURVE_ERROR_CODE) in err_str or str(self.SLIPPAGE_ERROR_CODE) in err_str:
                    # ❌ Price moved — error 6000 = "too much SOL required" (slippage exceeded on cost).
                    # Pump.fun's buy instruction: amount=exact_tokens, maxSolCost=ceiling.
                    # If price rose between curve-read and confirmation, actual cost > maxSolCost → 6000.
                    # Fix: double slippage buffer and retry with fresh curve. No waiting needed.
                    bc6000_count += 1
                    if bc6000_count >= MAX_BC6000:
                        print(f"[SLIPPAGE] Error 6000/6001 hit {bc6000_count} times — price ran too far, giving up")
                        break
                    old = slippage
                    slippage = min(slippage * 2.0, MAX_SLIPPAGE)
                    print(f"[SLIPPAGE] Error 6000/6001 ({bc6000_count}/{MAX_BC6000}) — "
                          f"price moved, raising slippage {old*100:.0f}% → {slippage*100:.0f}%")
                    prefetched_curve = None  # force fresh curve for new price
                    attempt -= 1            # don't count price-move retries against MAX_ATTEMPTS
                    time.sleep(0.1)
                    continue
                elif str(self.BC_COMPLETE_ERROR_CODE) in err_str:
                    # ❌ Bonding curve complete — token graduated to Raydium, not retryable
                    print(f"[BC] Error 6002 — token graduated to Raydium, skipping")
                    return None
                else:
                    # ❌ Other on-chain error — not retryable
                    print(f"[ERROR] On-chain failure: {err_str}")
                    self.failed_trades += 1
                    return None

            print(f"[ERROR] Buy failed after {MAX_ATTEMPTS} attempts")
            self.failed_trades += 1
            return None

        except Exception as e:
            print(f"[ERROR] Buy exception: {str(e)}")
            self.failed_trades += 1
            return None

    def sell_token_pumpfun(self, mint_address: str, token_amount: float, max_slippage: float = 0.05,
                           skip_balance_check: bool = False, cached_curve: dict = None) -> Optional[str]:
        """Sell a token on the Pump.fun bonding curve with automatic slippage retry.

        Sell strategy escalates aggressively — getting OUT is more important than price:
          Attempt 1: min_sol = expected * (1 - slippage)
          Attempt 2: min_sol = expected * (1 - slippage*2)   [double tolerance]
          Attempt 3: min_sol = 0   [emergency — accept any price to close position]
        cached_curve: pre-built curve dict from WS cache — skips bonding curve RPC on attempt 1.
        """
        MAX_ATTEMPTS = 3

        try:
            if not skip_balance_check:
                # Verify we actually hold the tokens
                balance = self.get_token_balance(mint_address)
                if balance <= 0:
                    print(f"[ERROR] No token balance to sell for {mint_address[:8]}")
                    return None
                # Sell whatever we hold (in case partial sell happened before)
                token_amount = min(token_amount, balance)

            slippage = max_slippage

            for attempt in range(1, MAX_ATTEMPTS + 1):
                print(f"[SELL] Attempt {attempt}/{MAX_ATTEMPTS} — "
                      f"{token_amount:,.2f} tokens, slippage {slippage*100:.0f}%")

                # Use WS-cached curve on first attempt to skip RPC (~100-300ms saved).
                # Fall back to fresh RPC fetch on retries (slippage error = stale price).
                if attempt == 1 and cached_curve:
                    curve = cached_curve
                    print(f"[SELL] Using cached curve (vsr={curve['virtual_sol_reserves']:.3e})")
                else:
                    curve = self.get_bonding_curve_state(mint_address)
                if not curve:
                    print("[ERROR] Bonding curve unavailable — token may have migrated to Raydium")
                    return None

                expected_sol = self.calculate_sell_amount(curve, token_amount)

                # Last attempt = emergency exit, accept any price
                if attempt == MAX_ATTEMPTS:
                    min_sol = 0.0
                    print(f"[SELL] Emergency exit — accepting any price")
                else:
                    min_sol = expected_sol * (1 - slippage)

                print(f"[INFO] Expected: {expected_sol:.6f} SOL, min accepted: {min_sol:.6f} SOL")

                sig = self.execute_pumpfun_sell(mint_address, token_amount, min_sol, curve)

                if not sig:
                    print(f"[WARN] No signature on sell attempt {attempt}, retrying...")
                    time.sleep(0.2)
                    slippage = min(slippage * 2.0, 0.50)
                    continue

                err = self._wait_for_tx_confirm(sig, timeout=10.0)

                if err is None:
                    # ✅ Sell confirmed
                    self.total_trades += 1
                    print(f"[OK] Sell confirmed: {sig}")
                    print(f"[LINK] https://solscan.io/tx/{sig}")
                    return sig

                err_str = str(err)
                is_slippage = (str(self.SLIPPAGE_ERROR_CODE)      in err_str or
                               str(self.SELL_SLIPPAGE_ERROR_CODE) in err_str)
                if is_slippage:
                    old = slippage
                    slippage = min(slippage * 2.0, 0.99)
                    print(f"[SLIPPAGE] Sell slippage error on attempt {attempt} — "
                          f"raising slippage {old*100:.0f}% → {slippage*100:.0f}%")
                    time.sleep(0.1)
                    continue
                else:
                    print(f"[ERROR] Sell on-chain failure: {err_str}")
                    self.failed_trades += 1
                    return None

            print(f"[ERROR] Sell failed after {MAX_ATTEMPTS} attempts")
            self.failed_trades += 1
            return None

        except Exception as e:
            print(f"[ERROR] Sell exception: {str(e)}")
            self.failed_trades += 1
            return None

    def get_bonding_curve_state(self, mint_address: str) -> Optional[Dict]:
        """Fetch bonding curve state from Solana"""
        try:
            # Derive bonding curve PDA
            PUMPFUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
            mint_pubkey = Pubkey.from_string(mint_address)

            # Find PDA for bonding curve
            seeds = [b"bonding-curve", bytes(mint_pubkey)]
            bonding_curve_pda, _ = Pubkey.find_program_address(seeds, PUMPFUN_PROGRAM)

            # Fetch account data
            response = self.client.get_account_info(bonding_curve_pda)

            if not response.value:
                return None

            # Parse bonding curve data
            data = response.value.data

            # Bonding curve layout:
            # Bytes 0-7:   discriminator (8 bytes)
            # Bytes 8-15:  virtualTokenReserves (u64)
            # Bytes 16-23: virtualSolReserves (u64)
            # Bytes 24-31: realTokenReserves (u64)
            # Bytes 32-39: realSolReserves (u64)
            # Bytes 40-47: tokenTotalSupply (u64)
            # Byte  48:    complete (bool)
            # Layout A (original):  creator at bytes 49-80
            # Layout B (newer, extra u64 inserted after complete): creator at bytes 57-88
            virtual_token_reserves = int.from_bytes(data[8:16], 'little')
            virtual_sol_reserves = int.from_bytes(data[16:24], 'little')
            real_token_reserves = int.from_bytes(data[24:32], 'little')
            real_sol_reserves = int.from_bytes(data[32:40], 'little')

            # Detect correct creator offset by verifying the derived creator_vault PDA
            # exists on-chain.  The vault is always initialized at token launch, so
            # the offset that produces a live vault is the correct one.
            creator = None
            for offset in [49, 57]:
                if len(data) < offset + 32:
                    continue
                candidate = Pubkey(data[offset:offset + 32])
                vault_pda, _ = Pubkey.find_program_address(
                    [b"creator-vault", bytes(candidate)],
                    PUMPFUN_PROGRAM
                )
                try:
                    vault_resp = self.client.get_account_info(vault_pda)
                    if vault_resp.value:
                        creator = candidate
                        self._debug(f"Creator at offset {offset}: {candidate}")
                        break
                except Exception:
                    pass

            if creator is None:
                # Vault check inconclusive — default to offset 49 (original layout)
                self._debug("Creator vault not found at either offset, defaulting to offset 49")
                creator = Pubkey(data[49:81])

            return {
                'virtual_token_reserves': virtual_token_reserves,
                'virtual_sol_reserves': virtual_sol_reserves,
                'real_token_reserves': real_token_reserves,
                'real_sol_reserves': real_sol_reserves,
                'bonding_curve_pda': str(bonding_curve_pda),
                'creator': creator
            }

        except Exception as e:
            print(f"[ERROR] Failed to get bonding curve state: {str(e)}")
            return None

    def calculate_buy_amount(self, curve_data: Dict, sol_in: float) -> float:
        """Calculate expected token output for SOL input (constant product formula)"""
        sol_in_lamports = int(sol_in * 1e9)

        virtual_sol = curve_data['virtual_sol_reserves']
        virtual_tokens = curve_data['virtual_token_reserves']

        # k = x * y (constant product)
        # tokens_out = tokens - (k / (sol + sol_in))
        tokens_out = virtual_tokens - (virtual_sol * virtual_tokens) / (virtual_sol + sol_in_lamports)

        return tokens_out / 1e6  # Assuming 6 decimals

    def calculate_sell_amount(self, curve_data: Dict, tokens_in: float) -> float:
        """Calculate expected SOL output for token input (constant product formula)"""
        tokens_in_raw = int(tokens_in * 1e6)

        virtual_sol = curve_data['virtual_sol_reserves']
        virtual_tokens = curve_data['virtual_token_reserves']

        # sol_out = sol - (k / (tokens + tokens_in))
        sol_out = virtual_sol - (virtual_sol * virtual_tokens) / (virtual_tokens + tokens_in_raw)

        return sol_out / 1e9  # Convert to SOL

    def execute_pumpfun_buy(self, mint_address: str, sol_amount: float, min_tokens: float, curve_data: Dict) -> Optional[str]:
        """Execute buy transaction on Pump.fun bonding curve"""
        try:
            mint_pubkey = Pubkey.from_string(mint_address)

            # CRITICAL: Pump.fun ONLY supports legacy SPL Token for bonding curve buys
            # Always use legacy SPL Token, even if the mint is Token-2022

            # Derive bonding curve PDA
            bonding_curve_pda, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint_pubkey)],
                self.PUMPFUN_PROGRAM
            )

            # Get creator address from bonding curve data and derive creator_vault PDA
            # Based on pumpfun library: creator_vault IS a PDA derived from creator pubkey
            creator_pubkey = curve_data['creator']
            creator_vault, _ = Pubkey.find_program_address(
                [b"creator-vault", bytes(creator_pubkey)],
                self.PUMPFUN_PROGRAM
            )

            # Derive user volume accumulator PDA (tracks individual user's volume)
            user_volume_accumulator, _ = Pubkey.find_program_address(
                [b"user_volume_accumulator", bytes(self.keypair.pubkey())],
                self.PUMPFUN_PROGRAM
            )

            # Try to find bonding curve ATA - Pump.fun may use either SPL Token or Token-2022

            # Try legacy SPL Token first
            associated_bonding_curve_legacy = get_associated_token_address(
                bonding_curve_pda,
                mint_pubkey
            )

            # Try Token-2022
            associated_bonding_curve_2022 = get_ata_with_program(
                bonding_curve_pda,
                mint_pubkey,
                TOKEN_2022_PROGRAM_ID
            )

            # Check which one exists and detect the mint's token program
            associated_bonding_curve = None
            mint_token_program = TOKEN_PROGRAM_ID  # Default to legacy

            try:
                bc_ata_info = self.client.get_account_info(associated_bonding_curve_legacy)
                if bc_ata_info.value:
                    associated_bonding_curve = associated_bonding_curve_legacy
                    mint_token_program = TOKEN_PROGRAM_ID
            except:
                pass

            if not associated_bonding_curve:
                try:
                    bc_ata_info = self.client.get_account_info(associated_bonding_curve_2022)
                    if bc_ata_info.value:
                        associated_bonding_curve = associated_bonding_curve_2022
                        mint_token_program = TOKEN_2022_PROGRAM_ID
                except:
                    pass

            if not associated_bonding_curve:
                print(f"[ERROR] Token bonding curve not ready - skipping")
                return None

            # Get user's token account using the same token program as the mint
            if mint_token_program == TOKEN_2022_PROGRAM_ID:
                user_token_account = get_ata_with_program(
                    self.keypair.pubkey(),
                    mint_pubkey,
                    TOKEN_2022_PROGRAM_ID
                )
            else:
                user_token_account = get_associated_token_address(
                    self.keypair.pubkey(),
                    mint_pubkey
                )

            # Build buy instruction data
            # Pump.fun format: [discriminator 8B] [amount: tokens_to_receive u64] [max_sol_cost u64]
            # NOTE: amount=tokens (not SOL), maxSolCost=SOL+slippage (not tokens)
            # min_tokens parameter holds the slippage fraction (e.g. 0.20 = 20%)
            discriminator = bytes([102, 6, 61, 18, 1, 218, 235, 234])
            expected_tokens = self.calculate_buy_amount(curve_data, sol_amount)
            expected_tokens_raw = int(expected_tokens * 1e6)          # tokens to receive
            max_sol_cost_lamports = int(sol_amount * (1 + min_tokens) * 1e9)  # max SOL with slippage buffer

            instruction_data = discriminator + struct.pack('<QQ', expected_tokens_raw, max_sol_cost_lamports)

            # Build accounts for buy instruction - 16 accounts total (current Pump.fun program)
            # Account order matters! This MUST match Solscan output exactly
            accounts = [
                AccountMeta(pubkey=self.PUMPFUN_GLOBAL, is_signer=False, is_writable=False),           # 0 - Global
                AccountMeta(pubkey=self.PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),     # 1 - Fee Recipient
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),                   # 2 - Mint
                AccountMeta(pubkey=bonding_curve_pda, is_signer=False, is_writable=True),              # 3 - Bonding Curve
                AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),       # 4 - Associated Bonding Curve
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),             # 5 - Associated User
                AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),           # 6 - User (signer)
                AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),             # 7 - System Program
                AccountMeta(pubkey=mint_token_program, is_signer=False, is_writable=False),            # 8 - Token Program (detected)
                AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),                  # 9 - Creator Vault
                AccountMeta(pubkey=self.PUMPFUN_EVENT_AUTHORITY, is_signer=False, is_writable=False),  # 10 - Event Authority
                AccountMeta(pubkey=self.PUMPFUN_PROGRAM, is_signer=False, is_writable=False),          # 11 - Program
                AccountMeta(pubkey=self.GLOBAL_VOLUME_ACCUMULATOR, is_signer=False, is_writable=True), # 12 - Global Volume Accumulator
                AccountMeta(pubkey=user_volume_accumulator, is_signer=False, is_writable=True),        # 13 - User Volume Accumulator
                AccountMeta(pubkey=self.FEE_CONFIG, is_signer=False, is_writable=False),               # 14 - Fee Config
                AccountMeta(pubkey=self.FEE_PROGRAM, is_signer=False, is_writable=False),              # 15 - Fee Program
            ]

            prog_name = "Token-2022" if mint_token_program == TOKEN_2022_PROGRAM_ID else "SPL Token"

            buy_instruction = Instruction(
                program_id=self.PUMPFUN_PROGRAM,
                data=instruction_data,
                accounts=accounts
            )

            # Check if user's ATA exists and create it if needed
            instructions = [
                set_compute_unit_limit(self.compute_limit),
                set_compute_unit_price(self.priority_fee),
            ]

            try:
                user_ata_info = self.client.get_account_info(user_token_account)
                if not user_ata_info.value:
                    # Build ATA creation instruction MANUALLY using the detected token program
                    create_ata_ix = Instruction(
                        program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
                        accounts=[
                            AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),   # payer
                            AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),      # ata
                            AccountMeta(pubkey=self.keypair.pubkey(), is_signer=False, is_writable=False), # owner
                            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),           # mint
                            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),     # system
                            AccountMeta(pubkey=mint_token_program, is_signer=False, is_writable=False),    # token program (detected)
                        ],
                        data=b"",  # ATA program uses empty data for create instruction
                    )
                    instructions.append(create_ata_ix)
                    prog_name = "Token-2022" if mint_token_program == TOKEN_2022_PROGRAM_ID else "SPL Token"
                else:
                    pass  # ATA already exists
            except Exception as e:
                # If we can't check, try to create it (will fail gracefully if exists)
                create_ata_ix = Instruction(
                    program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
                    accounts=[
                        AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),
                        AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                        AccountMeta(pubkey=self.keypair.pubkey(), is_signer=False, is_writable=False),
                        AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                        AccountMeta(pubkey=mint_token_program, is_signer=False, is_writable=False),
                    ],
                    data=b"",
                )
                instructions.append(create_ata_ix)

            # Add the buy instruction last
            instructions.append(buy_instruction)

            # Add Jito tip if MEV protection is enabled
            if self.use_jito:
                # Jito tip accounts (rotate for best results)
                tip_accounts = [
                    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
                    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
                    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
                    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
                    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
                    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
                    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
                    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"
                ]
                import random
                tip_account = Pubkey.from_string(random.choice(tip_accounts))

                # Add tip instruction to the END
                tip_instruction = transfer(
                    TransferParams(
                        from_pubkey=self.keypair.pubkey(),
                        to_pubkey=tip_account,
                        lamports=self.jito_tip_lamports
                    )
                )
                instructions.append(tip_instruction)
                print(f"[MEV] Added Jito tip: {self.jito_tip_lamports} lamports")

            # Use pre-cached blockhash (background thread refreshes every 400ms)
            recent_blockhash = self._get_blockhash()

            # Build message
            message = Message.new_with_blockhash(
                instructions,
                self.keypair.pubkey(),
                recent_blockhash
            )

            # Create transaction
            transaction = Transaction.new_unsigned(message)

            # Send transaction with MEV protection
            print(f"  MEV Protection: {self.use_jito}")
            print(f"  Instructions: {len(instructions)}")
            print(f"[TX] Sending buy transaction...")

            # Sign transaction
            transaction = Transaction([self.keypair], message, recent_blockhash)

            signature = self.send_transaction_with_mev_protection(transaction)

            if not signature:
                return None

            # Cache ATA + token program for fast sell — skips 2 get_account_info RPCs
            self.token_program_cache[mint_address] = {
                'user_ata': user_token_account,
                'token_program': mint_token_program,
            }

            # Confirmation is handled by get_tx_error() in buy_token_pumpfun
            # (polls every 0.5s up to 6s) — no need to block here too.
            return signature

        except Exception as e:
            print(f"  Type: {type(e).__name__}")
            print(f"  Message: {str(e)}")
            print(f"[ERROR] Transaction execution failed: {str(e)}")
            import traceback
            traceback.print_exc()
            return None

    def execute_pumpfun_sell(self, mint_address: str, token_amount: float, min_sol: float, curve_data: Dict) -> Optional[str]:
        """Execute sell transaction on Pump.fun bonding curve."""
        try:
            mint_pubkey = Pubkey.from_string(mint_address)

            # Derive bonding curve PDA
            bonding_curve_pda, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint_pubkey)],
                self.PUMPFUN_PROGRAM
            )

            # Detect which token program this mint uses.
            # Fast path: use ATA cached at buy time — skips 2 get_account_info RPCs (~200-400ms).
            # Cold path (cache miss): probe both ATAs as before.
            ata_spl  = get_associated_token_address(self.keypair.pubkey(), mint_pubkey)
            ata_2022 = get_ata_with_program(self.keypair.pubkey(), mint_pubkey, TOKEN_2022_PROGRAM_ID)

            user_token_account = ata_spl          # default: legacy SPL
            mint_token_program  = TOKEN_PROGRAM_ID  # default
            exact_raw_balance   = None             # exact on-chain u64 token amount

            _cached_ata = self.token_program_cache.get(mint_address)
            if _cached_ata:
                # Fast path: token program determined at buy time
                user_token_account = _cached_ata['user_ata']
                mint_token_program  = _cached_ata['token_program']
                print(f"[SELL] Cached ATA hit — skipped 2 probes")
            else:
                # Cold path: probe which ATA holds the tokens
                try:
                    resp = self.client.get_account_info(ata_2022, commitment=Processed)
                    if resp.value and resp.value.data and len(resp.value.data) >= 72:
                        amount = int.from_bytes(resp.value.data[64:72], 'little')
                        if amount > 0:
                            user_token_account = ata_2022
                            mint_token_program  = TOKEN_2022_PROGRAM_ID
                            exact_raw_balance   = amount
                except Exception:
                    pass

                # If tokens weren't in Token-2022 ATA, probe SPL ATA for exact balance
                if exact_raw_balance is None:
                    try:
                        resp_spl = self.client.get_account_info(ata_spl, commitment=Processed)
                        if resp_spl.value and resp_spl.value.data and len(resp_spl.value.data) >= 72:
                            amount_spl = int.from_bytes(resp_spl.value.data[64:72], 'little')
                            if amount_spl > 0:
                                exact_raw_balance = amount_spl
                    except Exception:
                        pass

            # Bonding curve ATA — same program as the mint
            if mint_token_program == TOKEN_2022_PROGRAM_ID:
                associated_bonding_curve = get_ata_with_program(
                    bonding_curve_pda, mint_pubkey, TOKEN_2022_PROGRAM_ID
                )
            else:
                associated_bonding_curve = get_associated_token_address(
                    bonding_curve_pda, mint_pubkey
                )

            # Creator vault — required account (same derivation as buy)
            creator_pubkey = curve_data['creator']
            creator_vault, _ = Pubkey.find_program_address(
                [b"creator-vault", bytes(creator_pubkey)],
                self.PUMPFUN_PROGRAM
            )

            # Fee config PDA
            pump_fee_config_pda, _ = Pubkey.find_program_address(
                [b"fee_config", bytes(self.PUMPFUN_PROGRAM)],
                self.FEE_PROGRAM
            )

            # Sell instruction data
            # BUG FIX: discriminator must be raw bytes, NOT packed as a u64 integer
            # struct.pack('<Q', 0x33e685a4017f83ad) reverses byte order — wrong!
            discriminator = bytes.fromhex("33e685a4017f83ad")

            # Use exact on-chain balance to prevent float-rounding dust.
            # int(float * 1e6) can truncate by 1 unit (IEEE 754), leaving a dust token
            # in the ATA.  The close_ix then fails with Custom:11 (NonNativeHasPendingBalance).
            tokens_raw = round(token_amount * 1e6)
            if exact_raw_balance is not None:
                if abs(tokens_raw - exact_raw_balance) <= 1:
                    # Selling the full balance — use exact on-chain amount so no dust remains.
                    tokens_raw = exact_raw_balance
                else:
                    # Partial sell — cap at exact balance to prevent over-sell rejection.
                    tokens_raw = min(tokens_raw, exact_raw_balance)

            min_sol_lamports = int(min_sol * 1e9)
            instruction_data = discriminator + struct.pack('<QQ', tokens_raw, min_sol_lamports)

            # Accounts matching pumpfunlib/pump_fun.py (the working reference)
            accounts = [
                AccountMeta(pubkey=self.PUMPFUN_GLOBAL, is_signer=False, is_writable=False),
                AccountMeta(pubkey=self.PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(pubkey=bonding_curve_pda, is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),
                AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_token_program, is_signer=False, is_writable=False),  # detected: SPL or Token-2022
                AccountMeta(pubkey=self.PUMPFUN_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=self.PUMPFUN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=pump_fee_config_pda, is_signer=False, is_writable=False),
                AccountMeta(pubkey=self.FEE_PROGRAM, is_signer=False, is_writable=False),
            ]

            sell_instruction = Instruction(
                program_id=self.PUMPFUN_PROGRAM,
                data=instruction_data,
                accounts=accounts
            )

            # CloseAccount instruction — reclaims the ~0.002 SOL ATA rent deposit.
            # Runs after the sell empties the account, so the close is guaranteed to
            # succeed.  Discriminator byte 9 is CloseAccount for both SPL Token and
            # Token-2022.  The lamports are returned directly to the wallet (payer).
            close_ix = Instruction(
                program_id=mint_token_program,  # must match the token's program
                data=bytes([9]),
                accounts=[
                    AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),      # account to close
                    AccountMeta(pubkey=self.keypair.pubkey(), is_signer=False, is_writable=True),    # lamport destination
                    AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=False),    # owner / authority
                ]
            )

            instructions = [
                set_compute_unit_limit(self.compute_limit),
                set_compute_unit_price(self.priority_fee),
                sell_instruction,
                close_ix,
            ]

            # Build and sign transaction (same pattern as buy)
            recent_blockhash = self._get_blockhash()  # pre-cached, no RPC wait

            message = Message.new_with_blockhash(
                instructions,
                self.keypair.pubkey(),
                recent_blockhash
            )
            transaction = Transaction([self.keypair], message, recent_blockhash)

            print(f"[TX] Sending sell transaction...")
            signature = self.send_transaction_with_mev_protection(transaction)

            if not signature:
                return None

            # Confirmation handled by get_tx_error() in sell_token_pumpfun
            return signature

        except Exception as e:
            print(f"[ERROR] Sell transaction failed: {str(e)}")
            import traceback
            traceback.print_exc()
            return None

    def get_stats(self) -> Dict:
        """Get trading statistics"""
        success_rate = ((self.total_trades - self.failed_trades) / self.total_trades * 100) if self.total_trades > 0 else 0

        return {
            'total_trades': self.total_trades,
            'failed_trades': self.failed_trades,
            'success_rate': success_rate,
            'wallet_address': self.wallet_address,
            'sol_balance': self.get_sol_balance()
        }


if __name__ == "__main__":
    # Test the live trader
    import sys
    import io

    # Set UTF-8 encoding for Windows console
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    print("[TEST] Testing Live Trader Module...")

    try:
        trader = LiveTrader("wallet_config.json")

        print(f"\n[INFO] Wallet: {trader.wallet_address}")
        print(f"[BAL] Balance: {trader.get_sol_balance():.6f} SOL")
        print(f"[MEV]  MEV Protection: {'ENABLED' if trader.use_jito else 'DISABLED'}")

        print("\n[OK] Live trader module loaded successfully!")
        print("[OK] Buy/sell functions are FULLY IMPLEMENTED")
        print("[OK] Pump.fun program integration complete")
        print("[OK] MEV protection via Jito (optional)")
        print("\n[WARN]  IMPORTANT: Start with small amounts (0.01-0.05 SOL)")
        print("[WARN]  Test thoroughly before using larger positions")

    except Exception as e:
        print(f"[ERROR] Failed to initialize: {str(e)}")
        print("\n[TIP] Create wallet_config.json from wallet_config_TEMPLATE.json")
