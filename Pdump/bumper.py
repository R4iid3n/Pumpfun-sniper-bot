"""
PDUMP - Pump.fun Token Bumper
Generates 8 sub-wallets that perform tiny buy+sell cycles to bump a token.
"""

import json
import os
import struct
import sys
import threading
import time
import random
import tkinter as tk
from tkinter import scrolledtext
from datetime import datetime
import urllib.request

# Add parent pumpfunlib to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pumpfunlib'))

from solana.rpc.api import Client
from solana.rpc.commitment import Processed, Confirmed
from solana.rpc.types import TxOpts

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price  # type: ignore
from solders.instruction import AccountMeta, Instruction  # type: ignore
from solders.keypair import Keypair  # type: ignore
from solders.message import MessageV0  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.system_program import TransferParams, transfer  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore

from spl.token.instructions import (
    CloseAccountParams,
    close_account,
    create_associated_token_account,
)

from construct import Bytes, Flag, Int64ul, Padding, Struct

from constants import (
    GLOBAL, FEE_RECIPIENT, SYSTEM_PROGRAM, TOKEN_PROGRAM,
    ASSOC_TOKEN_ACC_PROG, EVENT_AUTHORITY, PUMP_FUN_PROGRAM,
    GLOBAL_VOL_ACC, FEE_PROGRAM,
)

TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

BONDING_CURVE_STRUCT = Struct(
    Padding(8),
    "virtualTokenReserves" / Int64ul,
    "virtualSolReserves" / Int64ul,
    "realTokenReserves" / Int64ul,
    "realSolReserves" / Int64ul,
    "tokenTotalSupply" / Int64ul,
    "complete" / Flag,
    "creator" / Bytes(32),
)

SOL_DEC = 1_000_000_000
TOK_DEC = 1_000_000
RENT_EXEMPT_LAMPORTS = 890_880       # min balance for a 0-byte SOL account
ATA_RENT_LAMPORTS = 2_039_280        # rent for a token account (SPL/Token-2022)


def fetch_sol_price():
    """Fetch SOL/USD price from CoinGecko. Returns float or 0 on failure."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return float(data["solana"]["usd"])
    except Exception:
        return 0.0


def fmt_sol_usd(sol_amount, sol_price):
    """Format SOL amount with USD equivalent."""
    if sol_price > 0:
        return f"{sol_amount:.5f} SOL (${sol_amount * sol_price:.2f})"
    return f"{sol_amount:.5f} SOL"


# ─── Helpers ──────────────────────────────────────────────────────────

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'wallet_config.json')
    with open(config_path) as f:
        cfg = json.load(f)
    master_kp = Keypair.from_bytes(bytes(cfg['private_key']))
    client = Client(cfg['rpc_url'], commitment=Processed)
    return cfg, master_kp, client


def get_sol_balance(client, pubkey):
    try:
        return client.get_balance(pubkey, commitment=Processed).value
    except Exception:
        return 0


def get_ata(owner, mint, token_program):
    """Derive ATA for any token program."""
    ata, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOC_TOKEN_ACC_PROG,
    )
    return ata


def detect_token_program(client, mint):
    """Check mint account owner to determine SPL Token vs Token-2022."""
    try:
        info = client.get_account_info(mint)
        if info.value and info.value.owner == TOKEN_2022_PROGRAM:
            return TOKEN_2022_PROGRAM
    except Exception:
        pass
    return TOKEN_PROGRAM


def get_token_balance(client, ata_pubkey):
    """Get token balance from a specific ATA address. Returns (ui_amount, raw_amount)."""
    try:
        resp = client.get_token_account_balance(ata_pubkey, commitment=Processed)
        if resp.value:
            return float(resp.value.ui_amount or 0), int(resp.value.amount)
    except Exception:
        pass
    return 0.0, 0


def get_bonding_curve_state(client, mint_pubkey):
    bonding_curve, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_pubkey)], PUMP_FUN_PROGRAM,
    )
    try:
        info = client.get_account_info(bonding_curve)
        if not info.value:
            return None
        parsed = BONDING_CURVE_STRUCT.parse(info.value.data)
        return {
            'bonding_curve': bonding_curve,
            'virtual_token_reserves': parsed.virtualTokenReserves,
            'virtual_sol_reserves': parsed.virtualSolReserves,
            'complete': parsed.complete,
            'creator': Pubkey.from_bytes(parsed.creator),
        }
    except Exception:
        return None


def sol_for_tokens(sol_spent, sol_reserves, token_reserves):
    new_sol = sol_reserves + sol_spent
    new_tok = (sol_reserves * token_reserves) / new_sol
    return round(token_reserves - new_tok)


def tokens_for_sol(tokens_to_sell, sol_reserves, token_reserves):
    new_tok = token_reserves + tokens_to_sell
    new_sol = (sol_reserves * token_reserves) / new_tok
    return sol_reserves - new_sol


def confirm_txn(client, sig, max_retries=20, interval=2):
    """Returns (success: bool, error_detail: str or None)."""
    for _ in range(max_retries):
        try:
            resp = client.get_transaction(sig, commitment=Confirmed, max_supported_transaction_version=0)
            if resp.value:
                err = resp.value.transaction.meta.err
                if err is None:
                    return True, None
                return False, str(err)
        except Exception:
            pass
        time.sleep(interval)
    return False, "Timeout - not confirmed"


# ─── Buy & Sell ───────────────────────────────────────────────────────

def buy_token(client, payer_kp, mint_pubkey, sol_amount, slippage_bps, cu_limit, cu_price, token_prog):
    """Execute pump.fun buy. Returns (success, message)."""
    try:
        bc = get_bonding_curve_state(client, mint_pubkey)
        if not bc:
            return False, "Bonding curve not found"
        if bc['complete']:
            return False, "Token graduated"

        user = payer_kp.pubkey()
        creator = bc['creator']
        bonding_curve = bc['bonding_curve']
        associated_bc = get_ata(bonding_curve, mint_pubkey, token_prog)
        associated_user = get_ata(user, mint_pubkey, token_prog)
        creator_vault = Pubkey.find_program_address([b'creator-vault', bytes(creator)], PUMP_FUN_PROGRAM)[0]
        user_vol_acc = Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_FUN_PROGRAM)[0]
        fee_config = Pubkey.find_program_address([b"fee_config", bytes(PUMP_FUN_PROGRAM)], FEE_PROGRAM)[0]

        # Check if ATA exists
        ata_info = client.get_account_info(associated_user)
        need_ata = ata_info.value is None

        # Calculate amounts
        v_sol = bc['virtual_sol_reserves'] / SOL_DEC
        v_tok = bc['virtual_token_reserves'] / TOK_DEC
        tokens_out = sol_for_tokens(sol_amount, v_sol, v_tok)
        tokens_raw = int(tokens_out * TOK_DEC)
        slippage_mult = 1 + (slippage_bps / 10000)
        max_sol_cost = int(sol_amount * slippage_mult * SOL_DEC)

        keys = [
            AccountMeta(pubkey=GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_RECIPIENT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_bc, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_user, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=token_prog, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
            AccountMeta(pubkey=EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=GLOBAL_VOL_ACC, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_vol_acc, is_signer=False, is_writable=True),
            AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

        data = bytearray()
        data.extend(bytes.fromhex("66063d1201daebea"))
        data.extend(struct.pack('<Q', tokens_raw))
        data.extend(struct.pack('<Q', max_sol_cost))

        instructions = [
            set_compute_unit_limit(cu_limit),
            set_compute_unit_price(cu_price),
        ]
        if need_ata:
            instructions.append(create_associated_token_account(user, user, mint_pubkey, token_prog))
        instructions.append(Instruction(PUMP_FUN_PROGRAM, bytes(data), keys))

        bh = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(user, instructions, [], bh)
        tx = VersionedTransaction(msg, [payer_kp])
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=True)).value

        ok, err = confirm_txn(client, sig, max_retries=15, interval=1)
        sig_short = str(sig)[:20]
        if ok:
            return True, f"BUY {sol_amount} SOL | sig: {sig_short}..."
        return False, f"BUY {sol_amount} SOL | sig: {sig_short}... | err: {err}"

    except Exception as e:
        return False, f"Buy error: {e}"


def sell_token(client, payer_kp, mint_pubkey, slippage_bps, cu_limit, cu_price, token_prog):
    """Sell 100% of tokens. Returns (success, message)."""
    try:
        bc = get_bonding_curve_state(client, mint_pubkey)
        if not bc:
            return False, "Bonding curve not found"

        user = payer_kp.pubkey()
        creator = bc['creator']
        bonding_curve = bc['bonding_curve']
        associated_bc = get_ata(bonding_curve, mint_pubkey, token_prog)
        associated_user = get_ata(user, mint_pubkey, token_prog)
        creator_vault = Pubkey.find_program_address([b'creator-vault', bytes(creator)], PUMP_FUN_PROGRAM)[0]
        fee_config = Pubkey.find_program_address([b"fee_config", bytes(PUMP_FUN_PROGRAM)], FEE_PROGRAM)[0]

        # Get token balance from ATA
        ui_balance, raw_balance = get_token_balance(client, associated_user)
        if raw_balance <= 0:
            return False, "No tokens to sell"

        # Calculate min SOL output
        v_sol = bc['virtual_sol_reserves'] / SOL_DEC
        v_tok = bc['virtual_token_reserves'] / TOK_DEC
        sol_out = tokens_for_sol(ui_balance, v_sol, v_tok)
        slippage_mult = 1 - (slippage_bps / 10000)
        min_sol = max(0, int(sol_out * slippage_mult * SOL_DEC))

        keys = [
            AccountMeta(pubkey=GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_RECIPIENT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_bc, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_user, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_prog, is_signer=False, is_writable=False),
            AccountMeta(pubkey=EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

        data = bytearray()
        data.extend(bytes.fromhex("33e685a4017f83ad"))
        data.extend(struct.pack('<Q', raw_balance))
        data.extend(struct.pack('<Q', min_sol))

        instructions = [
            set_compute_unit_limit(cu_limit),
            set_compute_unit_price(cu_price),
            Instruction(PUMP_FUN_PROGRAM, bytes(data), keys),
            close_account(CloseAccountParams(token_prog, associated_user, user, user)),
        ]

        bh = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(user, instructions, [], bh)
        tx = VersionedTransaction(msg, [payer_kp])
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=True)).value

        ok, err = confirm_txn(client, sig, max_retries=15, interval=1)
        sig_short = str(sig)[:20]
        if ok:
            return True, f"SELL 100% ({ui_balance:.0f} tokens) | sig: {sig_short}..."
        return False, f"SELL ({ui_balance:.0f} tokens) | sig: {sig_short}... | err: {err}"

    except Exception as e:
        return False, f"Sell error: {e}"


# ─── Fund & Drain ────────────────────────────────────────────────────

def fund_sub_wallets(client, master_kp, wallets, amount_each_lamports):
    """Send SOL from master to each sub-wallet in one batched TX."""
    try:
        instructions = []
        for w in wallets:
            instructions.append(
                transfer(TransferParams(
                    from_pubkey=master_kp.pubkey(),
                    to_pubkey=w.pubkey(),
                    lamports=amount_each_lamports,
                ))
            )
        bh = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(master_kp.pubkey(), instructions, [], bh)
        tx = VersionedTransaction(msg, [master_kp])
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=True)).value
        ok, err = confirm_txn(client, sig, max_retries=15, interval=1)
        if ok:
            return True, f"Funded {len(wallets)} wallets | sig: {str(sig)[:20]}..."
        return False, f"Fund failed | sig: {str(sig)[:20]}... | err: {err}"
    except Exception as e:
        return False, f"Fund error: {e}"


def close_ata_if_exists(client, sub_kp, mint_pubkey, token_prog):
    """Close the sub-wallet's ATA for this mint to reclaim rent. Returns (success, msg)."""
    try:
        user = sub_kp.pubkey()
        ata = get_ata(user, mint_pubkey, token_prog)
        info = client.get_account_info(ata)
        if info.value is None:
            return True, "No ATA"

        # Check if ATA has tokens - if so, we can't close without selling first
        _, raw_bal = get_token_balance(client, ata)
        if raw_bal > 0:
            return False, f"ATA has {raw_bal} tokens (sell first)"

        # ATA exists but is empty → close it to reclaim rent
        ix_close = close_account(CloseAccountParams(token_prog, ata, user, user))
        bh = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(user, [ix_close], [], bh)
        tx = VersionedTransaction(msg, [sub_kp])
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=True)).value
        ok, err = confirm_txn(client, sig, max_retries=10, interval=1)
        if ok:
            return True, f"Closed ATA, reclaimed ~0.00204 SOL"
        return False, f"Close ATA failed: {err}"
    except Exception as e:
        return False, f"Close ATA error: {e}"


def drain_one_wallet(client, sub_kp, master_pubkey):
    """Drain all SOL from sub-wallet back to master."""
    try:
        balance = get_sol_balance(client, sub_kp.pubkey())
        if balance <= 5000:
            return True, "Empty"
        send_amount = balance - 5000
        ix = transfer(TransferParams(
            from_pubkey=sub_kp.pubkey(),
            to_pubkey=master_pubkey,
            lamports=send_amount,
        ))
        bh = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(sub_kp.pubkey(), [ix], [], bh)
        tx = VersionedTransaction(msg, [sub_kp])
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=True)).value
        ok, _ = confirm_txn(client, sig, max_retries=10, interval=1)
        return ok, f"Drained {send_amount / SOL_DEC:.6f} SOL"
    except Exception as e:
        return False, f"Drain error: {e}"


# ─── GUI ──────────────────────────────────────────────────────────────

BG = "#1a1a2e"
BG2 = "#16213e"
BG3 = "#0a0a1a"
FG = "#e0e0e0"
CYAN = "#00d4ff"
GREEN = "#00ff88"
GOLD = "#ffd700"
RED = "#ff4444"
FONT = ("Consolas", 10)
FONT_SM = ("Consolas", 9)
FONT_LG = ("Consolas", 14, "bold")


class BumperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDUMP - Token Bumper")
        self.geometry("780x720")
        self.configure(bg=BG)
        self.resizable(False, False)

        self.running = False
        self.worker_thread = None
        self.sub_wallets = [Keypair() for _ in range(8)]
        self.bump_counts = [0] * 8
        self.wallet_balances = [0] * 8
        self.total_bumps = 0
        self.sol_price = 0.0
        self._active_mint = None
        self._active_token_prog = None
        self._active_slippage = 500
        self._active_cu_sell = 70000
        self._active_cu_price_sell = 1

        try:
            self.cfg, self.master_kp, self.client = load_config()
        except Exception as e:
            tk.Label(self, text=f"Config error: {e}", bg=BG, fg=RED, font=FONT).pack(pady=50)
            return

        self._build_gui()
        self._fetch_price_and_balance()
        self._start_price_refresh()

    def _build_gui(self):
        entry_opts = dict(bg=BG2, fg=FG, insertbackground=FG, font=FONT, relief="flat", bd=2)

        # Title
        tk.Label(self, text="PDUMP - Token Bumper", bg=BG, fg=CYAN, font=FONT_LG).pack(pady=(10, 5))

        # Inputs
        inp = tk.Frame(self, bg=BG)
        inp.pack(fill="x", padx=20, pady=5)

        tk.Label(inp, text="Token Mint:", bg=BG, fg=FG, font=FONT).grid(row=0, column=0, sticky="w", pady=3)
        self.mint_var = tk.StringVar()
        tk.Entry(inp, textvariable=self.mint_var, width=55, **entry_opts).grid(row=0, column=1, columnspan=3, padx=5, pady=3, sticky="w")

        tk.Label(inp, text="Budget SOL:", bg=BG, fg=FG, font=FONT).grid(row=1, column=0, sticky="w", pady=3)
        budget_f = tk.Frame(inp, bg=BG)
        budget_f.grid(row=1, column=1, sticky="w", padx=5, pady=3)
        self.budget_var = tk.StringVar(value="0.015")
        self.budget_var.trace_add("write", lambda *_: self._update_usd_labels())
        tk.Entry(budget_f, textvariable=self.budget_var, width=10, **entry_opts).pack(side="left")
        self.budget_usd_lbl = tk.Label(budget_f, text="", bg=BG, fg=GOLD, font=FONT_SM)
        self.budget_usd_lbl.pack(side="left", padx=4)

        tk.Label(inp, text="Buy Range SOL:", bg=BG, fg=FG, font=FONT).grid(row=1, column=2, sticky="w", padx=(15, 0), pady=3)
        buy_f = tk.Frame(inp, bg=BG)
        buy_f.grid(row=1, column=3, sticky="w", padx=5, pady=3)
        self.buy_min_var = tk.StringVar(value="0.001")
        self.buy_max_var = tk.StringVar(value="0.005")
        self.buy_min_var.trace_add("write", lambda *_: self._update_usd_labels())
        self.buy_max_var.trace_add("write", lambda *_: self._update_usd_labels())
        tk.Entry(buy_f, textvariable=self.buy_min_var, width=6, **entry_opts).pack(side="left")
        tk.Label(buy_f, text="-", bg=BG, fg=FG, font=FONT).pack(side="left", padx=2)
        tk.Entry(buy_f, textvariable=self.buy_max_var, width=6, **entry_opts).pack(side="left")
        self.buy_usd_lbl = tk.Label(buy_f, text="", bg=BG, fg=GOLD, font=FONT_SM)
        self.buy_usd_lbl.pack(side="left", padx=4)

        tk.Label(inp, text="Delay (s):", bg=BG, fg=FG, font=FONT).grid(row=2, column=0, sticky="w", pady=3)
        delay_f = tk.Frame(inp, bg=BG)
        delay_f.grid(row=2, column=1, sticky="w", padx=5, pady=3)
        self.delay_min_var = tk.StringVar(value="2")
        self.delay_max_var = tk.StringVar(value="5")
        tk.Entry(delay_f, textvariable=self.delay_min_var, width=4, **entry_opts).pack(side="left")
        tk.Label(delay_f, text=" - ", bg=BG, fg=FG, font=FONT).pack(side="left")
        tk.Entry(delay_f, textvariable=self.delay_max_var, width=4, **entry_opts).pack(side="left")

        self.master_bal_var = tk.StringVar(value="Master: ... SOL")
        tk.Label(inp, textvariable=self.master_bal_var, bg=BG, fg=GOLD, font=FONT).grid(row=2, column=2, columnspan=2, sticky="e", pady=3)

        # Buttons
        btn_f = tk.Frame(self, bg=BG)
        btn_f.pack(pady=8)
        self.start_btn = tk.Button(
            btn_f, text="  START BUMPING  ", bg="#0f3460", fg=CYAN,
            font=("Consolas", 12, "bold"), relief="flat", padx=15, pady=4,
            activebackground="#1a4a80", activeforeground=CYAN, command=self.start_bumping,
        )
        self.start_btn.pack(side="left", padx=10)
        self.stop_btn = tk.Button(
            btn_f, text="  STOP & DRAIN  ", bg="#3a0e0e", fg=RED,
            font=("Consolas", 12, "bold"), relief="flat", padx=15, pady=4,
            activebackground="#5a1e1e", activeforeground=RED, command=self.stop_bumping, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=10)

        # Status bar
        stat_f = tk.Frame(self, bg=BG2, relief="flat", bd=1)
        stat_f.pack(fill="x", padx=20, pady=3)
        self.status_var = tk.StringVar(value="Idle")
        tk.Label(stat_f, textvariable=self.status_var, bg=BG2, fg=GOLD, font=("Consolas", 10, "bold")).pack(side="left", padx=10, pady=4)
        self.total_var = tk.StringVar(value="Bumps: 0")
        tk.Label(stat_f, textvariable=self.total_var, bg=BG2, fg=GREEN, font=FONT).pack(side="right", padx=10, pady=4)

        # Wallet table
        wf = tk.Frame(self, bg=BG)
        wf.pack(fill="x", padx=20, pady=3)
        tk.Label(wf, text="Sub-Wallets", bg=BG, fg=CYAN, font=("Consolas", 11, "bold")).pack(anchor="w")

        self.wallet_labels = []
        for i in range(8):
            addr = str(self.sub_wallets[i].pubkey())
            short = f"{addr[:6]}...{addr[-4:]}"
            lbl = tk.Label(
                wf, text=f" #{i+1}  {short}  |  0.00000 SOL  |  0 bumps",
                bg=BG2, fg="#a0a0a0", font=FONT_SM, anchor="w", padx=6, pady=2,
            )
            lbl.pack(fill="x", pady=1)
            self.wallet_labels.append(lbl)

        # Log
        tk.Label(self, text="Log", bg=BG, fg=CYAN, font=("Consolas", 11, "bold")).pack(anchor="w", padx=20, pady=(5, 0))
        self.log_box = scrolledtext.ScrolledText(
            self, height=10, bg=BG3, fg="#909090", font=FONT_SM, relief="flat", state="disabled",
        )
        self.log_box.pack(fill="both", padx=20, pady=(0, 10), expand=True)

    # ── Logging & Display ─────────────────────────────────────────────

    def log(self, msg, color=None):
        ts = datetime.now().strftime("%H:%M:%S")
        def _do():
            self.log_box.config(state="normal")
            self.log_box.insert("end", f"[{ts}] {msg}\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _do)

    def _fetch_price_and_balance(self):
        """Fetch SOL price and master balance on startup."""
        def _do():
            self.sol_price = fetch_sol_price()
            bal = get_sol_balance(self.client, self.master_kp.pubkey())
            sol = bal / SOL_DEC
            self.after(0, lambda: self.master_bal_var.set(f"Master: {fmt_sol_usd(sol, self.sol_price)}"))
            self.after(0, self._update_usd_labels)
        threading.Thread(target=_do, daemon=True).start()

    def _start_price_refresh(self):
        """Refresh SOL price every 60 seconds."""
        def _tick():
            self.sol_price = fetch_sol_price()
            self._update_usd_labels()
            self._update_wallet_display()
        def _bg():
            _tick()
        threading.Thread(target=_bg, daemon=True).start()
        self.after(60000, self._start_price_refresh)

    def _show_master_balance(self):
        def _do():
            bal = get_sol_balance(self.client, self.master_kp.pubkey())
            sol = bal / SOL_DEC
            self.after(0, lambda: self.master_bal_var.set(f"Master: {fmt_sol_usd(sol, self.sol_price)}"))
        threading.Thread(target=_do, daemon=True).start()

    def _update_usd_labels(self):
        """Update the USD labels next to budget and buy size inputs."""
        p = self.sol_price
        try:
            b = float(self.budget_var.get())
            self.budget_usd_lbl.config(text=f"(${b * p:.2f})" if p > 0 else "")
        except ValueError:
            self.budget_usd_lbl.config(text="")
        try:
            lo = float(self.buy_min_var.get())
            hi = float(self.buy_max_var.get())
            self.buy_usd_lbl.config(text=f"(${lo * p:.2f}-${hi * p:.2f})" if p > 0 else "")
        except ValueError:
            self.buy_usd_lbl.config(text="")

    def _update_wallet_display(self):
        def _do():
            p = self.sol_price
            total_sol = 0
            for i in range(8):
                addr = str(self.sub_wallets[i].pubkey())
                short = f"{addr[:6]}...{addr[-4:]}"
                bal = self.wallet_balances[i] / SOL_DEC
                total_sol += bal
                usd = f" (${bal * p:.3f})" if p > 0 else ""
                self.wallet_labels[i].config(
                    text=f" #{i+1}  {short}  |  {bal:.5f} SOL{usd}  |  {self.bump_counts[i]} bumps"
                )
            total_usd = f" (${total_sol * p:.2f})" if p > 0 else ""
            self.total_var.set(f"Bumps: {self.total_bumps}  |  Remaining: {total_sol:.5f} SOL{total_usd}")
        self.after(0, _do)

    def _refresh_balances(self):
        for i, w in enumerate(self.sub_wallets):
            self.wallet_balances[i] = get_sol_balance(self.client, w.pubkey())
        self._update_wallet_display()

    # ── Start / Stop ──────────────────────────────────────────────────

    def start_bumping(self):
        mint = self.mint_var.get().strip()
        if not mint or len(mint) < 32:
            self.log("Enter a valid token mint address")
            return
        try:
            budget = float(self.budget_var.get())
            buy_lo = float(self.buy_min_var.get())
            buy_hi = float(self.buy_max_var.get())
            delay_min = float(self.delay_min_var.get())
            delay_max = float(self.delay_max_var.get())
        except ValueError:
            self.log("Invalid number in input fields")
            return
        if budget <= 0 or buy_lo <= 0 or buy_hi <= 0:
            self.log("Budget and buy amounts must be > 0")
            return
        if buy_lo > buy_hi:
            buy_lo, buy_hi = buy_hi, buy_lo

        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.after(0, lambda: self.status_var.set("Starting..."))

        self.worker_thread = threading.Thread(
            target=self._worker, args=(mint, budget, buy_lo, buy_hi, delay_min, delay_max), daemon=True,
        )
        self.worker_thread.start()

    def stop_bumping(self):
        self.running = False
        self.after(0, lambda: self.status_var.set("Stopping..."))
        self.stop_btn.config(state="disabled")

    # ── Worker Thread ─────────────────────────────────────────────────

    def _worker(self, mint_str, budget, buy_lo, buy_hi, delay_min, delay_max):
        mint_pubkey = Pubkey.from_string(mint_str)

        # Detect token program once
        self.log("Detecting token program...")
        token_prog = detect_token_program(self.client, mint_pubkey)
        prog_name = "Token-2022" if token_prog == TOKEN_2022_PROGRAM else "SPL Token"
        self.log(f"Token uses {prog_name}")

        # TX config
        slippage = self.cfg.get('max_slippage_bps', 500)
        cu_buy = self.cfg.get('buy_compute_unit_limit', 100000)
        cu_sell = self.cfg.get('sell_compute_unit_limit', 70000)
        pf = self.cfg.get('priority_fee_lamports', 200000)
        cu_price_buy = max(1, (pf * 1_000_000) // cu_buy)
        cu_price_sell = max(1, (pf * 1_000_000) // cu_sell)

        # Store for drain phase
        self._active_mint = mint_pubkey
        self._active_token_prog = token_prog
        self._active_slippage = slippage
        self._active_cu_sell = cu_sell
        self._active_cu_price_sell = cu_price_sell

        # Use the MAX buy size for funding/threshold calculations (worst case)
        buy_max_lamports = int(buy_hi * SOL_DEC)
        slippage_extra = int(buy_max_lamports * slippage / 10000)
        cost_per_buy = buy_max_lamports + slippage_extra + pf + 5000
        cost_per_sell = pf + 5000
        overhead_per_wallet = ATA_RENT_LAMPORTS + RENT_EXEMPT_LAMPORTS
        min_needed = overhead_per_wallet + cost_per_buy + cost_per_sell

        # Fund sub-wallets: user budget for trading + overhead (overhead is reclaimed on drain)
        trading_each = int((budget / 8) * SOL_DEC)
        amount_each = trading_each + overhead_per_wallet

        self.log(f"Per wallet: {amount_each / SOL_DEC:.5f} SOL "
                 f"({trading_each / SOL_DEC:.5f} trading + "
                 f"{overhead_per_wallet / SOL_DEC:.5f} rent/overhead, reclaimed on drain)")
        total_needed = amount_each * 8
        master_bal = get_sol_balance(self.client, self.master_kp.pubkey())
        if total_needed + RENT_EXEMPT_LAMPORTS > master_bal:
            self.log(f"Insufficient master balance. Need {total_needed / SOL_DEC:.5f} SOL, "
                     f"have {master_bal / SOL_DEC:.5f} SOL")
            self._finish()
            return

        each_sol = amount_each / SOL_DEC
        self.log(f"Funding 8 wallets with {fmt_sol_usd(each_sol, self.sol_price)} each...")
        self.after(0, lambda: self.status_var.set("Funding wallets..."))
        ok, msg = fund_sub_wallets(self.client, self.master_kp, self.sub_wallets, amount_each)
        self.log(msg)
        if not ok:
            self.log("Funding TX failed on-chain. Aborting.")
            self._finish()
            return

        time.sleep(3)
        self._refresh_balances()
        self._show_master_balance()

        # Verify wallets actually received SOL
        funded_count = sum(1 for b in self.wallet_balances if b > RENT_EXEMPT_LAMPORTS)
        if funded_count == 0:
            self.log("No wallets have balance after funding TX. Check solscan for TX details.")
            self._finish()
            return
        self.log(f"Verified: {funded_count}/8 wallets funded")

        self.after(0, lambda: self.status_var.set("Bumping..."))
        self.log("Starting buy+sell cycles (round-robin across 8 wallets)...")

        wallet_idx = 0

        while self.running:
            kp = self.sub_wallets[wallet_idx]
            bal = get_sol_balance(self.client, kp.pubkey())
            self.wallet_balances[wallet_idx] = bal
            self._update_wallet_display()

            if bal < min_needed:
                # Check if all wallets depleted
                all_empty = all(
                    get_sol_balance(self.client, w.pubkey()) < min_needed
                    for w in self.sub_wallets
                )
                if all_empty:
                    self.log("All wallets depleted - stopping")
                    break
                wallet_idx = (wallet_idx + 1) % 8
                continue

            # BUY - random size within range
            buy_amt = round(random.uniform(buy_lo, buy_hi), 6)
            buy_usd = f" (${buy_amt * self.sol_price:.3f})" if self.sol_price > 0 else ""
            self.log(f"W#{wallet_idx+1} BUY {buy_amt} SOL{buy_usd}...")
            ok, msg = buy_token(self.client, kp, mint_pubkey, buy_amt, slippage, cu_buy, cu_price_buy, token_prog)
            self.log(f"  {'OK' if ok else 'FAIL'} - {msg}")

            if ok:
                self.bump_counts[wallet_idx] += 1
                self.total_bumps += 1
                self._update_wallet_display()

                # Brief pause before sell
                time.sleep(random.uniform(1.0, 2.5))

                # SELL 100%
                self.log(f"W#{wallet_idx+1} SELL 100%...")
                ok2, msg2 = sell_token(self.client, kp, mint_pubkey, slippage, cu_sell, cu_price_sell, token_prog)
                self.log(f"  {'OK' if ok2 else 'FAIL'} - {msg2}")

                if not ok2:
                    # Retry sell with higher slippage
                    time.sleep(1)
                    self.log(f"W#{wallet_idx+1} SELL retry (2x slippage)...")
                    ok3, msg3 = sell_token(self.client, kp, mint_pubkey, slippage * 2, cu_sell, cu_price_sell, token_prog)
                    self.log(f"  {'OK' if ok3 else 'FAIL'} - {msg3}")

            # Refresh balance
            self.wallet_balances[wallet_idx] = get_sol_balance(self.client, kp.pubkey())
            self._update_wallet_display()

            # Next wallet
            wallet_idx = (wallet_idx + 1) % 8

            # Random delay between cycles
            delay = random.uniform(delay_min, delay_max)
            self.log(f"Next bump in {delay:.1f}s...")
            for _ in range(int(delay * 10)):
                if not self.running:
                    break
                time.sleep(0.1)

        # Drain
        self.after(0, lambda: self.status_var.set("Draining wallets..."))
        self._drain_all()
        self._show_master_balance()
        self._finish()

    def _drain_all(self):
        self.log("Draining sub-wallets back to master...")

        # Phase 1: Close token accounts to reclaim ATA rent (~0.002 SOL each)
        if self._active_mint and self._active_token_prog:
            self.log("Phase 1: Closing token accounts to reclaim rent...")
            for i, kp in enumerate(self.sub_wallets):
                # First try to sell any stuck tokens
                ata = get_ata(kp.pubkey(), self._active_mint, self._active_token_prog)
                _, raw_bal = get_token_balance(self.client, ata)
                if raw_bal > 0:
                    self.log(f"  W#{i+1}: Selling {raw_bal} stuck tokens...")
                    ok, msg = sell_token(
                        self.client, kp, self._active_mint,
                        self._active_slippage * 3,  # high slippage for cleanup
                        self._active_cu_sell, self._active_cu_price_sell,
                        self._active_token_prog,
                    )
                    self.log(f"  W#{i+1}: {'OK' if ok else 'FAIL'} - {msg}")
                    if ok:
                        continue  # sell_token includes close_account, ATA already closed
                    time.sleep(0.5)

                # Close empty ATA (or ATA where sell just removed tokens)
                ok, msg = close_ata_if_exists(self.client, kp, self._active_mint, self._active_token_prog)
                if "Closed" in msg or "error" in msg.lower():
                    self.log(f"  W#{i+1}: {msg}")
            time.sleep(2)

        # Phase 2: Drain all SOL back to master
        self.log("Phase 2: Draining SOL...")
        total_drained = 0.0
        for i, kp in enumerate(self.sub_wallets):
            ok, msg = drain_one_wallet(self.client, kp, self.master_kp.pubkey())
            if msg != "Empty":
                self.log(f"  W#{i+1}: {msg}")
                try:
                    amt = float(msg.split("Drained ")[1].split(" SOL")[0])
                    total_drained += amt
                except (IndexError, ValueError):
                    pass
        time.sleep(2)
        self._refresh_balances()
        drained_usd = f" (${total_drained * self.sol_price:.2f})" if self.sol_price > 0 else ""
        self.log(f"Drain complete - recovered {total_drained:.6f} SOL{drained_usd}")

    def _finish(self):
        self.running = False
        self.after(0, lambda: self.status_var.set("Done"))
        self.after(0, lambda: self.start_btn.config(state="normal"))
        self.after(0, lambda: self.stop_btn.config(state="disabled"))


if __name__ == "__main__":
    app = BumperApp()
    app.mainloop()
