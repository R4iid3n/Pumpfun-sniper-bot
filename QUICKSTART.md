# Quick Start

Get the bot running in 5 minutes.

---

## 1. Install Dependencies

Requires **Python 3.11+**

```bash
pip install -r requirements.txt
```

---

## 2. Paper Trading (no wallet needed)

```bash
python pumpfun_bot.py
```

Click **START** — the bot connects to PumpPortal WebSocket, detects new tokens, and simulates trades in real time. No SOL, no wallet, no risk.

---

## 3. Live Trading Setup

### Step 1 — Create your wallet config

```bash
cp wallet_config.example.json wallet_config.json
```

Then edit `wallet_config.json`:

```json
{
  "private_key": [1, 2, 3, ...],
  "rpc_url": "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY",
  "max_slippage_bps": 500,
  "priority_fee_lamports": 100000,
  "compute_unit_limit": 200000,
  "use_jito_mev_protection": false,
  "jito_tip_lamports": 100000,
  "jito_block_engine_url": "https://mainnet.block-engine.jito.wtf"
}
```

> `wallet_config.json` is in `.gitignore` — it will never be committed.

### Step 2 — Get your private key bytes

If you have a base58 private key (from Phantom, Solflare, etc.):

```bash
python convert_key.py
```

Paste your base58 key when prompted. It outputs the 64-byte array for `private_key`.

### Step 3 — Get a private RPC URL

The free public Solana RPC is too rate-limited for sniping. Get a free key at [helius.xyz](https://helius.xyz) and set it in `rpc_url`.

### Step 4 — Fund your wallet

Send at least **0.1 SOL** to your wallet address. The bot keeps a 0.02 SOL reserve for gas fees.

Check your balance:

```bash
python check_balance.py
```

### Step 5 — Sanity test (recommended before full bot)

```bash
python sanity_test.py --sol 0.001 --slippage 0.20 --wait 5
```

This buys 0.001 SOL of the next new pump.fun token and immediately sells it. Confirms the full buy/sell pipeline is working. Expected output:

```
[OK] Buy confirmed in ~2000 ms
[OK] Sell confirmed in ~2300 ms
[OK] Net cost: ~0.00005 SOL  (fees only)
BOTH TRANSACTIONS EXECUTED SUCCESSFULLY
```

### Step 6 — Enable live trading in the GUI

```bash
python pumpfun_bot.py
```

1. Check the **LIVE TRADING** checkbox
2. Confirm the warning dialog
3. Click **START**

---

## 4. Jito MEV Protection (optional)

To route transactions through Jito's block engine for better landing rates:

In `wallet_config.json`:
```json
"use_jito_mev_protection": true,
"jito_tip_lamports": 100000
```

No API key needed. Adds ~0.0001 SOL tip per transaction. Recommended for competitive tokens.

---

## 5. Key Files

| File | Purpose |
|---|---|
| `pumpfun_bot.py` | Main GUI bot (paper + live) |
| `live_trader.py` | Live trade execution engine |
| `sanity_test.py` | One-shot buy+sell pipeline test |
| `wallet_config.json` | Your wallet + RPC config (gitignored) |
| `wallet_config.example.json` | Template — copy this |
| `convert_key.py` | Convert base58 private key → byte array |
| `check_balance.py` | Check wallet SOL balance |
| `requirements.txt` | Python dependencies |
| `pumpfun-sniper-rust/` | Rust implementation (faster, headless) |

---

## 6. Rust Bot (headless, faster)

```bash
cd pumpfun-sniper-rust
cp .env.example .env
# Edit .env — add your RPC URL and private key
cargo run --release
```

Set `DRY_RUN=true` in `.env` for paper trading (default). Set `DRY_RUN=false` for live.

---

## Common Issues

**`429 Too Many Requests`** — You're using the public RPC. Set a private RPC in `wallet_config.json`.

**`Buy confirmed but Sell failed (Custom:11)`** — Fixed in current version. Was a float precision bug leaving dust tokens.

**`Bonding curve not ready after retries`** — The token may have been a failed launch. Retry with the next token.

**`Slippage exceeded (6001/6003)`** — The price moved before your tx landed. The bot auto-escalates slippage and retries up to 3 times.
