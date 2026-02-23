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
from solana.rpc.commitment import Processed
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address
import struct
# Direct file logger — flushes every line (Windows buffers logging.FileHandler)
_debug_file = open('live_debug.log', 'w', encoding='utf-8')

class _DebugLog:
    def info(self, msg):
        _debug_file.write(f"{datetime.now().strftime('%H:%M:%S')} {msg}\n")
        _debug_file.flush()
    def debug(self, msg):
        self.info(msg)
    def warning(self, msg):
        self.info(f"WARN {msg}")

_log = _DebugLog()

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
        self.starting_sol_balance = 0.0  # SOL balance at bot start (for real P&L tracking)
        self.positions: List[Dict] = []
        self.trade_history: List[Dict] = []
        self.new_coins: List[Dict] = []
        self.stats = {'totalTrades': 0, 'wins': 0, 'losses': 0, 'breakeven': 0, 'totalPnL': 0, 'winRate': 0}

        # Failed update tracking (Solution 1)
        self.failed_updates: Dict[str, int] = {}  # {mint: consecutive_fail_count}

        # Live trading
        self.live_mode = False
        self.live_trader = None

        # Configuration - OPTIMIZED v2 (fixed "buying at the top" + added fast scalps)
        self.config = {
            'maxPositionSize': 0.05,
            'trailingStop': 5,           # 5% — widened to hold through dips before second waves (was 4%)
            'trailingTakeProfit': 15,    # 15% — take profit before the dump comes
            'momentumStaleSeconds': 7,   # Sell if no buys for 7s while profitable (was 5s — raised to hold through brief pauses between waves)
            'latencySlippagePct': 2,     # 2% buffer for execution delay (Bloodmoon showed ~2% actual slippage)
            'minLiquidity': 3,           # $3 min - filters ultra-low-cap rugpulls
            'maxDevHolding': 12,         # 12% max - stricter rug detection
            'maxTopHolder': 25,          # 25% max single non-BC wallet — raised from 20 to reduce over-blocking
            'minBurnerHolderPct': 8.0,   # check holders with > 8% of supply for burner status (was 5)
            'burnerTxThreshold': 5,       # wallets with < 5 lifetime txs are flagged as burners
            'minBurnersToBlock': 3,       # 3+ burner wallets = hard block (was 2, raised to reduce false blocks)
            'autoTrade': True,           # ENABLED for auto-sniping
            'maxOpenPositions': 1,       # Only 1 position at a time (focus strategy)
            'useMartingale': False,      # Enable/disable Martingale on losses
            'useSoftMartingale': False,  # OPTIMIZED: Disabled for small budget testing
            'martingaleMultiplier': 2.0, # Double position after loss
            'maxMartingaleLevel': 3,     # Max 3 levels (1x, 2x, 4x, then reset)
            'earlyDumpDetection': True,  # Exit if price dumps in first 15s
            'earlyDumpThreshold': -7,    # Exit at -7% in first 15s — cut losses fast
            'earlyDumpWindow': 15,       # OPTIMIZED: 10 → 15 (more time before triggering)
            'noBuysExit': True,          # Exit if no buys detected
            'noBuysWindow': 3,           # 3s — exit dead tokens faster; frees capital for next opportunity
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
        # Tokens waiting for first buy activity before we enter
        # {mint: {'coin': coin_data, 'amount': position_size, 'created': time.time()}}
        self.pending_buys: Dict[str, dict] = {}

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
        
        # Trading window indicator
        tw_frame = tk.Frame(self.root, bg='#111111', relief='solid', bd=1)
        tw_frame.pack(fill='x', padx=10, pady=(0, 4))
        self.create_trading_window_panel(tw_frame)

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
        
    def create_trading_window_panel(self, parent):
        """Dynamic CET trading window indicator — green 15:00-02:00, red otherwise."""
        from datetime import timezone, timedelta
        # Left: live status indicator (updates every 30s)
        self.tw_status_label = tk.Label(
            parent, text="", bg='#111111', font=('Courier', 11, 'bold'), width=42, anchor='w'
        )
        self.tw_status_label.pack(side='left', padx=(12, 0), pady=4)

        # Separator
        tk.Label(parent, text="│", bg='#111111', fg='#444', font=('Courier', 11)).pack(side='left', padx=6)

        # Right: static schedule legend
        sched = tk.Frame(parent, bg='#111111')
        sched.pack(side='left', pady=4)
        rows = [
            ("🟢", "15:00–23:00 CET", "US market open — peak volume, real buyers"),
            ("🟢", "23:00–02:00 CET", "US evening  — most +30%+ scalps happen here"),
            ("🔴", "02:00–09:00 CET", "Dead zone   — bots & rugs, 18% win rate"),
            ("🔴", "09:00–15:00 CET", "US asleep   — pump.fun barely active"),
        ]
        for icon, window, desc in rows:
            row = tk.Frame(sched, bg='#111111')
            row.pack(anchor='w')
            tk.Label(row, text=icon,    bg='#111111', font=('Courier', 9), width=2).pack(side='left')
            tk.Label(row, text=window,  bg='#111111', fg='#ccc', font=('Courier', 9, 'bold'), width=18).pack(side='left')
            tk.Label(row, text=desc,    bg='#111111', fg='#666', font=('Courier', 9)).pack(side='left')

        self._update_trading_window()

    def _update_trading_window(self):
        from datetime import datetime, timezone, timedelta
        cet = datetime.now(timezone.utc) + timedelta(hours=1)  # CET = UTC+1 (Paris winter)
        h, m = cet.hour, cet.minute
        time_str = cet.strftime('%H:%M')
        in_window = (h >= 15) or (h < 2)
        if in_window:
            text  = f"🟢  {time_str} CET  —  PRIME TIME  ·  Run the bot"
            color = '#00ff44'
        elif 2 <= h < 9:
            text  = f"🔴  {time_str} CET  —  DEAD ZONE   ·  Pause — bots & rugs"
            color = '#ff4444'
        else:
            text  = f"🟡  {time_str} CET  —  LOW VOLUME  ·  Avoid until 15:00"
            color = '#ffaa00'
        self.tw_status_label.config(text=text, fg=color)
        self.root.after(30000, self._update_trading_window)  # refresh every 30s

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
        
        # SOL P&L (real trading P&L in SOL, unaffected by price fluctuations)
        card_sol = tk.Frame(parent, bg='#0a0a0a', relief='solid', bd=1)
        card_sol.pack(side='left', fill='both', expand=True, padx=5, pady=5)

        tk.Label(card_sol, text="SOL P&L (session)", bg='#0a0a0a', fg='#888', font=('Courier', 8)).pack()
        self.sol_pnl_label = tk.Label(card_sol, text="0.0000 SOL", bg='#0a0a0a', fg='#888', font=('Courier', 14, 'bold'))
        self.sol_pnl_label.pack()
        self.sol_pnl_sub = tk.Label(card_sol, text="start bot to track", bg='#0a0a0a', fg='#555', font=('Courier', 8))
        self.sol_pnl_sub.pack()

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
        self.position_entry.insert(0, "2.00")  # $2 min — fees are 6% not 24% of position
        self.position_entry.pack(side='left', padx=5)

        tk.Label(row1, text="Trailing Stop (%):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.trailing_entry = tk.Entry(row1, width=10, font=('Courier', 9))
        self.trailing_entry.insert(0, "4")  # 4% - tighter for fast scalping
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
        self.liquidity_entry.insert(0, "3")  # $3 min - filters ultra-low-cap rugpulls
        self.liquidity_entry.pack(side='left', padx=5)

        tk.Label(row2, text="Max Dev Holding (%):", bg='#1a1a1a', fg='#0f0', font=('Courier', 9)).pack(side='left', padx=5)
        self.devhold_entry = tk.Entry(row2, width=10, font=('Courier', 9))
        self.devhold_entry.insert(0, "12")  # 12% max - stricter rug detection
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
            
        # Snapshot SOL balance at start for real P&L tracking
        if self.live_mode and self.live_trader:
            try:
                self.starting_sol_balance = self.live_trader.get_sol_balance()
            except Exception:
                self.starting_sol_balance = 0.0
        else:
            self.starting_sol_balance = 0.0

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
        """Cache real-time price from a WebSocket trade event (zero API calls).

        Unit normalization:
          PumpPortal newToken events  → virtualSolReserves  in lamports (e.g. 30_000_000_000)
                                        virtualTokenReserves in raw units (e.g. 1_073_000_000_000_000)
          PumpPortal tokenTrades events → vSolInBondingCurve  in SOL     (e.g. 30.5)
                                          vTokensInBondingCurve in tokens (e.g.  793_000_000)
        The price loop formula (vsr/1e9)/(vtr/1e6) assumes raw units.
        Normalise here so the formula stays valid for both sources.
        """
        mint = data.get('mint')
        if not mint:
            return
        vsr = data.get('vSolInBondingCurve') or data.get('virtualSolReserves')
        vtr = data.get('vTokensInBondingCurve') or data.get('virtualTokenReserves')
        if vsr and vtr:
            vsr = float(vsr)
            vtr = float(vtr)
            # Detect human-unit SOL (< 1e6 = definitely < 1 million SOL) → convert to lamports
            if vsr < 1e6:
                vsr *= 1e9
            # Detect human-unit tokens (< 1e12 = not raw ×1e6 yet) → convert to raw units
            if vtr < 1e12:
                vtr *= 1e6
            self.token_prices[mint] = {
                'virtualSolReserves': vsr,
                'virtualTokenReserves': vtr,
                'timestamp': time.time(),
                'txType': data.get('txType', ''),
            }
            # Real-time exit check for tokens we hold positions in.
            # The 0.25s price_update_loop misses bursts of sells that gap through
            # the -8% hard stop.  Checking HERE catches the FIRST breaching sell.
            for pos in self.positions:
                if pos['mint'] == mint and not pos.get('_selling'):
                    price = (vsr / 1e9) / (vtr / 1e6)
                    tx = data.get('txType', '')
                    _log.info(f"TRADE_EVENT {mint[:12]} vsr={vsr:.4e} vtr={vtr:.4e} "
                              f"price={price:.12e} txType={tx}")

                    # Track consecutive sells and buy momentum
                    if tx == 'sell':
                        pos['consecutive_sells'] = pos.get('consecutive_sells', 0) + 1
                    elif tx == 'buy':
                        # Only reset sell streak if the buy actually moves price up (not dust)
                        prev_price = pos.get('currentPrice', pos['buyPrice'])
                        price_move_pct = ((price - prev_price) / prev_price) * 100 if prev_price > 0 else 0
                        if price_move_pct >= 0.5:
                            pos['consecutive_sells'] = 0
                        pos['last_buy_time'] = time.time()
                        pos['buy_count_held'] = pos.get('buy_count_held', 0) + 1

                    # Keep cached reserves fresh — used to skip RPC on sell
                    pos['cached_vsr'] = int(vsr)
                    pos['cached_vtr'] = int(vtr)

                    pnl_pct = ((price - pos['buyPrice']) / pos['buyPrice']) * 100
                    consec = pos.get('consecutive_sells', 0)
                    slip = self.config.get('latencySlippagePct', 2)  # execution slippage buffer

                    # All thresholds shifted by slippage to account for ~2s execution delay.
                    # Losses: trigger earlier (less negative) so actual exit ≈ intended level.
                    # Profits: require higher profit so actual exit is still profitable.
                    if pnl_pct <= -(10 - slip):  # -8% trigger → ~-10% actual
                        pos['_selling'] = True
                        pos['_urgent_price'] = price
                        pos['_urgent_reason'] = 'Emergency Stop'
                        _log.info(f"EMERGENCY_EXIT {pos['symbol']} pnl={pnl_pct:.1f}% — absolute floor (trigger -{10-slip}%)")
                    elif pnl_pct <= -(8 - slip) and consec >= 2:  # -6% trigger → ~-8% actual
                        pos['_selling'] = True
                        pos['_urgent_price'] = price
                        pos['_urgent_reason'] = 'Hard Stop'
                        _log.info(f"URGENT_EXIT {pos['symbol']} pnl={pnl_pct:.1f}% sells={consec} (trigger -{8-slip}%)")
                    # MOMENTUM EXIT: profitable + sells starting → lock in gains.
                    # Raised from 2 consec/+4% → 3 consec/+8% to let big runners breathe past early sell waves.
                    elif pnl_pct >= (6 + slip) and consec >= 3:  # +8% trigger → ~+6% actual
                        pos['_selling'] = True
                        pos['_urgent_price'] = price
                        pos['_urgent_reason'] = 'Momentum Exit'
                        _log.info(f"MOMENTUM_EXIT {pos['symbol']} pnl={pnl_pct:.1f}% sells={consec} (trigger +{6+slip}%)")
                    # Break-even: trigger while still positive so actual exit ≈ breakeven.
                    # Floor raised from <+2% → <+3% (slip+1): TX latency is ~3s — if price drops
                    # from +4% to +3% and we trigger now, the sell lands at ~0% instead of -4%.
                    elif pnl_pct < (slip + 1) and ((pos['highestPrice'] - pos['buyPrice']) / pos['buyPrice']) * 100 >= 3:
                        pos['_selling'] = True
                        pos['_urgent_price'] = price
                        pos['_urgent_reason'] = 'Break-Even Exit'
                        _log.info(f"BREAKEVEN_EXIT {pos['symbol']} pnl={pnl_pct:.1f}% (was +3%+, trigger <+{slip+1}%)")
                    # Sell cascade: 5+ consecutive sells AND losing (was 3 — raised to avoid GROVEIFY-type exits on dips before pumps)
                    elif consec >= 5 and pnl_pct < -(3 - slip):  # -1% trigger → ~-3% actual
                        pos['_selling'] = True
                        pos['_urgent_price'] = price
                        pos['_urgent_reason'] = 'Sell Cascade'
                        _log.info(f"SELL_CASCADE {pos['symbol']} {consec} consecutive sells, "
                                  f"pnl={pnl_pct:.1f}% (trigger -{3-slip}%)")
                    break  # only one position per mint

            # Check if this is a trade event for a pending token
            tx_type = data.get('txType', '')
            if mint in self.pending_buys:
                pending = self.pending_buys[mint]
                current_price = (vsr / 1e9) / (vtr / 1e6)
                creation_price = pending['creation_price']
                price_gain = ((current_price - creation_price) / creation_price) * 100
                # Track SOL volume: how much SOL flowed in since creation
                sol_volume = (vsr - pending['creation_vsr']) / 1e9  # SOL added to curve

                if tx_type == 'buy':
                    pending['buy_count'] = pending.get('buy_count', 0) + 1
                    if 'first_buy_time' not in pending:
                        pending['first_buy_time'] = time.time()  # track when buying actually started
                    pending.setdefault('buy_times', []).append(time.time())
                    if len(pending['buy_times']) > 8:
                        pending['buy_times'].pop(0)
                    # Track largest single buy in SOL (vsr delta between events).
                    # A single buy >0.80 SOL is a whale pumping to bait bots — even if later
                    # smaller buys bring the average down, the initial whale is likely the dumper.
                    _prev_vsr = pending.get('last_vsr', pending['creation_vsr'])
                    _single_buy_sol = (vsr - _prev_vsr) / 1e9
                    if _single_buy_sol > pending.get('max_single_buy', 0):
                        pending['max_single_buy'] = _single_buy_sol
                pending['last_vsr'] = vsr  # update every event (buy or sell)
                if tx_type == 'sell':
                    pending['sell_count'] = pending.get('sell_count', 0) + 1
                    # Track largest single-sell price drop seen during monitoring.
                    # If one sell dumps the price >1.5%, it's a whale unloading → skip.
                    _prev_price = pending.get('last_price', current_price)
                    if _prev_price > 0 and _prev_price > current_price:
                        _sell_drop_pct = (_prev_price - current_price) / _prev_price * 100
                        if _sell_drop_pct > pending.get('max_sell_drop', 0):
                            pending['max_sell_drop'] = _sell_drop_pct
                # Update last seen price for sell-drop tracking
                pending['last_price'] = current_price
                # Rolling window of last 5 tx types for momentum quality check
                if tx_type in ('buy', 'sell'):
                    recent = pending.setdefault('recent_txs', [])
                    recent.append(tx_type)
                    if len(recent) > 5:
                        recent.pop(0)

                # Buy acceleration: compare first-half vs second-half buy rate.
                # Decelerating buy momentum = crowd already leaving = likely dead token.
                # Requires 4+ buy events; defaults to True (pass) when insufficient data.
                _buy_times = pending.get('buy_times', [])
                _is_accelerating = True
                _accel_ratio = None
                if len(_buy_times) >= 4:
                    _mid = len(_buy_times) // 2
                    _sf = max(_buy_times[_mid - 1] - _buy_times[0], 0.001)
                    _ss = max(_buy_times[-1]        - _buy_times[_mid], 0.001)
                    _rf = (_mid - 1) / _sf if _mid > 1 else 0
                    _rs = (len(_buy_times) - _mid - 1) / _ss if (len(_buy_times) - _mid) > 1 else 0
                    if _rf > 0:
                        _accel_ratio = _rs / _rf
                        _is_accelerating = _accel_ratio >= 0.5  # second half ≥ 50% as fast as first

                # Acceleration is used as a blocking filter only (rejects decelerating tokens).
                # Entry thresholds are fixed — lowering them for fast accel let rugs through (MILANO -17.5%).

                # Track if anyone is selling early (red flag — could be bait-and-dump)
                if tx_type == 'sell' and pending.get('buy_count', 0) <= 2:
                    # Sell before meaningful buys = dump pattern
                    self.pending_buys.pop(mint)
                    self.add_log(f"🚩 {pending['coin']['symbol']} — early sell detected, skipping (dump pattern)", 'warning')
                    self.ws_command_queue.put({
                        "method": "unsubscribeTokenTrade",
                        "keys": [mint]
                    })
                # SCALP-FOCUSED entry: only enter early (+3-7%), skip anything pumped.
                # +7%+: late entry disabled — by the time TX confirms (~3s), momentum exhausted.
                #       Coprolite +6.5% entered → already -3% on first update. ACE ~+10% → rug.
                elif price_gain > 20:
                    self.pending_buys.pop(mint)
                    self.add_log(f"🚩 {pending['coin']['symbol']} — already +{price_gain:.0f}% (too late), skipping", 'warning')
                    self.ws_command_queue.put({
                        "method": "unsubscribeTokenTrade",
                        "keys": [mint]
                    })
                elif price_gain >= 7:
                    # Late entry disabled — TX takes ~3s, token peaked by confirmation time.
                    # Losses: CHEEZYCA -4%, Kiki -12.9%, ACE -3.4%, Coprolite -6.1%
                    pass
                elif (price_gain >= 3
                      and pending.get('buy_count', 0) >= 1 and sol_volume >= 0.4
                      and pending.get('sell_count', 0) < pending.get('buy_count', 0)
                      and pending.get('recent_txs', []).count('sell') < 2
                      and pending.get('max_sell_drop', 0) < 1.5
                      and pending.get('max_single_buy', 0) <= 0.65
                      and tx_type == 'buy'
                      and (sol_volume / max(pending.get('buy_count', 1), 1)) <= 0.50
                      and (sol_volume / max(pending.get('buy_count', 1), 1)) >= 0.07
                      and (time.time() - pending['created']) >= 1.5
                      and (time.time() - pending['created']) <= 15
                      and _is_accelerating):
                    # ENTRY: min gain 3%, min 1 buy, vol ≥ 0.4 SOL, avg buy 0.06–0.50 SOL, age 1.5–15s.
                    # avg_buy ≤ 0.50: blocks pure mega-whale pumps (>0.50 SOL avg).
                    # avg_buy ≥ 0.07: blocks bot-farmed volume (Javis 0.055 -9.7%, CITY 0.061 -6.5%).
                    # max_single_buy ≤ 0.65: blocks bait-and-dump — one whale pumps >0.65 SOL to pump
                    #   price, then places smaller buys to normalize avg, then dumps after bots enter.
                    #   Caught: Shit (1.21 SOL first buy -7.2%), RAGENALD (0.664 SOL first buy -10.1%).
                    # sell_dump<1.5%: blocks entries where a whale already dumped during monitoring.
                    # recent_txs: skip if 2+ of last 5 trades are sells (momentum cooling).
                    pending.pop('_near_miss_logged', None)  # clear flag on actual entry
                    self.pending_buys.pop(mint)
                    coin = pending['coin']
                    amount = pending['amount']
                    coin['virtual_sol_reserves'] = vsr
                    coin['virtual_token_reserves'] = vtr
                    coin['price'] = current_price
                    if coin.get('prefetched_curve'):
                        coin['prefetched_curve']['virtual_sol_reserves'] = int(vsr)
                        coin['prefetched_curve']['virtual_token_reserves'] = int(vtr)
                    wait_time = time.time() - pending['created']
                    self.add_log(f"🚀 EARLY ENTRY: {coin['symbol']} +{price_gain:.1f}% | {pending['buy_count']} buys | {sol_volume:.2f} SOL vol | {wait_time:.1f}s", 'success')
                    _log.info(f"PENDING_TRIGGER {coin['symbol']} mint={mint[:12]} "
                              f"waited={wait_time:.1f}s price={coin['price']:.12e} "
                              f"gain={price_gain:.1f}% buys={pending['buy_count']} vol={sol_volume:.2f}")

                    # Re-check we still have capacity and capital
                    if (len(self.positions) < self.config['maxOpenPositions'] and
                            self.capital >= amount):
                        # BUY_GATE (dev/top/burner RPC checks) removed — always returned 0% for
                        # all tokens due to RPC propagation delay on fresh mints. Cost: 500-1500ms.
                        # Protection now comes from: sell_dump<1.5% entry filter, trailing stop,
                        # instant-rug exit, and sell-cascade detection during position monitoring.
                        self.execute_buy(coin, amount)
                    else:
                        reason = f"positions={len(self.positions)}/{self.config['maxOpenPositions']}" if len(self.positions) >= self.config['maxOpenPositions'] else f"capital={self.capital:.2f}<{amount:.2f}"
                        self.add_log(f"⏸️ {coin['symbol']} - can't buy (max positions or low capital)", 'info')
                        _log.info(f"BUY_SKIP {coin['symbol']} mint={mint[:12]} reason={reason}")
                        self.ws_command_queue.put({
                            "method": "unsubscribeTokenTrade",
                            "keys": [mint]
                        })
                elif price_gain >= 3 and tx_type == 'buy' and not pending.get('_near_miss_logged'):
                    # Token hit gain threshold but failed other entry conditions.
                    # Log once per token so we can diagnose which filter is too strict.
                    pending['_near_miss_logged'] = True
                    buy_count  = pending.get('buy_count', 0)
                    sell_count = pending.get('sell_count', 0)
                    recent_sells = pending.get('recent_txs', []).count('sell')
                    avg_buy = sol_volume / max(buy_count, 1)
                    reasons = []
                    if buy_count < 1:                        reasons.append(f"buys={buy_count}<1")
                    if sol_volume < 0.4:                     reasons.append(f"vol={sol_volume:.2f}<0.4")
                    if recent_sells >= 2:                    reasons.append(f"recent_sells={recent_sells}/5")
                    if avg_buy > 0.50:                       reasons.append(f"avg_buy={avg_buy:.3f}>0.50")
                    if sell_count >= buy_count:              reasons.append(f"sells({sell_count})>=buys({buy_count})")
                    _sell_drop = pending.get('max_sell_drop', 0)
                    if _sell_drop >= 1.5:                    reasons.append(f"sell_dump={_sell_drop:.1f}%>1.5%")
                    _max_single = pending.get('max_single_buy', 0)
                    if _max_single > 0.65:                   reasons.append(f"single_buy={_max_single:.2f}SOL>0.65")
                    if avg_buy < 0.07:                       reasons.append(f"avg_buy={avg_buy:.3f}<0.07(bot_farm)")
                    token_age = time.time() - pending['created']
                    if token_age < 1.5:                      reasons.append(f"token_age={token_age:.1f}s<1.5s(too_fresh)")
                    if token_age > 15:                       reasons.append(f"token_age={token_age:.0f}s>15s")
                    if _accel_ratio is not None and not _is_accelerating: reasons.append(f"decel={_accel_ratio:.2f}<0.5")
                    reason_str = ', '.join(reasons) if reasons else 'unknown'
                    _log.info(f"NEAR_MISS {pending['coin']['symbol']} gain={price_gain:.1f}% buys={buy_count} vol={sol_volume:.2f} — {reason_str}")
                    self.add_log(f"⚡ near miss: {pending['coin']['symbol']} +{price_gain:.1f}% — {reason_str}", 'info')

    def handle_new_coin(self, data):
        # Sanitize symbol/name — PumpPortal sends Unicode/emojis that crash Windows console
        symbol = data.get('symbol', data['mint'][:6])
        name = data.get('name', symbol)
        symbol = symbol.encode('ascii', 'ignore').decode('ascii').strip() or data['mint'][:6]
        name = name.encode('ascii', 'ignore').decode('ascii').strip() or symbol
        self.add_log(f"🆕 New token detected: {symbol}", 'info')

        try:
            # FAST PATH: PumpPortal new-token events already contain all reserves + metadata.
            # Use them directly — zero HTTP / RPC calls required.
            vsr = data.get('virtualSolReserves') or data.get('vSolInBondingCurve')
            vtr = data.get('virtualTokenReserves') or data.get('vTokensInBondingCurve')

            if vsr and vtr:
                # Normalise reserves to raw units so the price formula
                # (vsr/1e9)/(vtr/1e6) is consistent with trade-event updates.
                # PumpPortal newToken may send human-unit values (SOL, tokens)
                # or raw-unit values (lamports, raw tokens) depending on version.
                # Same thresholds used in handle_trade_event.
                vsr_raw = float(vsr)
                vtr_raw = float(vtr)
                if vsr_raw < 1e6:    # Human SOL (e.g. 30.0) → lamports
                    vsr_raw *= 1e9
                if vtr_raw < 1e12:   # Human tokens (e.g. 793_000_000) → raw units
                    vtr_raw *= 1e6

                # Build prefetched_curve so the live buy can skip BC_RETRIES RPC polling.
                # traderPublicKey = creator (needed for creator-vault PDA).
                trader_key = data.get('traderPublicKey')
                bc_key     = data.get('bondingCurveKey')
                prefetched_curve = None
                if trader_key:
                    try:
                        prefetched_curve = {
                            'virtual_token_reserves': int(vtr_raw),
                            'virtual_sol_reserves':   int(vsr_raw),
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
                    'creator_address': trader_key or '',
                    'bonding_curve_key': bc_key or '',
                    'virtual_sol_reserves': vsr_raw,
                    'virtual_token_reserves': vtr_raw,
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
                # Seed the price cache with normalised raw-unit values so the first
                # price-update cycle uses the same scale as subsequent trade events.
                self.token_prices[data['mint']] = {
                    'virtualSolReserves': vsr_raw,
                    'virtualTokenReserves': vtr_raw,
                    'timestamp': time.time(),
                }
            else:
                # Fallback: reserves not in event → query Solana RPC once
                coin_data = self.fetch_from_solana(data['mint'], symbol=symbol, name=name)
                if not coin_data:
                    coin_data = self.fetch_coin_data_direct(data['mint'])
                # Seed price cache from RPC data so the fast path works after buy
                if coin_data and coin_data.get('virtual_sol_reserves') and coin_data.get('virtual_token_reserves'):
                    self.token_prices[data['mint']] = {
                        'virtualSolReserves': float(coin_data['virtual_sol_reserves']),
                        'virtualTokenReserves': float(coin_data['virtual_token_reserves']),
                        'timestamp': time.time(),
                    }

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

                    # Don't buy immediately — wait for confirmed momentum.
                    # Require price +3% above creation AND at least 2 buy events
                    # AND at least 0.3 SOL total volume. Enter EARLY before the wave peaks.
                    self.pending_buys[coin['mint']] = {
                        'coin': coin,
                        'amount': position_amount,
                        'created': time.time(),
                        'creation_price': coin['price'],
                        'creation_vsr': coin.get('liquidity', 30) * 1e9,  # SOL reserves at creation (lamports)
                        'buy_count': 0,
                        'sell_count': 0,
                        'buy_times': [],
                    }
                    self.ws_command_queue.put({
                        "method": "subscribeTokenTrade",
                        "keys": [coin['mint']]
                    })
                    self.add_log(f"👀 WATCHING {coin['symbol']} — waiting for first buy...", 'info')
                elif not risk['safe']:
                    failing = [r for r in risk['risks'] if r.startswith('❌') or r.startswith('⚠️')]
                    reject_reason = failing[0] if failing else risk['risks'][0]
                    self.add_log(f"🚫 REJECTED {coin['symbol']} | Score:{risk['riskScore']} | {reject_reason}", 'warning')
                    _log.info(f"REJECTED {coin['symbol']} mint={coin['mint'][:12]} score={risk['riskScore']} reason={reject_reason} dev={coin.get('devHolding',0):.1f}% top={coin.get('topHolderPct',0):.1f}% burners={coin.get('burnerWalletCount',0)}")
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

    def get_holder_risk_data(self, mint_address: str, bonding_curve_key: str,
                             check_burners: bool = True) -> tuple:
        """Return (max_holder_pct, burner_count).

        max_holder_pct: largest single holder % excluding the bonding curve (0.0 on failure).
        burner_count: number of significant holders (>minBurnerHolderPct) whose owner wallet
                      has fewer than burnerTxThreshold lifetime transactions — indicates a
                      freshly-funded burner wallet used for coordinated dump schemes.

        check_burners=False: skip per-holder RPC calls (fast path for initial token evaluation).
        check_burners=True:  full check including getAccountInfo + getSignaturesForAddress per candidate.
        """
        try:
            mint_pubkey = Pubkey.from_string(mint_address)
            bc_ata = None
            if bonding_curve_key:
                try:
                    bc_ata = str(get_associated_token_address(
                        Pubkey.from_string(bonding_curve_key), mint_pubkey
                    ))
                except Exception:
                    pass
            resp = self.solana_client.get_token_largest_accounts(mint_pubkey, commitment=Processed)
            if not resp.value:
                return (0.0, 0)
            max_pct = 0.0
            candidates = []  # (ata_address_str, pct) for significant non-BC holders
            min_pct = self.config.get('minBurnerHolderPct', 5.0)
            for account in resp.value:
                ata_str = str(account.address)
                if bc_ata and ata_str == bc_ata:
                    continue  # skip bonding curve — it holds unsold supply
                if account.ui_amount is not None:
                    pct = (float(account.ui_amount) / 1_000_000_000) * 100
                    if pct > max_pct:
                        max_pct = pct
                    if pct > min_pct and len(candidates) < 3:
                        candidates.append((ata_str, pct))

            # Fast path: skip per-holder RPC calls when called at creation time.
            # Burner check runs at buy trigger time instead (see execute_buy gate).
            if not check_burners:
                _log.info(
                    f"HOLDER_CHECK mint={mint_address[:12]} top={max_pct:.1f}% "
                    f"candidates={len(candidates)} burners=skipped"
                )
                return (max_pct, 0)

            # Burner wallet check: fetch owner of each significant holder ATA,
            # then count their lifetime transactions. Fresh (<5 txs) = burner.
            burner_count = 0
            tx_threshold = self.config.get('burnerTxThreshold', 5)
            for ata_addr, pct in candidates:
                try:
                    resp2 = self.solana_client.get_account_info(
                        Pubkey.from_string(ata_addr), commitment=Processed
                    )
                    if not (resp2.value and resp2.value.data and len(resp2.value.data) >= 64):
                        continue
                    # SPL token account layout: bytes 0-31 = mint, bytes 32-63 = owner pubkey
                    owner_bytes = bytes(resp2.value.data[32:64])
                    owner_pubkey = Pubkey.from_bytes(owner_bytes)
                    sigs = self.solana_client.get_signatures_for_address(owner_pubkey, limit=10)
                    tx_count = len(sigs.value) if sigs.value else 0
                    if tx_count < tx_threshold:
                        burner_count += 1
                        _log.info(
                            f"BURNER_WALLET detected: owner={str(owner_pubkey)[:16]}... "
                            f"txs={tx_count} pct={pct:.1f}%"
                        )
                except Exception:
                    pass  # skip this holder on any RPC error

            _log.info(
                f"HOLDER_CHECK mint={mint_address[:12]} top={max_pct:.1f}% "
                f"candidates={len(candidates)} burners={burner_count}"
            )
            return (max_pct, burner_count)
        except Exception:
            pass
        return (0.0, 0)

    def get_dev_holding_pct(self, mint_address: str, creator_address: str) -> float:
        """Fetch the creator's token holding % via RPC. Returns 0.0 on failure."""
        if not creator_address:
            return 0.0
        try:
            mint_pubkey    = Pubkey.from_string(mint_address)
            creator_pubkey = Pubkey.from_string(creator_address)
            ata = get_associated_token_address(creator_pubkey, mint_pubkey)
            resp = self.solana_client.get_token_account_balance(ata, commitment=Processed)
            if resp.value and resp.value.ui_amount is not None:
                # pump.fun total supply is always 1,000,000,000 tokens
                return (float(resp.value.ui_amount) / 1_000_000_000) * 100
        except Exception:
            pass
        return 0.0

    def process_coin(self, coin_data):
        price = self.calculate_price(coin_data)

        # Only non-RPC check at creation — honeypot / mintAuthority / freezeAuthority.
        # In paper mode this returns hardcoded safe values instantly (~0ms).
        # All RPC security checks (dev holding, top holder, burner wallets) are deferred
        # to buy trigger time so trade events are tracked from the very first millisecond.
        security = self.check_token_security(coin_data['mint'])

        return {
            'mint': coin_data['mint'],
            'name': coin_data.get('name', 'Unknown'),
            'symbol': coin_data.get('symbol', 'UNKN'),
            'price': price,
            'liquidity': coin_data.get('virtual_sol_reserves', 0) / 1e9,
            # RPC security fields — populated at buy trigger time with fresh data
            'devHolding': 0.0,
            'topHolderPct': 0.0,
            'burnerWalletCount': 0,
            # Pass creator + bonding curve addresses for security checks at buy time
            'creator_address': coin_data.get('creator_address', ''),
            'bonding_curve_key': coin_data.get('bonding_curve_key', ''),
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
        """Check if token has basic on-chain metadata.

        PumpPortal WebSocket events include name, symbol and metadata URI directly
        from the creation transaction. Social links (twitter/telegram/website) live
        inside the off-chain IPFS JSON and are NOT included in PumpPortal events,
        so they cannot be used as a filter here.

        Minimum bar: token must have a real name, real symbol, and a metadata URI.
        Tokens with no name/symbol/URI get a +40 penalty in check_rugpull_risk.
        Dead tokens that pass this check are caught by the 8s noBuysWindow exit.
        """
        has_name   = bool(coin.get('name')   and len(coin.get('name',   '')) > 2 and coin['name']   != 'Unknown')
        has_symbol = bool(coin.get('symbol') and len(coin.get('symbol', '')) > 1 and coin['symbol'] != 'UNKN')
        has_uri    = bool(coin.get('uri'))
        return has_name and has_symbol and has_uri

    def check_rugpull_risk(self, coin):
        risks = []
        risk_score = 0

        # CRITICAL: Honeypot check
        if not coin['canSell']:
            risks.append('❌ HONEYPOT - Cannot sell')
            risk_score += 100
            return {'riskScore': risk_score, 'risks': risks, 'safe': False}

        # Top holder concentration — hard block (flash dump whale risk)
        if coin.get('topHolderPct', 0) > self.config.get('maxTopHolder', 20):
            risks.append(f"❌ Top holder {coin['topHolderPct']:.1f}% — flash dump risk")
            risk_score += 50  # hard block

        # Burner wallet detection — coordinated multi-wallet dump risk
        burner_count = coin.get('burnerWalletCount', 0)
        min_burners = self.config.get('minBurnersToBlock', 2)
        if burner_count >= min_burners:
            risks.append(f"❌ {burner_count} burner wallets — coordinated dump risk")
            risk_score += 50  # hard block
            _log.info(f"MULTI_WALLET_RUG {coin['symbol']} mint={coin['mint'][:12]} burners={burner_count}")
        elif burner_count == 1:
            risks.append(f"⚠️ 1 burner wallet detected")
            risk_score += 15
            _log.info(f"BURNER_WALLET {coin['symbol']} mint={coin['mint'][:12]} burners=1")

        # High dev holdings — hard block (dev can dump entire position at any time)
        if coin['devHolding'] > self.config['maxDevHolding']:
            risks.append(f"❌ Dev holds {coin['devHolding']:.1f}% — rug risk")
            risk_score += 50  # pushes past safe threshold of 40 → hard block
        else:
            risks.append(f"✅ Dev holdings OK ({coin['devHolding']:.1f}%)")

        # Liquidity check removed from scoring.
        # minLiquidity config is in USD ($3) but coin['liquidity'] is in SOL (always 30+).
        # The comparison was always False for real tokens and only penalised when reserve
        # data was unavailable (producing a false $0 reading).
        # Metadata + social link filter handles low-effort tokens instead.

        # Metadata quality — HARD BLOCK. Only buy tokens with quality metadata.
        has_quality_metadata = self.check_metadata_quality(coin)
        if not has_quality_metadata:
            risks.append('❌ No metadata — skipping')
            return {'riskScore': 100, 'risks': risks, 'safe': False}

        risks.append('✅ Quality metadata')
        self.add_log(f"⭐ {coin['symbol']} has quality metadata", 'success')

        return {'riskScore': risk_score, 'risks': risks, 'safe': risk_score < 40}
        
    def execute_buy(self, coin, amount):
        # Live trading mode
        if self.live_mode and self.live_trader:
            try:
                # Get real SOL price
                sol_price_usd = self.get_sol_price_usd()
                sol_amount = min(amount / sol_price_usd, 0.2)  # Max 0.2 SOL safety limit

                self.add_log(f"🔴 LIVE BUY: {coin['symbol']} with {sol_amount:.4f} SOL (${amount:.2f} @ ${sol_price_usd:.2f}/SOL)", 'warning')
                self.add_log(f"[DEBUG] Mint: {coin['mint']}", 'info')

                # Execute real buy transaction (pass pre-fetched curve to skip RPC polling)
                signature = self.live_trader.buy_token_pumpfun(
                    coin['mint'], sol_amount, max_slippage=0.25,
                    prefetched_curve=coin.get('prefetched_curve')
                )

                if not signature:
                    self.add_log(f"❌ LIVE BUY FAILED for {coin['symbol']}", 'error')
                    _log.info(f"BUY_FAIL {coin['symbol']} mint={coin['mint'][:12]} reason=no_signature")
                    return

                # Log transaction signature with Solscan link
                solscan_url = f"https://solscan.io/tx/{signature}"
                self.add_log(f"📝 TX Signature: {signature}", 'success')
                self.add_log(f"🔗 Solscan: {solscan_url}", 'info')

                # Get actual tokens received — get_token_balance already retries
                # 5 times with 1s sleeps internally, so just a brief wait + single call
                time.sleep(0.3)  # tiny delay for RPC propagation
                token_balance = self.live_trader.get_token_balance(coin['mint'], verbose=True)

                if token_balance == 0:
                    self.add_log(f"❌ Failed to get token balance after 5 attempts", 'error')
                    self.add_log(f"[DEBUG] Possible causes: 1) Instant rug (token worthless <1s), 2) Rate limit (429 error), 3) ATA creation failed", 'error')
                    self.add_log(f"[DEBUG] Check Solscan to verify: {solscan_url}", 'error')
                    return

                usd_amount = sol_amount * sol_price_usd

                # Calculate ACTUAL buy price from what we paid vs what we received.
                # sol_amount / token_balance = effective SOL per token (the real execution price).
                # Previous approach (RPC bonding curve read) was wrong — it captured a
                # random snapshot AFTER the tx, often at a peak, causing false hard stops.
                actual_price = coin['price']  # fallback: trigger price
                if token_balance > 0:
                    actual_price = sol_amount / token_balance
                    _log.info(f"BUY_PRICE_UPDATE {coin['symbol']} trigger={coin['price']:.12e} actual={actual_price:.12e} (sol={sol_amount}/tokens={token_balance:.2f})")
                else:
                    _log.info(f"BUY_PRICE_UPDATE {coin['symbol']} trigger={coin['price']:.12e} actual=FALLBACK (no token_balance)")

                position = {
                    'mint': coin['mint'],
                    'symbol': coin['symbol'],
                    'buyPrice': actual_price,
                    'currentPrice': actual_price,
                    'highestPrice': actual_price,
                    'amount': usd_amount,
                    'quantity': usd_amount / actual_price,  # Same formula as paper — P&L comes out in USD
                    'token_balance': token_balance,  # Actual tokens for on-chain sell
                    'sol_amount': sol_amount,
                    'stopLoss': actual_price * (1 - self.config['trailingStop'] / 100),
                    'takeProfit': actual_price * (1 + self.config['trailingTakeProfit'] / 100),
                    'buyTime': time.time(),
                    'lastBuyActivity': time.time(),
                    'pnl': 0,
                    'pnlPercent': 0,
                    'live': True,
                    'signature': signature,
                    'consecutive_sells': 0,
                    # Cached curve data — skip bonding curve RPC on sell
                    'cached_creator': (coin.get('prefetched_curve') or {}).get('creator'),
                    'cached_vsr': int(coin.get('virtual_sol_reserves', 0)),
                    'cached_vtr': int(coin.get('virtual_token_reserves', 0)),
                }

                self.positions.append(position)
                self.add_log(f"✅ LIVE BUY SUCCESS: {coin['symbol']} | Tokens: {token_balance:.2f} | TX: {signature[:8]}...", 'success')

            except Exception as e:
                self.add_log(f"❌ LIVE BUY ERROR: {str(e)}", 'error')
                _log.info(f"BUY_FAIL {coin['symbol']} mint={coin['mint'][:12]} reason=exception err={str(e)[:80]}")
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
                'live': False,
                'consecutive_sells': 0,
            }

            self.capital -= amount
            self.positions.append(position)
            self.add_log(f"✅ BOUGHT {coin['symbol']} @ ${coin['price']:.6f} | Amount: ${amount:.2f}", 'success')

        # Subscribe to real-time trade events for this token (drives price updates via WS)
        self.ws_command_queue.put({
            "method": "subscribeTokenTrade",
            "keys": [coin['mint']]
        })

        # Debug: log buy details
        pos = self.positions[-1]
        _log.info(f"BUY {pos['symbol']} mint={coin['mint'][:12]} "
                  f"buyPrice={pos['buyPrice']:.12e} quantity={pos['quantity']:.6e} "
                  f"amount={pos['amount']:.4f} live={pos.get('live',False)} "
                  f"cache={'YES' if coin['mint'] in self.token_prices else 'NO'}")

        self.root.after(0, self.update_display)
        
    def execute_sell(self, position, reason):
        # Live trading mode
        if position.get('live') and self.live_trader:
            try:
                self.add_log(f"🔴 LIVE SELL: {position['symbol']} | Reason: {reason}", 'warning')

                # Use stored token_balance — skip redundant RPC balance check
                # sell_token_pumpfun has its own get_token_balance which would add 1-5s delay
                token_balance = position.get('token_balance', 0)
                if token_balance <= 0:
                    # Fallback: query on-chain if we somehow lost the stored balance
                    token_balance = self.live_trader.get_token_balance(position['mint'])

                if token_balance > 0:
                    # Build cached curve from WS data to skip bonding curve RPC on sell
                    cached_curve = None
                    creator = position.get('cached_creator')
                    vsr = position.get('cached_vsr', 0)
                    vtr = position.get('cached_vtr', 0)
                    if creator and vsr > 0 and vtr > 0:
                        cached_curve = {
                            'creator': creator,
                            'virtual_sol_reserves': vsr,
                            'virtual_token_reserves': vtr,
                            'real_token_reserves': 0,
                            'real_sol_reserves': 0,
                        }
                    # Execute real sell transaction (skip balance + curve RPC if cache available)
                    signature = self.live_trader.sell_token_pumpfun(
                        position['mint'],
                        token_balance,
                        max_slippage=0.1,
                        skip_balance_check=True,
                        cached_curve=cached_curve
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

        # Debug: log sell details
        _log.info(f"SELL {position['symbol']} reason={reason} live={position.get('live',False)} "
                  f"buyPrice={position['buyPrice']:.12e} currentPrice={position['currentPrice']:.12e} "
                  f"quantity={position['quantity']:.6e} pnl={pnl:.6f} pnl%={pnl_percent:.2f} "
                  f"amount={position['amount']:.4f} holdTime={time.time()-position['buyTime']:.1f}s")

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
                        # Update actual token balance (used for on-chain sell)
                        pos['token_balance'] = token_balance

                except Exception as e:
                    # If we can't fetch balance, keep the position but log warning
                    self.add_log(f"⚠️ Failed to fetch {pos['symbol']} balance: {str(e)}", 'warning')

        except Exception as e:
            self.add_log(f"[ERROR] Failed to sync wallet: {str(e)}", 'error')

    def _apply_price_and_check_exits(self, pos, new_price):
        """Update position price and evaluate all exit conditions. Returns True if sold."""
        if pos.get('_selling'):
            return True  # Already flagged / being sold — skip
        self.failed_updates[pos['mint']] = 0
        pos['currentPrice'] = new_price
        pos['highestPrice'] = max(pos['highestPrice'], new_price)
        # Dynamic trailing stop: tighten as profit grows
        peak_gain_pct = ((pos['highestPrice'] - pos['buyPrice']) / pos['buyPrice']) * 100
        if peak_gain_pct >= 10:
            effective_trail = 2   # Very tight at +10%+ — lock in gains
        elif peak_gain_pct >= 5:
            effective_trail = 3   # Moderate at +5-10%
        else:
            effective_trail = self.config['trailingStop']  # Default 5%
        pos['stopLoss'] = pos['highestPrice'] * (1 - effective_trail / 100)

        # Tiered profit floor: as peak rises, lock in progressively higher exits.
        # Each tier adds a hard floor so winners can't fully reverse.
        #   Peak +3% → floor at +1%  (exit green, not break-even)
        #   Peak +5% → floor at +3%  (lock in meaningful gain)
        #   Peak +8% → floor at +5%  (protect strong winners)
        if peak_gain_pct >= 8:
            profit_floor_pct = 5.0
        elif peak_gain_pct >= 5:
            profit_floor_pct = 3.0
        elif peak_gain_pct >= 3:
            profit_floor_pct = 1.0
        else:
            profit_floor_pct = 0.0
        if profit_floor_pct > 0:
            pos['stopLoss'] = max(pos['stopLoss'], pos['buyPrice'] * (1 + profit_floor_pct / 100))

        pos['pnl'] = (new_price - pos['buyPrice']) * pos['quantity']
        pos['pnlPercent'] = ((new_price - pos['buyPrice']) / pos['buyPrice']) * 100
        age = time.time() - pos['buyTime']
        slip = self.config.get('latencySlippagePct', 2)  # execution slippage buffer

        # All loss thresholds shifted by slippage: trigger earlier so actual exit ≈ intended.
        # All profit thresholds shifted by slippage: require more profit so actual exit is still green.
        if pos['pnlPercent'] <= -(10 - slip):  # -8% trigger → ~-10% actual
            self.add_log(f"🛑 EMERGENCY STOP: {pos['pnlPercent']:.1f}%!", 'error')
            self.execute_sell(pos, f'Emergency Stop ({pos["pnlPercent"]:.1f}%)')
            return True

        consec = pos.get('consecutive_sells', 0)
        if pos['pnlPercent'] <= -(8 - slip) and consec >= 2:  # -6% trigger → ~-8% actual
            self.add_log(f"🛑 HARD STOP: {pos['pnlPercent']:.1f}% ({consec} sells) - EXIT!", 'error')
            self.execute_sell(pos, f'Hard Stop ({pos["pnlPercent"]:.1f}%)')
            return True

        if age <= 5 and pos['pnlPercent'] <= -(5 - slip) and consec >= 2:  # -3% trigger → ~-5% actual
            self.add_log(f"💀 INSTANT RUG: {pos['pnlPercent']:.1f}% in {age:.0f}s - EXIT NOW!", 'error')
            self.execute_sell(pos, f'Instant Rug ({pos["pnlPercent"]:.1f}% in {age:.0f}s)')
            return True

        if self.config['earlyDumpDetection'] and age <= self.config['earlyDumpWindow']:
            if pos['pnlPercent'] <= self.config['earlyDumpThreshold'] + slip:  # -5% trigger → ~-7% actual
                self.add_log(f"⚡ Early dump: {pos['pnlPercent']:.1f}% in {age:.0f}s", 'warning')
                self.execute_sell(pos, f'Early Dump (-{abs(pos["pnlPercent"]):.1f}% in {age:.0f}s)')
                return True

        # MOMENTUM RIDE: ride while buys flow, exit when they stop.
        # Need +slip% extra so actual exit is still profitable after execution delay.
        if pos['pnlPercent'] >= (2 + slip):  # +4% trigger → ~+2% actual
            last_buy = pos.get('last_buy_time', pos['buyTime'])
            since_last_buy = time.time() - last_buy
            stale_threshold = self.config.get('momentumStaleSeconds', 5)
            # Momentum died: no buy event for N seconds while in profit → take it
            if since_last_buy >= stale_threshold:
                self.add_log(f"💰 MOMENTUM SELL: +{pos['pnlPercent']:.1f}% (no buys for {since_last_buy:.0f}s)", 'success')
                self.execute_sell(pos, f'Momentum Sell (+{pos["pnlPercent"]:.1f}% @ {age:.1f}s)')
                return True

        # Profit fading: peaked at +3%+ but dropped 3%+ from peak → lock in remainder
        # (was 2% — raised to 3% so tokens can breathe without false-fading on noise)
        if peak_gain_pct >= 3 and pos['pnlPercent'] < peak_gain_pct - 4:
            self.add_log(f"📉 Profit fading: was +{peak_gain_pct:.1f}%, now +{pos['pnlPercent']:.1f}%", 'warning')
            self.execute_sell(pos, f'Profit Fade (+{pos["pnlPercent"]:.1f}% from peak +{peak_gain_pct:.1f}%)')
            return True

        if self.config['noBuysExit']:
            last_buy = pos.get('last_buy_time', pos['buyTime'])
            since_last_buy = time.time() - last_buy
            # Only fire dead-token exit when NOT already in profit.
            # Profitable positions (≥3% pnl) are handled by Momentum Sell above with a
            # 5s stale timer — skipping noBuysExit here gives them the full 5s window
            # so second-wave pumps aren't cut short by the 3s dead-token timer.
            if since_last_buy >= self.config['noBuysWindow'] and pos['pnlPercent'] < (2 + slip):
                self.add_log(f"⚠️ No buys for {since_last_buy:.0f}s (pnl={pos['pnlPercent']:.1f}%)", 'warning')
                self.execute_sell(pos, f'Dead Token (no buys in {since_last_buy:.0f}s)')
                return True

        # Dead and losing: price hasn't moved AND we're in the red → exit after 20s
        if age >= 20 and pos['pnlPercent'] < -3:
            # Check if price is stuck (hasn't moved in recent ticks)
            price_gain_from_buy = ((pos['highestPrice'] - pos['buyPrice']) / pos['buyPrice']) * 100
            if price_gain_from_buy < 2:
                self.add_log(f"🔒 Stuck losing: {pos['pnlPercent']:.1f}% for {age:.0f}s (peak only +{price_gain_from_buy:.1f}%)", 'warning')
                self.execute_sell(pos, f'Stuck Loss ({pos["pnlPercent"]:.1f}% @ {age:.0f}s)')
                return True

        # Stuck in profit: price frozen (no trades happening), take profit and move on.
        # Frees up the position slot for new opportunities.
        if age >= 20 and pos['pnlPercent'] >= 3:
            # Check if price is barely moving (current ≈ last update = stuck)
            price_diff_from_peak = ((pos['highestPrice'] - pos['currentPrice']) / pos['highestPrice']) * 100
            if price_diff_from_peak < 2:  # within 2% of peak = price stalled
                self.add_log(f"💰 Stuck Profit: +{pos['pnlPercent']:.1f}% frozen for {age:.0f}s — taking profit", 'success')
                self.execute_sell(pos, f'Stuck Profit (+{pos["pnlPercent"]:.1f}% @ {age:.0f}s)')
                return True

        # FAST SCALP: Take quick profit in first 15s while momentum is hot.
        # Target: lock in +8% before the dump. MOMENTUM_EXIT now raised to +8% so we're not cut at +4% first.
        if age <= 15 and pos['pnlPercent'] >= 8:
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
            time.sleep(0.10)  # 100 ms tick — faster exit detection

            # Live mode: sync wallet every ~5 s
            if self.live_mode and self.is_running:
                sync_counter += 1
                if sync_counter >= 50:  # 50 × 0.10 s = 5 s
                    self.sync_wallet_positions()
                    sync_counter = 0

            # Clean up stale pending buys (no buy activity within 30s → dead token)
            now = time.time()
            stale = [m for m, p in self.pending_buys.items() if now - p['created'] > 30]
            for mint in stale:
                pending = self.pending_buys.pop(mint)
                self.add_log(f"💀 {pending['coin']['symbol']} — no buys in 30s, skipping", 'info')
                self.ws_command_queue.put({
                    "method": "unsubscribeTokenTrade",
                    "keys": [mint]
                })

            if not self.is_running or len(self.positions) == 0:
                continue

            for pos in self.positions[:]:
                mint = pos['mint']

                # ── URGENT EXIT: flagged by handle_trade_event in real-time ──────
                # WS handler already decided to sell — execute immediately, don't re-evaluate.
                # Bug fix: _apply_price_and_check_exits was re-evaluating and finding no match
                # for SELL_CASCADE at -1.4% (OpenAIGate: -1.4% → sat 24s → -27.7%).
                if pos.get('_urgent_price'):
                    urgent_price = pos.pop('_urgent_price')
                    reason = pos.pop('_urgent_reason', 'Urgent Exit')
                    pos['_selling'] = False
                    pos['currentPrice'] = urgent_price
                    pos['pnlPercent'] = ((urgent_price - pos['buyPrice']) / pos['buyPrice']) * 100
                    _log.info(f"URGENT_SELL {pos['symbol']} processing at {urgent_price:.12e}")
                    self.execute_sell(pos, f'{reason} ({pos["pnlPercent"]:.1f}%)')
                    continue

                try:
                    # ── FAST PATH: WebSocket trade event (zero API calls) ──────────────
                    # Cache lifetime 30s — live buys take 5-7s (tx + balance check),
                    # so the 5s expiry was killing the cache before the position existed.
                    ws_entry = self.token_prices.get(mint)
                    if ws_entry and (time.time() - ws_entry['timestamp']) < 30.0:
                        vsr = ws_entry['virtualSolReserves']
                        vtr = ws_entry['virtualTokenReserves']
                        if vsr > 0 and vtr > 0:
                            new_price = (vsr / 1e9) / (vtr / 1e6)
                            age = time.time() - pos['buyTime']
                            _log.debug(f"FAST {pos['symbol']} vsr={vsr:.4e} vtr={vtr:.4e} "
                                       f"new_price={new_price:.12e} buyPrice={pos['buyPrice']:.12e} "
                                       f"pnl%={(new_price-pos['buyPrice'])/pos['buyPrice']*100:.2f} age={age:.1f}s")
                            self._apply_price_and_check_exits(pos, new_price)
                            continue  # Done for this position this tick
                        else:
                            _log.warning(f"FAST SKIP {pos['symbol']} vsr={vsr} vtr={vtr} (zero reserves)")
                    else:
                        cache_age = (time.time() - ws_entry['timestamp']) if ws_entry else -1
                        _log.debug(f"NO CACHE {pos['symbol']} mint={mint[:12]} "
                                   f"cache_exists={ws_entry is not None} cache_age={cache_age:.1f}s")

                    # ── SLOW PATH: Solana RPC (rate-limited) ─────────────────────────
                    now = time.time()
                    if now - self.rpc_last_call_time < self.rpc_min_interval:
                        # Even without new price data, check time-based exits
                        # (noBuysWindow, timeout) so they aren't blocked by RPC failures.
                        age = now - pos['buyTime']
                        if self.config['noBuysExit']:
                            last_buy = pos.get('last_buy_time', pos['buyTime'])
                            since_last_buy = now - last_buy
                            if since_last_buy >= self.config['noBuysWindow']:
                                self.execute_sell(pos, f'Dead Token (no buys in {since_last_buy:.0f}s)')
                        elif age > 300:
                            self.execute_sell(pos, f'Timeout (5min)')
                        continue
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
                        # Track consecutive failures — force exit fast if no price data.
                        if mint not in self.failed_updates:
                            self.failed_updates[mint] = 0
                        self.failed_updates[mint] += 1
                        age = time.time() - pos['buyTime']
                        if self.failed_updates[mint] >= 5 or age > 30:
                            self.add_log(
                                f"❌ {pos['symbol']} — no price data ({self.failed_updates[mint]} fails, {age:.0f}s), FORCE CLOSING", 'error'
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

        # SOL P&L card — tracks real SOL gained/lost since session start (immune to price swings)
        if self.live_mode and self.live_trader and self.starting_sol_balance > 0:
            try:
                current_sol = self.live_trader.get_sol_balance()
                sol_pnl = current_sol - self.starting_sol_balance
                sol_color = '#0f0' if sol_pnl >= 0 else '#f00'
                self.sol_pnl_label.config(text=f"{sol_pnl:+.4f} SOL", fg=sol_color)
                sol_price = getattr(self, 'cached_sol_price', 190)
                self.sol_pnl_sub.config(text=f"(${sol_pnl * sol_price:+.3f} USD)", fg=sol_color)
            except Exception:
                pass
        elif self.starting_sol_balance == 0:
            self.sol_pnl_label.config(text="0.0000 SOL", fg='#888')
            self.sol_pnl_sub.config(text="start bot to track", fg='#555')
        
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

        # Dynamic average % in column header
        if self.trade_history:
            avg_pct = sum(t['pnlPercent'] for t in self.trade_history) / len(self.trade_history)
            avg_color = '+' if avg_pct >= 0 else ''
            self.history_tree.heading('%', text=f"% (avg {avg_color}{avg_pct:.1f}%)")
        else:
            self.history_tree.heading('%', text='%')

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