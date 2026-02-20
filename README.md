# Pump.fun Sniper Bot

A high-performance Python sniping bot for [pump.fun](https://pump.fun) tokens on Solana. Detects new token launches via WebSocket, executes buy+sell transactions on the bonding curve, and supports both paper trading and live trading.

**Tested buy speed: ~2,000ms end-to-end** (detection → confirmed on-chain).

---

## Architecture

```
pumpfun_bot.py       — Main GUI bot (tkinter) — paper + live trading
live_trader.py       — Live trading engine (Solana transactions)
sanity_test.py       — CLI: one buy + immediate sell (pipeline health check)
pumpfunlib/          — Low-level pump.fun bonding curve library
pumpfun-sniper-rust/ — High-performance Rust implementation (dry-run capable)
```

---

## Features

- **~2,000ms buy speed** — `processed` commitment everywhere, no supermajority wait
- **WebSocket-driven detection** — PumpPortal new token events, no HTTP polling
- **Event-driven transaction confirmation** — `signatureSubscribe` WS instead of polling
- **Event-driven bonding curve detection** — `logsSubscribe` WS fires as soon as BC account is created
- **Zero float-dust bug** — exact u64 raw balance used for sells; `close_ix` always succeeds
- **Automatic slippage escalation** — doubles on error 6001 (buy) / 6003 (sell), emergency exit on attempt 3
- **Jito MEV protection** — optional tip-based prioritization through Jito block engine
- **Paper trading mode** — realistic simulation with GUI, no wallet needed
- **Live trading** — full on-chain execution via pump.fun bonding curve instructions
- **Risk scoring** — liquidity, dev holdings, metadata quality, honeypot heuristics
- Advanced exit strategies: trailing stop, take profit, early dump detection, dead token detection

---

## Performance

| Stage | Time |
|---|---|
| Token detection (PumpPortal WS) | ~100–300ms after creation |
| Bonding curve ready (logsSubscribe) | immediate at `processed` |
| Buy transaction confirmed | ~1,500–2,000ms |
| Sell transaction confirmed | ~2,000–2,500ms |
| **Total buy round-trip** | **~2,000ms** |

The dominant speed lever is **`processed` commitment** — switching from `confirmed` to `processed` cut buy time from 15–18s to ~2s by eliminating the 2/3 validator supermajority wait.

---

## Quick Start

See [QUICKSTART.md](QUICKSTART.md) to be running in 5 minutes.

---

## Configuration

### `wallet_config.json`

Copy `wallet_config.example.json` → `wallet_config.json` and fill in your values.

```json
{
  "private_key": [1, 2, 3, ...],          // 64-byte keypair (use convert_key.py)
  "rpc_url": "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY",
  "max_slippage_bps": 500,               // 5% slippage cap
  "priority_fee_lamports": 100000,       // 0.0001 SOL compute budget fee
  "compute_unit_limit": 200000,

  "use_jito_mev_protection": false,      // set true to route via Jito
  "jito_tip_lamports": 100000,           // 0.0001 SOL Jito tip per tx
  "jito_block_engine_url": "https://mainnet.block-engine.jito.wtf"
}
```

| Key | Default | Description |
|---|---|---|
| `private_key` | — | 64-byte keypair as JSON int array |
| `rpc_url` | — | Private RPC endpoint (Helius, QuickNode, etc.) |
| `max_slippage_bps` | 500 | Max slippage in basis points (500 = 5%) |
| `priority_fee_lamports` | 100000 | Compute budget priority fee |
| `compute_unit_limit` | 200000 | CU cap per transaction |
| `use_jito_mev_protection` | false | Route transactions via Jito block engine |
| `jito_tip_lamports` | 100000 | Jito tip per transaction (0.0001 SOL) |

> **Never commit `wallet_config.json`** — it contains your private key. It is in `.gitignore`.

---

## Jito MEV Protection

When `use_jito_mev_protection: true`, transactions are routed through the [Jito block engine](https://jito.wtf) with a tip instruction. Jito validators (~60–70% of Solana stake) prioritize tip-paying transactions for inclusion.

**This does not speed up detection** — it improves landing reliability under congestion.

Recommended tip amounts:

| Tip | SOL cost | Use case |
|---|---|---|
| 10,000 | 0.00001 | Testing / low congestion |
| 100,000 | 0.0001 | Normal sniping (recommended) |
| 1,000,000 | 0.001 | High competition |

No API key required — the Jito block engine is a free public endpoint.

---

## RPC Recommendations

Use a **private RPC** to avoid public mainnet rate limits (429 errors). The free public endpoint (`api.mainnet-beta.solana.com`) is not suitable for sniping.

| Provider | Free Tier | Notes |
|---|---|---|
| [Helius](https://helius.xyz) | Yes | Recommended — fast, generous free tier |
| [QuickNode](https://quicknode.com) | Limited | Good performance |
| [Alchemy](https://alchemy.com) | Yes | Alternative option |

---

## Safety Limits (live mode)

- Max 0.01 SOL per trade by default
- Max 20 trades per day
- Max 1 SOL daily loss before auto-stop
- Hard stop loss at -18%
- 0.02 SOL minimum reserve always kept for gas

---

## Sanity Test (CLI)

Before running the full bot, verify the buy/sell pipeline works end-to-end:

```bash
python sanity_test.py --sol 0.001 --slippage 0.20 --wait 5
```

This waits for the next new pump.fun token, buys it, waits, and immediately sells. Reports buy/sell times and net SOL cost. A successful run confirms the full pipeline is operational.

---

## Tech Stack

- Python 3.11+
- [solana-py](https://github.com/michaelhly/solana-py) + [solders](https://github.com/kevinheavey/solders)
- [websockets](https://websockets.readthedocs.io/) — async WS for detection + confirmation
- [PumpPortal](https://pumpportal.fun) — new token event stream
- Jito block engine (optional) — MEV-protected transaction submission

---

## Disclaimer

This software is for educational purposes. Trading newly launched tokens on pump.fun carries extreme risk — most tokens lose value immediately after launch. Never trade with funds you cannot afford to lose. The authors are not responsible for any financial losses.
