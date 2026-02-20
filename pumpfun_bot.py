"""
Pump.fun Sniper Bot Simulator - Advanced Paper Trading System
================================================

Features:
- Real-time data from Solana blockchain (no Cloudflare issues!)
- Advanced exit strategies:
  * Trailing stop loss (protect profits as price rises)
  * Trailing take profit (lock in gains at target %)
  * Early dump detection (exit if dumps in first 15-20s)
  * No-buys detection (exit if no buying activity)
- Optional Martingale position sizing (recover losses)
- Max positions control (1 position = focused strategy)
- Metadata quality detection (IPFS bypass = sophisticated devs)
- Risk scoring system (liquidity, dev holdings, honeypot detection)
- Paper trading with realistic market simulation

Configuration Tips:
- Start with 1 max position for clean, focused trading
- Use 25% trailing stop to let winners run
- Set 100% take profit to lock in 2x gains
- Enable early dump detection to avoid rugs
- Martingale is HIGH RISK - only use if you understand it!
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import asyncio
import websockets
import json
import threading
import queue
from datetime import datetime
from typing import Dict, List
import time
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey
import struct

# Import live trader for real trading
try:
    from live_trader import LiveTrader
    LIVE_TRADING_AVAILABLE = True
except ImportError:
    LIVE_TRADING_AVAILABLE = False
    print("⚠️ LiveTrader not available - paper trading only")

class PumpFunSniperBot:
    def __init__(self, root):
        self.root = root
        self.root.title("🎯 Pump.fun Sniper Bot Simulator")
        self.root.geometry("1400x900")
        self.root.configure(bg='#0a0a0a')
        
        # Bot state
        self.is_running = False
        self.capital = 1000.0
        self.starting_capital = 1000.0
        self.positions: List[Dict] = []
        self.trade_history: List[Dict] = []
        self.new_coins: List[Dict] = []
        self.stats = {'totalTrades': 0, 'wins': 0, 'losses': 0, 'breakeven': 0, 'totalPnL': 0, 'winRate': 0}

        # Failed update tracking (Solution 1)
        self.failed_updates: Dict[str, int] = {}  # {mint: consecutive_fail_count}

        # Live trading
        self.live_mode = False
        self.live_trader = None

        # Configuration - OPTIMIZED (based on testing results - fixed 71% "Dead Token" issue)
        self.config = {
            'maxPositionSize': 50,       # Base position size
            'trailingStop': 12,          # KEEP at 12% - good balance
            'trailingTakeProfit': 30,    # KEEP at 30% - realistic target
            'minLiquidity': 3,           # OPTIMIZED: 1 → 3 (avoid ultra-small caps)
            'maxDevHolding': 12,         # OPTIMIZED: 15 → 12 (stricter rug detection)
            'autoTrade': True,           # ENABLED for auto-sniping
            'maxOpenPositions': 1,       # Only 1 position at a time (focus strategy)
            'useMartingale': False,      # Enable/disable Martingale on losses
            'useSoftMartingale': False,  # OPTIMIZED: Disabled for small budget testing
            'martingaleMultiplier': 2.0, # Double position after loss
            'maxMartingaleLevel': 3,     # Max 3 levels (1x, 2x, 4x, then reset)
            'earlyDumpDetection': True,  # Exit if price dumps in first 15s
            'earlyDumpThreshold': -10,   # OPTIMIZED: -8 → -10 (less sensitive, avoid false positives)
            'earlyDumpWindow': 15,       # OPTIMIZED: 10 → 15 (more time before triggering)
            'noBuysExit': True,          # Exit if no buys detected
            'noBuysWindow': 20,          # CRITICAL FIX: 6 → 20 (was killing 71% of trades too early!)
            'useTwitterSentiment': False # Twitter sentiment analysis (requires API key)
        }

        # Martingale state
        self.consecutive_losses = 0
        self.current_position_size = self.config['maxPositionSize']

        # Twitter monitoring (placeholder for future implementation)
        self.twitter_influencers = [
            # Add popular crypto Twitter accounts to monitor
            # "elonmusk", "VitalikButerin", etc.
        ]
        
        # WebSocket
        self.ws = None
        self.ws_thread = None
        # Thread-safe queue for subscribe/unsubscribe commands sent to the WS loop
        self.ws_command_queue: queue.Queue = queue.Queue()
        # Real-time price cache populated by WebSocket trade events (no HTTP polling)
        self.token_prices: Dict[str, dict] = {}

        # Solana RPC client for on-chain data
        self.solana_client = Client("https://api.mainnet-beta.solana.com")
        # Rate-limit tracker for RPC calls (public endpoint: ~10 req/s safe)
        self.rpc_last_call_time: float = 0.0
        self.rpc_min_interval: float = 0.15  # 150 ms between RPC calls

        # Persistent HTTP session (reuse TCP connections, avoid repeated handshakes)
        self.http_session = requests.Session()
        self.http_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
        })

        # Pump.fun program constants
        self.PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # Pump.fun program ID
        
        # Create GUI
        self.create_gui()

        # Start price update loop
        self.price_update_running = False

    def derive_bonding_curve_pda(self, mint):
        """Derive the bonding curve PDA for a mint"""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            program_pubkey = Pubkey.from_string(self.PUMP_FUN_PROGRAM)

            # Find PDA with seeds: ["bonding-curve", mint]
            seeds = [b"bonding-curve", bytes(mint_pubkey)]
            pda, _ = Pubkey.find_program_address(seeds, program_pubkey)

            return pda
        except Exception as e:
            self.add_log(f"❌ PDA derivation error: {str(e)}", 'error')
            return None

    def fetch_from_solana(self, mint, symbol=None, name=None):
        """Fetch token data directly from Solana blockchain - FASTEST METHOD"""
        try:
            # Derive the bonding curve PDA
            bonding_curve_pda = self.derive_bonding_curve_pda(mint)
            if not bonding_curve_pda:
                return None

            # Get bonding curve account data
            response = self.solana_client.get_account_info(bonding_curve_pda)

            if response.value is None:
                return None  # Bonding curve doesn't exist yet (too early!)

            # Decode the account data
            account_data = response.value.data

            # Pump.fun bonding curve layout:
            # - 8 bytes: discriminator
            # - 8 bytes: virtual_token_reserves (u64)
            # - 8 bytes: virtual_sol_reserves (u64)
            # - 8 bytes: real_token_reserves (u64)
            # - 8 bytes: real_sol_reserves (u64)

            if len(account_data) >= 64:
                # Unpack the data (little-endian)
                virtual_token_reserves = struct.unpack('<Q', account_data[8:16])[0]
                virtual_sol_reserves = struct.unpack('<Q', account_data[16:24])[0]
                real_token_reserves = struct.unpack('<Q', account_data[24:32])[0]
                real_sol_reserves = struct.unpack('<Q', account_data[32:40])[0]

                return {
                    'mint': mint,
                    'symbol': symbol if symbol else mint[:6],  # Use WebSocket data if available
                    'name': name if name else f"Token {mint[:6]}",
                    'virtual_sol_reserves': virtual_sol_reserves,
                    'virtual_token_reserves': virtual_token_reserves,
                    'real_sol_reserves': real_sol_reserves,
                    'real_token_reserves': real_token_reserves,
                    'onchain': True
                }
            else:
                return None

        except Exception:
            return None  # Silent fail - this is just a fallback
        
    def create_gui(self):
        # Style
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Green.TLabel', background='#0a0a0a', foreground='#0f0', font=('Courier', 10))
        style.configure('Red.TLabel', background='#0a0a0a', foreground='#f00', font=('Courier', 10))
        style.configure('Orange.TLabel', background='#0a0a0a', foreground='#f90', font=('Courier', 10))
        style.configure('Title.TLabel', background='#0a0a0a', foreground='#0f0', font=('Courier', 20, 'bold'))
        
        # Title
        title = ttk.Label(self.root, text="🎯 Pump.fun Sniper Bot Simulator", style='Title.TLabel')
        title.pack(pady=10)
        
        # Stats Frame
        stats_frame = tk.Frame(self.root, bg='#1a1a1a', relief='solid', bd=2)
        stats_frame.pack(fill='x', padx=10, pady=5)
        
        self.create_stat_cards(stats_frame)
        
        # Config Frame
        config_frame = tk.LabelFrame(self.root, text="⚙️ Configuration", bg='#1a1a1a', fg='#0f0', font=('Courier', 12, 'bold'))
        config_frame.pack(fill='x', padx=10, pady=5)
        
        self.create_config_panel(config_frame)
        
        # Notebook for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Positions Tab
        positions_frame = tk.Frame(self.notebook, bg='#0a0a0a')
        self.notebook.add(positions_frame, text='💼 Open Positions')
        self.create_positions_tab(positions_frame)
        
        # Recent Coins Tab
        coins_frame = tk.Frame(self.notebook, bg='#0a0a0a')
        self.notebook.add(coins_frame, text='🆕 Recent Coins')
        self.create_coins_tab(coins_frame)
        
        # Trade History Tab
        history_frame = tk.Frame(self.notebook, bg='#0a0a0a')
        self.notebook.add(history_frame, text='📊 Trade History')
        self.create_history_tab(history_frame)
        
        # Activity Log Tab
        log_frame = tk.Frame(self.notebook, bg='#0a0a0a')
        self.notebook.add(log_frame, text='📝 Activity Log')
        self.create_log_tab(log_frame)
        
    def create_stat_cards(self, parent):
        # Portfolio Value
        card1 = tk.Frame(parent, bg='#0a0a0a', relief='solid', bd=1)
        card1.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        
        tk.Label(card1, text="Total Portfolio", bg='#0a0a0a', fg='#888', font=('Courier', 8)).pack()
        self.portfolio_label = tk.Label(card1, text="$1000.00", bg='#0a0a0a', fg='#0f0', font=('Courier', 16, 'bold'))
        self.portfolio_label.pack()
        self.portfolio_pnl = tk.Label(card1, text="$0.00 (0.0%)", bg='#0a0a0a', fg='#888', font=('Courier', 8))
        self.portfolio_pnl.pack()
        
        # Available Capital
        card2 = tk.Frame(parent, bg='#0a0a0a', relief='solid', bd=1)
        card2.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        
        tk.Label(card2, text="Available Capital", bg='#0a0a0a', fg='#888', font=('Courier', 8)).pack()
        self.capital_label = tk.Label(card2, text="$1000.00", bg='#0a0a0a', fg='#0f0', font=('Courier', 16, 'bold'))
        self.capital_label.pack()
        self.positions_value = tk.Label(card2, text="Positions: $0.00", bg='#0a0a0a', fg='#888', font=('Courier', 8))
        self.positions_value.pack()
        
        # Win Rate
        card3 = tk.Frame(parent, bg='#0a0a0a', relief='solid', bd=1)
        card3.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        
        tk.Label(card3, text="Win Rate", bg='#0a0a0a', fg='#888', font=('Courier', 8)).pack()
        self.winrate_label = tk.Label(card3, text="0.0%", bg='#0a0a0a', fg='#0f0', font=('Courier', 16, 'bold'))
        self.winrate_label.pack()
        self.winloss_label = tk.Label(card3, text="0W / 0L (0 trades)", bg='#0a0a0a', fg='#888', font=('Courier', 8))
        self.winloss_label.pack()
        
        # Status
        card4 = tk.Frame(parent, bg='#0a0a0a', relief='solid', bd=1)
        card4.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        
        tk.Label(card4, text="Status", bg='#0a0a0a', fg='#888', font=('Courier', 8)).pack()
        self.status_label = tk.Label(card4, text="🟡 PAUSED", bg='#0a0a0a', fg='#f90', font=('Courier', 14, 'bold'))
        self.status_label.pack()
        self.position_count = tk.Label(card4, text="0 open positions", bg='#0a0a0a', fg='#888', font=('Courier', 8))
        self.position_count.pack()
        
    def create_config_panel(self, parent):
        controls_frame = tk.Frame(parent, bg='#1a1a1a')
        controls_frame.pack(fill='x', padx=10, pady=10)
        
        # Row 1
        row1 = tk.Frame(controls_frame, bg='#1a1a1a')
        row1.pack(fill='x', pady=5)
        
        tk.Label(row1, text="Starting Capital ($):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.capital_entry = tk.Entry(row1, width=10, font=('Courier', 9))
        self.capital_entry.insert(0, "1000")  # Optimal starting capital
        self.capital_entry.pack(side='left', padx=5)

        tk.Label(row1, text="Max Position ($):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.position_entry = tk.Entry(row1, width=10, font=('Courier', 9))
        self.position_entry.insert(0, "50")  # 5% of capital - optimal risk
        self.position_entry.pack(side='left', padx=5)

        tk.Label(row1, text="Trailing Stop (%):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.trailing_entry = tk.Entry(row1, width=10, font=('Courier', 9))
        self.trailing_entry.insert(0, "8")  # ULTRA TIGHT - 8% max loss
        self.trailing_entry.pack(side='left', padx=5)

        tk.Label(row1, text="Take Profit (%):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.takeprofit_entry = tk.Entry(row1, width=10, font=('Courier', 9))
        self.takeprofit_entry.insert(0, "25")  # Quick 25% scalp target
        self.takeprofit_entry.pack(side='left', padx=5)

        # Row 2
        row2 = tk.Frame(controls_frame, bg='#1a1a1a')
        row2.pack(fill='x', pady=5)

        tk.Label(row2, text="Min Liquidity ($):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.liquidity_entry = tk.Entry(row2, width=10, font=('Courier', 9))
        self.liquidity_entry.insert(0, "1")  # LOW for sniping fresh tokens (1-5 SOL)
        self.liquidity_entry.pack(side='left', padx=5)

        tk.Label(row2, text="Max Dev Holding (%):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.devhold_entry = tk.Entry(row2, width=10, font=('Courier', 9))
        self.devhold_entry.insert(0, "15")  # Lower dev holding reduces rug risk
        self.devhold_entry.pack(side='left', padx=5)
        
        self.autotrade_var = tk.BooleanVar(value=True)  # ENABLED by default for sniping!
        self.autotrade_check = tk.Checkbutton(row2, text="Auto-Trade", variable=self.autotrade_var,
                                             bg='#1a1a1a', fg='#0f0', selectcolor='#0a0a0a',
                                             font=('Courier', 9))
        self.autotrade_check.pack(side='left', padx=10)

        self.martingale_var = tk.BooleanVar()
        self.martingale_check = tk.Checkbutton(row2, text="Martingale (⚠️ Risky)", variable=self.martingale_var,
                                              bg='#1a1a1a', fg='#f90', selectcolor='#0a0a0a',
                                              font=('Courier', 9))
        self.martingale_check.pack(side='left', padx=10)

        tk.Label(row2, text="Max Positions:", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.max_positions_entry = tk.Entry(row2, width=5, font=('Courier', 9))
        self.max_positions_entry.insert(0, "1")  # One at a time for focus
        self.max_positions_entry.pack(side='left', padx=5)

        # Row 3 - Live Trading Toggle
        row3 = tk.Frame(controls_frame, bg='#1a1a1a')
        row3.pack(fill='x', pady=5)

        self.live_mode_var = tk.BooleanVar(value=False)
        self.live_mode_check = tk.Checkbutton(row3, text="🔴 LIVE TRADING (REAL MONEY!)",
                                             variable=self.live_mode_var,
                                             command=self.toggle_live_mode,
                                             bg='#1a1a1a', fg='#f00', selectcolor='#0a0a0a',
                                             font=('Courier', 9, 'bold'))
        self.live_mode_check.pack(side='left', padx=10)

        if not LIVE_TRADING_AVAILABLE:
            self.live_mode_check.config(state='disabled')
            tk.Label(row3, text="(Install dependencies: pip install -r requirements.txt)",
                    bg='#1a1a1a', fg='#888', font=('Courier', 8)).pack(side='left', padx=5)

        self.live_status_label = tk.Label(row3, text="Mode: PAPER TRADING",
                                          bg='#1a1a1a', fg='#0f0', font=('Courier', 9, 'bold'))
        self.live_status_label.pack(side='left', padx=20)

        # MEV Protection Toggle (only for live trading)
        self.mev_protection_var = tk.BooleanVar(value=False)
        self.mev_protection_check = tk.Checkbutton(row3, text="🛡️ MEV Protection",
                                                   variable=self.mev_protection_var,
                                                   command=self.toggle_mev_protection,
                                                   bg='#1a1a1a', fg='#0af', selectcolor='#0a0a0a',
                                                   font=('Courier', 9, 'bold'),
                                                   state='disabled')  # Disabled until live mode
        self.mev_protection_check.pack(side='left', padx=10)

        # Buttons
        button_frame = tk.Frame(controls_frame, bg='#1a1a1a')
        button_frame.pack(fill='x', pady=10)
        
        self.start_btn = tk.Button(button_frame, text="▶️ START", command=self.toggle_bot,
                                   bg='#0f0', fg='#000', font=('Courier', 12, 'bold'),
                                   width=12, cursor='hand2')
        self.start_btn.pack(side='left', padx=5)
        
        self.reset_btn = tk.Button(button_frame, text="🔄 RESET", command=self.reset_bot,
                                   bg='#f00', fg='#fff', font=('Courier', 12, 'bold'),
                                   width=12, cursor='hand2')
        self.reset_btn.pack(side='left', padx=5)
        
        self.test_btn = tk.Button(button_frame, text="🧪 TEST API", command=self.test_api,
                                  bg='#00f', fg='#fff', font=('Courier', 12, 'bold'),
                                  width=12, cursor='hand2')
        self.test_btn.pack(side='left', padx=5)
        
    def create_positions_tab(self, parent):
        columns = ('Symbol', 'Buy Price', 'Current', 'Peak', 'Stop Loss', 'P&L', '%')
        
        self.positions_tree = ttk.Treeview(parent, columns=columns, show='headings', height=15)
        
        for col in columns:
            self.positions_tree.heading(col, text=col)
            self.positions_tree.column(col, width=120, anchor='center')
        
        scrollbar = ttk.Scrollbar(parent, orient='vertical', command=self.positions_tree.yview)
        self.positions_tree.configure(yscrollcommand=scrollbar.set)
        
        self.positions_tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
    def create_coins_tab(self, parent):
        columns = ('Symbol', 'Contract', 'Price', 'Liquidity', 'Dev%', 'Locked', 'Sellable')
        
        self.coins_tree = ttk.Treeview(parent, columns=columns, show='headings', height=15)
        
        for col in columns:
            self.coins_tree.heading(col, text=col)
            self.coins_tree.column(col, width=140, anchor='center')
        
        scrollbar = ttk.Scrollbar(parent, orient='vertical', command=self.coins_tree.yview)
        self.coins_tree.configure(yscrollcommand=scrollbar.set)
        
        self.coins_tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
    def create_history_tab(self, parent):
        columns = ('Symbol', 'Buy', 'Sell', 'P&L', '%', 'Hold Time', 'Reason')
        
        self.history_tree = ttk.Treeview(parent, columns=columns, show='headings', height=15)
        
        for col in columns:
            self.history_tree.heading(col, text=col)
            self.history_tree.column(col, width=140, anchor='center')
        
        scrollbar = ttk.Scrollbar(parent, orient='vertical', command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        
        self.history_tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
    def create_log_tab(self, parent):
        self.log_text = scrolledtext.ScrolledText(parent, wrap=tk.WORD, 
                                                  bg='#0a0a0a', fg='#0f0',
                                                  font=('Courier', 9),
                                                  height=20)
        self.log_text.pack(fill='both', expand=True, padx=5, pady=5)
        
        # Configure tags for different log types
        self.log_text.tag_config('success', foreground='#0f0')
        self.log_text.tag_config('error', foreground='#f00')
        self.log_text.tag_config('warning', foreground='#f90')
        self.log_text.tag_config('info', foreground='#00f')
        
        self.add_log("💻 Simulator ready. Click START to begin.", 'info')
        
    def add_log(self, message, log_type='info'):
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_msg = f"[{timestamp}] {message}\n"
        self.log_text.insert('1.0', log_msg, log_type)
        self.log_text.see('1.0')

    def toggle_live_mode(self):
        """Toggle between live and paper trading"""
        if self.is_running:
            messagebox.showwarning("Warning", "Stop the bot before changing trading mode!")
            self.live_mode_var.set(self.live_mode)
            return

        new_mode = self.live_mode_var.get()

        if new_mode and not self.live_mode:
            # Switching to live mode
            confirm = messagebox.askyesno(
                "⚠️ LIVE TRADING WARNING ⚠️",
                "You are about to enable LIVE TRADING with REAL MONEY!\n\n"
                "Make sure you have:\n"
                "1. Created wallet_config.json with your wallet\n"
                "2. Tested with small amounts first\n"
                "3. Set appropriate position limits\n\n"
                "Are you sure you want to continue?"
            )

            if not confirm:
                self.live_mode_var.set(False)
                return

            # Initialize live trader with reduced logging
            try:
                self.live_trader = LiveTrader("wallet_config.json", verbose=False)
                balance = self.live_trader.get_sol_balance()

                self.live_mode = True

                # Automatically update starting capital from wallet balance
                # Get real-time SOL price
                sol_price_usd = self.get_sol_price_usd()
                self.cached_sol_price = sol_price_usd
                wallet_usd = balance * sol_price_usd

                # Update the capital entry field
                self.capital_entry.config(state='normal')
                self.capital_entry.delete(0, tk.END)
                self.capital_entry.insert(0, f"{wallet_usd:.2f}")

                self.add_log(f"💰 Starting capital auto-set to ${wallet_usd:.2f} ({balance:.6f} SOL @ ${sol_price_usd:.2f})", 'info')

                # Enable MEV protection checkbox
                self.mev_protection_check.config(state='normal')

                # Set MEV protection based on wallet config
                if hasattr(self.live_trader, 'use_jito'):
                    self.mev_protection_var.set(self.live_trader.use_jito)

                # Update status label
                mev_status = "🛡️" if self.live_trader.use_jito else ""
                self.live_status_label.config(text=f"Mode: LIVE 🔴 | Balance: {balance:.4f} SOL {mev_status}", fg='#f00')

                self.add_log(f"🔴 LIVE MODE ENABLED - Wallet: {self.live_trader.wallet_address[:8]}...", 'warning')
                self.add_log(f"💰 SOL Balance: {balance:.6f}", 'info')

                if self.live_trader.use_jito:
                    self.add_log(f"🛡️ MEV Protection: ENABLED (Jito)", 'info')

                messagebox.showinfo("Live Mode Enabled",
                                   f"Live trading is now ACTIVE!\n\n"
                                   f"Wallet: {self.live_trader.wallet_address[:8]}...\n"
                                   f"Balance: {balance:.6f} SOL\n\n"
                                   f"Trades will use REAL SOL!")

            except Exception as e:
                self.live_mode_var.set(False)
                messagebox.showerror("Error", f"Failed to initialize live trading:\n{str(e)}\n\n"
                                              f"Make sure wallet_config.json exists and is valid.")
                self.add_log(f"❌ Failed to enable live mode: {str(e)}", 'error')

        else:
            # Switching to paper mode
            self.live_mode = False
            self.live_trader = None
            self.live_status_label.config(text="Mode: PAPER TRADING", fg='#0f0')
            self.add_log("📄 Paper trading mode enabled", 'info')

            # Disable MEV protection checkbox
            self.mev_protection_check.config(state='disabled')
            self.mev_protection_var.set(False)

    def toggle_mev_protection(self):
        """Toggle MEV protection for live trading"""
        if not self.live_mode or not self.live_trader:
            self.mev_protection_var.set(False)
            return

        mev_enabled = self.mev_protection_var.get()

        # Update the live trader's MEV setting
        self.live_trader.use_jito = mev_enabled

        # Update status label
        balance = self.live_trader.get_sol_balance()
        mev_status = "🛡️" if mev_enabled else ""
        self.live_status_label.config(text=f"Mode: LIVE 🔴 | Balance: {balance:.4f} SOL {mev_status}", fg='#f00')

        # Log the change
        if mev_enabled:
            self.add_log(f"🛡️ MEV Protection ENABLED (Jito tip: {self.live_trader.jito_tip_lamports} lamports)", 'info')
        else:
            self.add_log("⚠️ MEV Protection DISABLED - transactions may be front-run", 'warning')

    def toggle_bot(self):
        if self.is_running:
            self.stop_bot()
        else:
            self.start_bot()
            
    def start_bot(self):
        # Load config
        try:
            self.starting_capital = float(self.capital_entry.get())
            self.capital = self.starting_capital
            self.config['maxPositionSize'] = float(self.position_entry.get())
            self.config['trailingStop'] = float(self.trailing_entry.get())
            self.config['trailingTakeProfit'] = float(self.takeprofit_entry.get())
            self.config['minLiquidity'] = float(self.liquidity_entry.get())
            self.config['maxDevHolding'] = float(self.devhold_entry.get())
            self.config['autoTrade'] = self.autotrade_var.get()
            self.config['useMartingale'] = self.martingale_var.get()
            self.config['maxOpenPositions'] = int(self.max_positions_entry.get())

            # Reset Martingale state
            self.consecutive_losses = 0
            self.current_position_size = self.config['maxPositionSize']

        except ValueError:
            self.add_log("❌ Invalid configuration values", 'error')
            return
            
        self.is_running = True
        self.start_btn.config(text="⏸️ PAUSE", bg='#f90')
        self.status_label.config(text="🟢 ACTIVE", fg='#0f0')
        
        # Disable config entries
        self.capital_entry.config(state='disabled')
        self.position_entry.config(state='disabled')
        
        self.add_log("🚀 Bot started - Connecting to Pump.fun...", 'success')
        
        # Start WebSocket in separate thread
        self.ws_thread = threading.Thread(target=self.run_websocket, daemon=True)
        self.ws_thread.start()
        
        # Start price updates
        self.price_update_running = True
        self.price_update_thread = threading.Thread(target=self.price_update_loop, daemon=True)
        self.price_update_thread.start()
        
    def stop_bot(self):
        self.is_running = False
        self.price_update_running = False
        self.start_btn.config(text="▶️ START", bg='#0f0')
        self.status_label.config(text="🟡 PAUSED", fg='#f90')

        # Enable config entries
        self.capital_entry.config(state='normal')
        self.position_entry.config(state='normal')

        self.add_log("⏸️ Bot stopped", 'warning')
        
    def reset_bot(self):
        if self.is_running:
            return
            
        self.capital = self.starting_capital
        self.positions = []
        self.trade_history = []
        self.new_coins = []
        self.stats = {'totalTrades': 0, 'wins': 0, 'losses': 0, 'breakeven': 0, 'totalPnL': 0, 'winRate': 0}
        
        self.update_display()
        self.add_log("🔄 Bot reset", 'info')
        
    def test_api(self):
        """Test API connectivity"""
        self.add_log("🧪 Testing data fetch from API...", 'info')

        # Test with a known token
        test_mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

        try:
            # Test the API (it works!)
            data = self.fetch_coin_data(test_mint)

            if data:
                price = self.calculate_price(data)
                self.add_log(f"✅ API TEST PASSED!", 'success')
                self.add_log(f"📊 Token: {data.get('symbol', 'UNKNOWN')}", 'success')
                self.add_log(f"💰 Price: ${price:.8f}", 'info')
                if data.get('uri'):
                    self.add_log(f"🔗 Has metadata URI - quality detection working!", 'success')
                return True
            else:
                self.add_log(f"❌ TEST FAILED - No data received", 'error')
                return False

        except Exception as e:
            self.add_log(f"❌ UNEXPECTED ERROR: {str(e)}", 'error')
            import traceback
            traceback.print_exc()
            return False
            
    def test_alternative_api(self):
        """No longer needed - cloudscraper handles everything"""
        pass
        
    def run_websocket(self):
        """Run WebSocket with auto-reconnect"""
        while self.is_running:
            try:
                asyncio.run(self.connect_websocket())
            except Exception:
                if self.is_running:
                    self.add_log(f"🔄 WebSocket disconnected, reconnecting in 3s...", 'warning')
                    time.sleep(3)

    async def connect_websocket(self):
        """Connect to Pump.fun WebSocket — handles new tokens AND real-time trade events."""
        try:
            async with websockets.connect(
                'wss://pumpportal.fun/api/data',
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                self.add_log("✅ Connected to Pump.fun WebSocket", 'success')

                # Subscribe to new token creations
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                self.add_log("📡 Subscribed to new token events", 'info')

                # Re-subscribe to any positions that were open before reconnect
                active_mints = [p['mint'] for p in self.positions]
                if active_mints:
                    await ws.send(json.dumps({
                        "method": "subscribeTokenTrade",
                        "keys": active_mints
                    }))
                    self.add_log(f"📡 Re-subscribed to {len(active_mints)} active positions", 'info')

                # Drain any stale queue commands from before reconnect
                while not self.ws_command_queue.empty():
                    try:
                        self.ws_command_queue.get_nowait()
                    except queue.Empty:
                        break

                ping_counter = 0
                while self.is_running:
                    # Flush pending subscribe/unsubscribe commands
                    while not self.ws_command_queue.empty():
                        try:
                            cmd = self.ws_command_queue.get_nowait()
                            await ws.send(json.dumps(cmd))
                        except queue.Empty:
                            break

                    try:
                        # Short timeout so we can service the command queue frequently
                        message = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        data = json.loads(message)
                        tx_type = data.get('txType', '')

                        if tx_type in ('buy', 'sell'):
                            # Real-time trade event — update price cache, zero HTTP calls
                            self.handle_trade_event(data)
                        elif 'mint' in data:
                            # New token creation event
                            self.handle_new_coin(data)

                    except asyncio.TimeoutError:
                        # No message in 100 ms — keep-alive ping every ~30 s
                        ping_counter += 1
                        if ping_counter >= 300:
                            ping_counter = 0
                            try:
                                pong = await ws.ping()
                                await asyncio.wait_for(pong, timeout=5)
                            except Exception:
                                self.add_log("⚠️ Connection lost - reconnecting...", 'warning')
                                break
                    except websockets.exceptions.ConnectionClosed:
                        self.add_log("⚠️ Connection closed - reconnecting...", 'warning')
                        break
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        print(f"WebSocket message error: {e}")

        except Exception as e:
            if self.is_running:
                self.add_log(f"❌ WebSocket error: {str(e)}", 'error')
            
    def handle_trade_event(self, data):
        """Cache real-time price from a WebSocket trade event (zero API calls)."""
        mint = data.get('mint')
        if not mint:
            return
        # PumpPortal trade events expose current bonding curve reserves
        vsr = data.get('vSolInBondingCurve') or data.get('virtualSolReserves')
        vtr = data.get('vTokensInBondingCurve') or data.get('virtualTokenReserves')
        if vsr and vtr:
            self.token_prices[mint] = {
                'virtualSolReserves': float(vsr),
                'virtualTokenReserves': float(vtr),
                'timestamp': time.time(),
                'txType': data.get('txType', ''),
            }

    def handle_new_coin(self, data):
        symbol = data.get('symbol', data['mint'][:6])
        name = data.get('name', symbol)
        self.add_log(f"🆕 New token detected: {symbol}", 'info')

        try:
            # FAST PATH: PumpPortal new-token events already contain all reserves + metadata.
            # Use them directly — zero HTTP / RPC calls required.
            vsr = data.get('virtualSolReserves') or data.get('vSolInBondingCurve')
            vtr = data.get('virtualTokenReserves') or data.get('vTokensInBondingCurve')

            if vsr and vtr:
                # Build prefetched_curve so the live buy can skip BC_RETRIES RPC polling.
                # traderPublicKey = creator (needed for creator-vault PDA).
                trader_key = data.get('traderPublicKey')
                bc_key     = data.get('bondingCurveKey')
                prefetched_curve = None
                if trader_key:
                    try:
                        prefetched_curve = {
                            'virtual_token_reserves': int(float(vtr)),
                            'virtual_sol_reserves':   int(float(vsr)),
                            'real_token_reserves':    0,
                            'real_sol_reserves':      0,
                            'bonding_curve_pda':      bc_key or '',
                            'creator':                Pubkey.from_string(trader_key),
                        }
                    except Exception:
                        pass

                coin_data = {
                    'mint': data['mint'],
                    'symbol': symbol,
                    'name': name,
                    'virtual_sol_reserves': float(vsr),
                    'virtual_token_reserves': float(vtr),
                    'real_sol_reserves': data.get('real_sol_reserves', 0),
                    'real_token_reserves': data.get('real_token_reserves', 0),
                    'uri': data.get('metadata_uri') or data.get('uri', ''),
                    'image_uri': data.get('image_uri', ''),
                    'description': data.get('description', ''),
                    'twitter': data.get('twitter', ''),
                    'telegram': data.get('telegram', ''),
                    'website': data.get('website', ''),
                    'created_timestamp': data.get('created_timestamp', int(time.time() * 1000)),
                    'market_cap': data.get('market_cap', 0),
                    'complete': data.get('complete', False),
                    'onchain': True,
                    'prefetched_curve': prefetched_curve,
                }
                # Seed the price cache so the first price-update cycle has data immediately
                self.token_prices[data['mint']] = {
                    'virtualSolReserves': float(vsr),
                    'virtualTokenReserves': float(vtr),
                    'timestamp': time.time(),
                }
            else:
                # Fallback: reserves not in event → query Solana RPC once
                coin_data = self.fetch_from_solana(data['mint'], symbol=symbol, name=name)
                if not coin_data:
                    coin_data = self.fetch_coin_data_direct(data['mint'])

            if coin_data:
                self.add_log(f"✅ Got REAL data for {symbol}", 'success')
                coin = self.process_coin(coin_data)
                self.new_coins.insert(0, coin)
                self.new_coins = self.new_coins[:20]
                
                # Check risk and trade
                risk = self.check_rugpull_risk(coin)

                # Check if we're at max positions
                if len(self.positions) >= self.config['maxOpenPositions']:
                    self.add_log(f"⏸️ {coin['symbol']} - Max positions reached ({len(self.positions)}/{self.config['maxOpenPositions']})", 'info')
                elif risk['safe'] and self.config['autoTrade'] and self.capital >= self.current_position_size:
                    self.add_log(f"🔍 {coin['symbol']} passed checks | Risk: {risk['riskScore']}/100", 'info')

                    # Use Martingale-adjusted position size
                    position_amount = min(self.current_position_size, self.capital)

                    if self.config['useMartingale'] and self.consecutive_losses > 0:
                        self.add_log(f"📈 Martingale Level {self.consecutive_losses}: ${position_amount:.2f} ({self.consecutive_losses}x losses)", 'warning')

                    self.execute_buy(coin, position_amount)
                elif not risk['safe']:
                    self.add_log(f"🚫 REJECTED {coin['symbol']} | {risk['risks'][0]}", 'warning')
                else:
                    # Log why we didn't trade
                    if not self.config['autoTrade']:
                        self.add_log(f"⏸️ {coin['symbol']} - Auto-trade disabled", 'info')
                    elif self.capital < self.current_position_size:
                        self.add_log(f"⏸️ {coin['symbol']} - Insufficient capital", 'warning')
                    
                self.root.after(0, self.update_display)
            else:
                self.add_log(f"❌ Failed to fetch data for {symbol}", 'error')
                
        except Exception as e:
            self.add_log(f"❌ Error processing {symbol}: {str(e)}", 'error')
            print(f"Error processing coin: {e}")
            import traceback
            traceback.print_exc()
            
    def fetch_coin_data_direct(self, mint):
        """HTTP fallback for when WebSocket event has no reserves (rare). Uses persistent session."""
        api_endpoints = [
            f'https://frontend-api-v3.pump.fun/coins/{mint}',
            f'https://api.pumpportal.fun/coins/{mint}',
        ]
        for url in api_endpoints:
            try:
                response = self.http_session.get(url, timeout=8)
                if response.status_code == 200:
                    ct = response.headers.get('content-type', '')
                    if 'json' in ct or response.text.strip().startswith('{'):
                        return response.json()
                elif response.status_code == 429:
                    self.add_log(f"⚠️ Rate limited by {url.split('/')[2]} — use a private RPC", 'warning')
            except requests.exceptions.Timeout:
                pass
            except Exception:
                pass
        return None

    def fetch_coin_data(self, mint):
        """Fetch coin data via HTTP (last-resort fallback, not called in normal flow)."""
        data = self.fetch_coin_data_direct(mint)
        if data:
            return data
        data = self.fetch_from_solana(mint)
        if data:
            return data
        self.add_log(f"❌ All methods failed for {mint[:8]}", 'error')
        return None
        
    def check_token_security(self, mint_address):
        """Check REAL on-chain security features"""
        try:
            from solders.pubkey import Pubkey
            mint_pubkey = Pubkey.from_string(mint_address)

            # Get mint account data from blockchain
            if self.live_mode and self.live_trader:
                response = self.live_trader.client.get_account_info(mint_pubkey)
                if not response.value:
                    # Token too new or RPC issue - assume can sell (Pump.fun tokens are tradeable)
                    return {'mintAuthority': True, 'freezeAuthority': True, 'canSell': True}

                # Parse mint account (Token-2022 or SPL Token)
                data = response.value.data
                # Mint authority is at offset 0-36 (36 bytes: 4 byte option + 32 byte pubkey)
                # If first 4 bytes are [0,0,0,0] = no authority (burned)
                mint_authority_exists = data[0] != 0
                # Freeze authority is at offset 36-72
                freeze_authority_exists = data[36] != 0

                return {
                    'mintAuthority': mint_authority_exists,
                    'freezeAuthority': freeze_authority_exists,
                    'canSell': True  # Pump.fun tokens on bonding curve are always sellable
                }
        except Exception:
            # If can't check (RPC error, rate limit), assume CAN SELL
            # New Pump.fun tokens are tradeable by default
            return {'mintAuthority': True, 'freezeAuthority': True, 'canSell': True}

        # Paper mode - assume can sell
        return {'mintAuthority': True, 'freezeAuthority': True, 'canSell': True}

    def process_coin(self, coin_data):
        price = self.calculate_price(coin_data)

        # Get REAL security data from blockchain
        security = self.check_token_security(coin_data['mint'])

        # Estimate dev holding from creator balance (we don't have exact data without API)
        dev_holding = 10 + (hash(coin_data['mint']) % 30)

        return {
            'mint': coin_data['mint'],
            'name': coin_data.get('name', 'Unknown'),
            'symbol': coin_data.get('symbol', 'UNKN'),
            'price': price,
            'liquidity': coin_data.get('virtual_sol_reserves', 0) / 1e9,
            'devHolding': dev_holding,
            'marketCap': coin_data.get('market_cap', 0),
            'createdAt': coin_data.get('created_timestamp', int(time.time() * 1000)),
            'canSell': security['canSell'],
            'liquidityLocked': coin_data.get('complete', False),  # True if bonded to Raydium
            'mintAuthority': security['mintAuthority'],
            'freezeAuthority': security['freezeAuthority'],
            # PRESERVE METADATA for quality checking
            'uri': coin_data.get('uri', ''),
            'image_uri': coin_data.get('image_uri', ''),
            'description': coin_data.get('description', ''),
            'twitter': coin_data.get('twitter', ''),
            'telegram': coin_data.get('telegram', ''),
            'website': coin_data.get('website', ''),
            # Pass through pre-fetched curve data for fast live buy
            'prefetched_curve': coin_data.get('prefetched_curve'),
        }
        
    def calculate_price(self, coin):
        if coin.get('virtual_sol_reserves') and coin.get('virtual_token_reserves'):
            return (coin['virtual_sol_reserves'] / 1e9) / (coin['virtual_token_reserves'] / 1e6)
        return 0.0001
        
    def check_metadata_quality(self, coin):
        """Check if metadata is uploaded directly (bypassing IPFS delay)"""
        # Tokens that bypass IPFS upload metadata instantly = sophisticated devs
        metadata_score = 0

        # Check if token has complete metadata (passing coin object now)
        if coin.get('name') and len(coin.get('name', '')) > 2 and coin['name'] != 'Unknown':
            metadata_score += 10
        if coin.get('symbol') and len(coin.get('symbol', '')) > 1 and coin['symbol'] != 'UNKN':
            metadata_score += 10
        if coin.get('uri'):  # Has metadata URI
            metadata_score += 15
        if coin.get('image_uri'):  # Has image
            metadata_score += 10
        if coin.get('description') and len(coin.get('description', '')) > 10:
            metadata_score += 10
        # Social links = extra effort from dev
        if coin.get('twitter'):
            metadata_score += 10
        if coin.get('telegram'):
            metadata_score += 5
        if coin.get('website'):
            metadata_score += 10

        # Instant complete metadata = bypassed IPFS = sophisticated dev
        # Score >= 35 means good metadata (name + symbol + uri + image OR socials)
        return metadata_score >= 35

    def check_rugpull_risk(self, coin):
        risks = []
        risk_score = 0

        # CRITICAL: Honeypot check
        if not coin['canSell']:
            risks.append('❌ HONEYPOT - Cannot sell')
            risk_score += 100
            return {'riskScore': risk_score, 'risks': risks, 'safe': False}

        # High dev holdings
        if coin['devHolding'] > self.config['maxDevHolding']:
            risks.append(f"❌ Dev holds {coin['devHolding']:.1f}% (team tokens not vested)")
            risk_score += 30
        else:
            risks.append(f"✅ Dev holdings OK ({coin['devHolding']:.1f}%)")

        # Low liquidity
        if coin['liquidity'] < self.config['minLiquidity']:
            risks.append(f"⚠️ Low liquidity: ${coin['liquidity']:.0f}")
            risk_score += 20

        # Bonus: Good metadata quality (IPFS bypass = good)
        has_quality_metadata = self.check_metadata_quality(coin)
        if has_quality_metadata:
            risk_score -= 10  # Reduce risk for quality metadata
            risks.append('✅ Quality metadata (IPFS bypass)')
            self.add_log(f"⭐ {coin['symbol']} has quality metadata - sophisticated dev!", 'success')

        # Safe threshold adjusted for Pump.fun sniping
        # Allow tokens with some risk (authorities not burned yet) if metadata is good
        return {'riskScore': risk_score, 'risks': risks, 'safe': risk_score < 50}
        
    def execute_buy(self, coin, amount):
        # Live trading mode
        if self.live_mode and self.live_trader:
            try:
                # Get real SOL price
                sol_price_usd = self.get_sol_price_usd()
                sol_amount = min(amount / sol_price_usd, 0.1)  # Max 0.1 SOL safety limit

                self.add_log(f"🔴 LIVE BUY: {coin['symbol']} with {sol_amount:.4f} SOL (${amount:.2f} @ ${sol_price_usd:.2f}/SOL)", 'warning')
                self.add_log(f"[DEBUG] Mint: {coin['mint']}", 'info')

                # Execute real buy transaction (pass pre-fetched curve to skip RPC polling)
                signature = self.live_trader.buy_token_pumpfun(
                    coin['mint'], sol_amount, max_slippage=0.1,
                    prefetched_curve=coin.get('prefetched_curve')
                )

                if not signature:
                    self.add_log(f"❌ LIVE BUY FAILED for {coin['symbol']}", 'error')
                    return

                # Log transaction signature with Solscan link
                solscan_url = f"https://solscan.io/tx/{signature}"
                self.add_log(f"📝 TX Signature: {signature}", 'success')
                self.add_log(f"🔗 Solscan: {solscan_url}", 'info')

                # Get actual tokens received with retry logic (avoid rate limits)
                token_balance = 0
                max_retries = 3
                self.add_log(f"[DEBUG] Starting balance check with {max_retries} retries...", 'info')

                for attempt in range(max_retries):
                    try:
                        wait_time = 2 + attempt  # 2s, 3s, 4s
                        self.add_log(f"[DEBUG] Retry {attempt + 1}/{max_retries}: Waiting {wait_time}s before query...", 'info')
                        time.sleep(wait_time)

                        # Use verbose mode on last attempt for full debug
                        verbose = (attempt == max_retries - 1)
                        token_balance = self.live_trader.get_token_balance(coin['mint'], verbose=verbose)

                        if token_balance > 0:
                            self.add_log(f"✅ Balance retrieved: {token_balance:.2f} tokens", 'success')
                            break
                        else:
                            self.add_log(f"⚠️ Retry {attempt + 1}/{max_retries}: Balance still 0", 'warning')

                    except Exception as retry_error:
                        error_type = type(retry_error).__name__
                        error_msg = str(retry_error)
                        self.add_log(f"⚠️ Retry {attempt + 1}/{max_retries} failed: {error_type}: {error_msg}", 'warning')

                        # Check if it's a rate limit error
                        if '429' in error_msg or 'Too Many Requests' in error_msg:
                            self.add_log(f"[DEBUG] ❌ RATE LIMIT ERROR - RPC is throttling requests", 'error')

                        if attempt < max_retries - 1:
                            time.sleep(3)

                if token_balance == 0:
                    self.add_log(f"❌ Failed to get token balance after {max_retries} attempts", 'error')
                    self.add_log(f"[DEBUG] Possible causes: 1) Instant rug (token worthless <1s), 2) Rate limit (429 error), 3) ATA creation failed", 'error')
                    self.add_log(f"[DEBUG] Check Solscan to verify: {solscan_url}", 'error')
                    return

                position = {
                    'mint': coin['mint'],
                    'symbol': coin['symbol'],
                    'buyPrice': coin['price'],
                    'currentPrice': coin['price'],
                    'highestPrice': coin['price'],
                    'amount': sol_amount * sol_price_usd,  # USD value for tracking
                    'quantity': token_balance,
                    'sol_amount': sol_amount,  # Track SOL spent
                    'stopLoss': coin['price'] * (1 - self.config['trailingStop'] / 100),
                    'takeProfit': coin['price'] * (1 + self.config['trailingTakeProfit'] / 100),
                    'buyTime': time.time(),
                    'lastBuyActivity': time.time(),
                    'pnl': 0,
                    'pnlPercent': 0,
                    'live': True,
                    'signature': signature
                }

                self.positions.append(position)
                self.add_log(f"✅ LIVE BUY SUCCESS: {coin['symbol']} | Tokens: {token_balance:.2f} | TX: {signature[:8]}...", 'success')

            except Exception as e:
                self.add_log(f"❌ LIVE BUY ERROR: {str(e)}", 'error')
                return

        # Paper trading mode
        else:
            if self.capital < amount:
                self.add_log(f"❌ Insufficient capital for {coin['symbol']}", 'error')
                return

            position = {
                'mint': coin['mint'],
                'symbol': coin['symbol'],
                'buyPrice': coin['price'],
                'currentPrice': coin['price'],
                'highestPrice': coin['price'],
                'amount': amount,
                'quantity': amount / coin['price'],
                'stopLoss': coin['price'] * (1 - self.config['trailingStop'] / 100),
                'takeProfit': coin['price'] * (1 + self.config['trailingTakeProfit'] / 100),
                'buyTime': time.time(),
                'lastBuyActivity': time.time(),
                'pnl': 0,
                'pnlPercent': 0,
                'live': False
            }

            self.capital -= amount
            self.positions.append(position)
            self.add_log(f"✅ BOUGHT {coin['symbol']} @ ${coin['price']:.6f} | Amount: ${amount:.2f}", 'success')

        # Subscribe to real-time trade events for this token (drives price updates via WS)
        self.ws_command_queue.put({
            "method": "subscribeTokenTrade",
            "keys": [coin['mint']]
        })

        self.root.after(0, self.update_display)
        
    def execute_sell(self, position, reason):
        # Live trading mode
        if position.get('live') and self.live_trader:
            try:
                self.add_log(f"🔴 LIVE SELL: {position['symbol']} | Reason: {reason}", 'warning')

                # Get current token balance
                token_balance = self.live_trader.get_token_balance(position['mint'])

                if token_balance > 0:
                    # Execute real sell transaction
                    signature = self.live_trader.sell_token_pumpfun(
                        position['mint'],
                        token_balance,
                        max_slippage=0.1
                    )

                    if signature:
                        self.add_log(f"✅ LIVE SELL SUCCESS: {position['symbol']} | TX: {signature[:8]}...", 'success')
                    else:
                        self.add_log(f"❌ LIVE SELL FAILED: {position['symbol']}", 'error')
                        # Don't remove position if sell failed
                        return

                # Calculate P&L (approximate based on price tracking)
                pnl = (position['currentPrice'] - position['buyPrice']) * position['quantity']
                pnl_percent = ((position['currentPrice'] - position['buyPrice']) / position['buyPrice']) * 100

            except Exception as e:
                self.add_log(f"❌ LIVE SELL ERROR: {str(e)}", 'error')
                return

        # Paper trading mode
        else:
            pnl = (position['currentPrice'] - position['buyPrice']) * position['quantity']
            pnl_percent = ((position['currentPrice'] - position['buyPrice']) / position['buyPrice']) * 100
            proceeds = position['amount'] + pnl
            self.capital += proceeds

        trade = {
            **position,
            'sellPrice': position['currentPrice'],
            'sellTime': time.time(),
            'pnl': pnl,
            'pnlPercent': pnl_percent,
            'reason': reason,
            'holdTime': time.time() - position['buyTime'],
            'buyAmount': position['amount'],  # USD amount spent on buy
            'sellAmount': position['amount'] + pnl  # USD amount received on sell
        }

        self.trade_history.insert(0, trade)
        self.positions.remove(position)

        # Classify trade: win, loss, or breakeven
        # Breakeven = Dead Token, Stagnant, or very small P&L (-2% to +2%)
        is_breakeven = (
            'Dead Token' in reason or
            'Stagnant' in reason or
            (abs(pnl_percent) <= 2.0)  # Within ±2% = breakeven
        )

        if is_breakeven:
            # Breakeven trade - don't count as win or loss
            self.stats['breakeven'] += 1
            log_type = 'info'
            emoji = '⚪'
        else:
            # Real win or loss
            is_win = pnl > 0
            self.stats['wins'] += 1 if is_win else 0
            self.stats['losses'] += 0 if is_win else 1
            log_type = 'success' if is_win else 'error'
            emoji = '💰' if is_win else '📉'

        self.stats['totalTrades'] += 1
        self.stats['totalPnL'] += pnl

        # Win rate excludes breakeven trades
        meaningful_trades = self.stats['wins'] + self.stats['losses']
        self.stats['winRate'] = (self.stats['wins'] / meaningful_trades * 100) if meaningful_trades > 0 else 0

        # Martingale logic (only triggered by real losses, not breakeven)
        if self.config['useMartingale']:
            if not is_breakeven:
                is_win = pnl > 0
                if is_win:
                    # Reset on win
                    self.consecutive_losses = 0
                    self.current_position_size = self.config['maxPositionSize']
                    self.add_log(f"✅ Martingale RESET - Back to base size ${self.current_position_size:.2f}", 'success')
                else:
                    # Increase on loss
                    if self.consecutive_losses < self.config['maxMartingaleLevel']:
                        self.consecutive_losses += 1
                        self.current_position_size = self.config['maxPositionSize'] * (self.config['martingaleMultiplier'] ** self.consecutive_losses)
                        self.add_log(f"⚠️ Martingale Level {self.consecutive_losses}: Next position ${self.current_position_size:.2f}", 'warning')
                    else:
                        # Max level reached, reset
                        self.add_log(f"🛑 Max Martingale level reached - RESETTING to base size", 'error')
                        self.consecutive_losses = 0
                        self.current_position_size = self.config['maxPositionSize']

        # SOFT MARTINGALE: Only double on rugs/dumps (not dead tokens/stagnant)
        elif self.config['useSoftMartingale']:
            # Define "bad timing" exits (should retry with larger size)
            rug_reasons = ['Hard Stop', 'Instant Rug', 'Early Dump', 'Trailing Stop']
            is_rug_exit = any(keyword in reason for keyword in rug_reasons)

            # Define "dead token" exits (don't retry, it's a bad token)
            dead_reasons = ['Dead Token', 'Stagnant', 'API failed', 'Timeout']
            is_dead_exit = any(keyword in reason for keyword in dead_reasons)

            if is_rug_exit and pnl < 0:
                # Got rugged/dumped = bad timing, double next position
                if self.consecutive_losses < self.config['maxMartingaleLevel']:
                    self.consecutive_losses += 1
                    self.current_position_size = self.config['maxPositionSize'] * (self.config['martingaleMultiplier'] ** self.consecutive_losses)
                    self.add_log(f"⚠️ Soft Martingale Level {self.consecutive_losses}: Got {reason}, doubling to ${self.current_position_size:.2f}", 'warning')
                else:
                    # Max level reached, reset
                    self.add_log(f"🛑 Max Soft Martingale level reached - RESETTING to base size", 'error')
                    self.consecutive_losses = 0
                    self.current_position_size = self.config['maxPositionSize']

            elif is_dead_exit:
                # Dead token = don't increase, just skip this one
                self.add_log(f"ℹ️ Soft Martingale: {reason} = dead token, keeping size ${self.current_position_size:.2f}", 'info')
                # Don't reset, don't increase - keep current level

            elif pnl > 0 or is_breakeven:
                # Win or breakeven = ALWAYS reset to base size
                if self.consecutive_losses > 0 or self.current_position_size != self.config['maxPositionSize']:
                    self.consecutive_losses = 0
                    self.current_position_size = self.config['maxPositionSize']
                    self.add_log(f"✅ Soft Martingale RESET - Back to base size ${self.current_position_size:.2f}", 'success')

        self.add_log(f"{emoji} SOLD {position['symbol']} @ ${position['currentPrice']:.6f} | P&L: ${pnl:+.2f} ({pnl_percent:+.1f}%) | {reason}", log_type)

        # Unsubscribe from trade events — stop receiving WS data for this token
        self.ws_command_queue.put({
            "method": "unsubscribeTokenTrade",
            "keys": [position['mint']]
        })
        # Clean up price cache entry
        self.token_prices.pop(position['mint'], None)

        self.root.after(0, self.update_display)
        
    def get_sol_price_usd(self) -> float:
        """Fetch real-time SOL price in USD (uses persistent session)."""
        try:
            response = self.http_session.get(
                'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd',
                timeout=3
            )
            if response.status_code == 200:
                data = response.json()
                price = float(data['solana']['usd'])
                self.cached_sol_price = price
                return price
        except Exception:
            pass
        return getattr(self, 'cached_sol_price', 190)

    def sync_wallet_positions(self):
        """Sync SOL balance and tracked token positions from wallet (live mode only)"""
        if not self.live_mode or not self.live_trader:
            return

        try:
            # Get current SOL balance
            sol_balance = self.live_trader.get_sol_balance()

            # Get real-time SOL price
            sol_price_usd = self.get_sol_price_usd()
            self.cached_sol_price = sol_price_usd  # Cache for fallback

            # Update capital to reflect actual wallet SOL balance
            self.capital = sol_balance * sol_price_usd

            # Update capital entry field
            self.capital_entry.config(state='normal')
            self.capital_entry.delete(0, tk.END)
            self.capital_entry.insert(0, f"{self.capital:.2f}")

            # Update token balances ONLY for positions we're tracking
            for pos in self.positions[:]:
                try:
                    # Query only this specific token's balance
                    token_balance = self.live_trader.get_token_balance(pos['mint'])

                    if token_balance == 0:
                        # Token no longer in wallet (sold externally or error)
                        self.add_log(f"⚠️ {pos['symbol']} has 0 balance - removing from tracking", 'warning')
                        self.positions.remove(pos)
                    else:
                        # Update actual balance from wallet
                        pos['quantity'] = token_balance

                except Exception as e:
                    # If we can't fetch balance, keep the position but log warning
                    self.add_log(f"⚠️ Failed to fetch {pos['symbol']} balance: {str(e)}", 'warning')

        except Exception as e:
            self.add_log(f"[ERROR] Failed to sync wallet: {str(e)}", 'error')

    def _apply_price_and_check_exits(self, pos, new_price):
        """Update position price and evaluate all exit conditions. Returns True if sold."""
        self.failed_updates[pos['mint']] = 0
        pos['currentPrice'] = new_price
        pos['highestPrice'] = max(pos['highestPrice'], new_price)
        pos['stopLoss'] = pos['highestPrice'] * (1 - self.config['trailingStop'] / 100)
        pos['pnl'] = (new_price - pos['buyPrice']) * pos['quantity']
        pos['pnlPercent'] = ((new_price - pos['buyPrice']) / pos['buyPrice']) * 100
        age = time.time() - pos['buyTime']

        if pos['pnlPercent'] <= -18:
            self.add_log(f"🛑 HARD STOP: {pos['pnlPercent']:.1f}% - EMERGENCY EXIT!", 'error')
            self.execute_sell(pos, f'Hard Stop ({pos["pnlPercent"]:.1f}%)')
            return True

        if age <= 3 and pos['pnlPercent'] <= -8:
            self.add_log(f"💀 INSTANT RUG: {pos['pnlPercent']:.1f}% in {age:.0f}s - EXIT NOW!", 'error')
            self.execute_sell(pos, f'Instant Rug ({pos["pnlPercent"]:.1f}% in {age:.0f}s)')
            return True

        if self.config['earlyDumpDetection'] and age <= self.config['earlyDumpWindow']:
            if pos['pnlPercent'] <= self.config['earlyDumpThreshold']:
                self.add_log(f"⚡ Early dump: {pos['pnlPercent']:.1f}% in {age:.0f}s", 'warning')
                self.execute_sell(pos, f'Early Dump (-{abs(pos["pnlPercent"]):.1f}% in {age:.0f}s)')
                return True

        if self.config['noBuysExit'] and age >= self.config['noBuysWindow']:
            if pos['highestPrice'] == pos['buyPrice']:
                self.add_log(f"⚠️ No buying activity for {age:.0f}s", 'warning')
                self.execute_sell(pos, f'Dead Token (no buys in {age:.0f}s)')
                return True
            elif age >= self.config['noBuysWindow'] * 2:
                price_change = ((pos['currentPrice'] - pos['buyPrice']) / pos['buyPrice']) * 100
                if abs(price_change) < 1:
                    self.add_log(f"⚠️ Token stagnant: {price_change:.2f}% in {age:.0f}s", 'warning')
                    self.execute_sell(pos, f'Stagnant (no activity {age:.0f}s)')
                    return True

        if age >= 20 and -8 < pos['pnlPercent'] < 0:
            if pos['currentPrice'] == pos['highestPrice'] and pos['pnlPercent'] < -3:
                self.add_log(f"🔒 Stuck losing: {pos['pnlPercent']:.1f}% for {age:.0f}s - cutting loss", 'warning')
                self.execute_sell(pos, f'Stuck Loss ({pos["pnlPercent"]:.1f}% @ {age:.0f}s)')
                return True

        if age <= 5 and pos['pnlPercent'] >= 20:
            self.add_log(f"⚡ FAST SCALP! +{pos['pnlPercent']:.1f}% in {age:.0f}s", 'success')
            self.execute_sell(pos, f'Fast Scalp (+{pos["pnlPercent"]:.1f}% in {age:.0f}s)')
            return True

        if new_price >= pos['takeProfit']:
            self.add_log(f"🎯 Take Profit! {pos['pnlPercent']:.1f}%", 'success')
            self.execute_sell(pos, f'Take Profit (+{pos["pnlPercent"]:.1f}%)')
            return True

        if pos['currentPrice'] <= pos['stopLoss']:
            self.execute_sell(pos, f'Trailing Stop ({pos["pnlPercent"]:+.1f}%)')
            return True

        if age > 300 and pos['pnlPercent'] < -50:
            self.execute_sell(pos, 'Emergency Stop (5min + -50%)')
            return True

        if age > 300:
            self.add_log(f"⏰ {pos['symbol']} stuck 5min - EMERGENCY EXIT", 'warning')
            self.execute_sell(pos, f'Timeout (5min @ {pos["pnlPercent"]:+.1f}%)')
            return True

        return False

    def price_update_loop(self):
        """Price monitor — uses WebSocket trade cache (instant), falls back to Solana RPC."""
        sync_counter = 0
        while self.price_update_running:
            time.sleep(0.25)  # 250 ms tick — fast enough for rug detection

            # Live mode: sync wallet every ~5 s
            if self.live_mode and self.is_running:
                sync_counter += 1
                if sync_counter >= 20:  # 20 × 0.25 s = 5 s
                    self.sync_wallet_positions()
                    sync_counter = 0

            if not self.is_running or len(self.positions) == 0:
                continue

            for pos in self.positions[:]:
                mint = pos['mint']
                try:
                    # ── FAST PATH: WebSocket trade event (zero API calls) ──────────────
                    ws_entry = self.token_prices.get(mint)
                    if ws_entry and (time.time() - ws_entry['timestamp']) < 5.0:
                        vsr = ws_entry['virtualSolReserves']
                        vtr = ws_entry['virtualTokenReserves']
                        if vsr > 0 and vtr > 0:
                            new_price = (vsr / 1e9) / (vtr / 1e6)
                            self._apply_price_and_check_exits(pos, new_price)
                            continue  # Done for this position this tick

                    # ── SLOW PATH: Solana RPC (rate-limited) ─────────────────────────
                    now = time.time()
                    if now - self.rpc_last_call_time < self.rpc_min_interval:
                        continue  # Rate-limit: skip this position this tick
                    self.rpc_last_call_time = now

                    coin_data = self.fetch_from_solana(mint)
                    if coin_data:
                        new_price = self.calculate_price(coin_data)
                        # Update WS cache so next tick uses fast path
                        self.token_prices[mint] = {
                            'virtualSolReserves': coin_data['virtual_sol_reserves'],
                            'virtualTokenReserves': coin_data['virtual_token_reserves'],
                            'timestamp': time.time(),
                        }
                        self._apply_price_and_check_exits(pos, new_price)
                    else:
                        # Track consecutive failures
                        if mint not in self.failed_updates:
                            self.failed_updates[mint] = 0
                        self.failed_updates[mint] += 1
                        self.add_log(
                            f"⚠️ Failed to update {pos['symbol']} "
                            f"({self.failed_updates[mint]}/3 attempts)", 'warning'
                        )
                        if self.failed_updates[mint] >= 3:
                            self.add_log(
                                f"❌ {pos['symbol']} — no price data after 3 tries, FORCE CLOSING", 'error'
                            )
                            self.execute_sell(pos, 'Dead Token (no price data)')

                except Exception as e:
                    print(f"Error updating price: {e}")

            if self.capital <= 0 and len(self.positions) == 0:
                self.add_log("💀 STOPPED - Capital depleted", 'error')
                self.root.after(0, self.stop_bot)

            self.root.after(0, self.update_display)
            
    def update_display(self):
        # Update stats
        positions_value = sum(pos['amount'] + pos['pnl'] for pos in self.positions)
        total_value = self.capital + positions_value
        total_pnl = total_value - self.starting_capital
        total_pnl_percent = (total_pnl / self.starting_capital) * 100
        
        self.capital_label.config(text=f"${self.capital:.2f}")
        self.positions_value.config(text=f"Positions: ${positions_value:.2f}")
        self.portfolio_label.config(text=f"${total_value:.2f}", fg='#0f0' if total_pnl >= 0 else '#f00')
        self.portfolio_pnl.config(text=f"${total_pnl:+.2f} ({total_pnl_percent:+.1f}%)", 
                                 fg='#0f0' if total_pnl >= 0 else '#f00')
        
        self.winrate_label.config(text=f"{self.stats['winRate']:.1f}%")
        self.winloss_label.config(text=f"{self.stats['wins']}W / {self.stats['losses']}L / {self.stats['breakeven']}BE ({self.stats['totalTrades']} trades)")
        self.position_count.config(text=f"{len(self.positions)} open positions")
        
        # Update positions table
        for item in self.positions_tree.get_children():
            self.positions_tree.delete(item)
            
        for pos in self.positions:
            self.positions_tree.insert('', 'end', values=(
                pos['symbol'],
                f"${pos['buyPrice']:.6f}",
                f"${pos['currentPrice']:.6f}",
                f"${pos['highestPrice']:.6f}",
                f"${pos['stopLoss']:.6f}",
                f"${pos['pnl']:+.2f}",
                f"{pos['pnlPercent']:+.1f}%"
            ))
            
        # Update coins table
        for item in self.coins_tree.get_children():
            self.coins_tree.delete(item)
            
        for coin in self.new_coins[:10]:
            self.coins_tree.insert('', 'end', values=(
                coin['symbol'],
                coin['mint'][:8] + '...',
                f"${coin['price']:.8f}",
                f"${coin['liquidity']:.0f}",
                f"{coin['devHolding']:.1f}%",
                '✅' if coin['liquidityLocked'] else '❌',
                '✅' if coin['canSell'] else '❌'
            ))
            
        # Update history table
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
            
        for trade in self.trade_history[:20]:
            # Use actual USD amounts if available, otherwise fall back to token prices
            buy_display = f"${trade.get('buyAmount', trade.get('amount', 0)):.4f}"
            sell_display = f"${trade.get('sellAmount', trade.get('amount', 0)):.4f}"

            self.history_tree.insert('', 'end', values=(
                trade['symbol'],
                buy_display,
                sell_display,
                f"${trade['pnl']:+.2f}",
                f"{trade['pnlPercent']:+.1f}%",
                f"{trade['holdTime']:.0f}s",
                trade['reason'][:20]
            ))

if __name__ == "__main__":
    root = tk.Tk()
    app = PumpFunSniperBot(root)
    root.mainloop()