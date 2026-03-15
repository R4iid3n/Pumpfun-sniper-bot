# ⚡ Pump.fun Sniper Bot

A high-performance Python sniping bot for [pump.fun](https://pump.fun) tokens on Solana. Detects new token launches via WebSocket, executes buy+sell transactions on the bonding curve, and supports both paper trading and live trading.

**Latest benchmark (Frankfurt VPS): 340ms buy · 638ms sell** 🏆

---

## 🏗️ Architecture

```
pumpfun_bot.py       — Main GUI bot (tkinter) — paper + live trading
live_trader.py       — Live trading engine (Solana transactions)
sanity_test.py       — CLI: one buy + immediate sell (pipeline health check)
pumpfunlib/          — Low-level pump.fun bonding curve library
```

---

## ✨ Features

- **⚡ Sub-second execution** — 340ms buy / 638ms sell on Frankfurt VPS
- **🔀 Multi-endpoint transaction blasting** — RPC + Jito MEV + NextBlock in parallel, whichever lands first wins
- **📡 QUIC/TPU direct send** — fire transactions directly to validator TPU ports via `aioquic`, bypassing the RPC relay hop
- **🔔 WebSocket-driven confirmation** — `signatureSubscribe` WS for real-time confirmation, polling fallback on timeout
- **🪙 Token-2022 support** — dynamic ATA detection, SPL + Token-2022 both handled
- **⚡ Prefetched bonding curve** — curve state cached at token creation, zero extra RPC reads at buy time
- **🔁 Smart retry logic** — auto-escalating slippage on error 6001/6003, retryable handling for errors 2006/6042
- **🛡️ Jito MEV protection** — optional tip-based prioritization through Jito block engine
- **🚀 NextBlock integration** — Frankfurt fast-lane direct relay to validators
- **📊 Paper trading mode** — realistic simulation with GUI, no wallet needed
- **🔒 Safety limits** — max SOL per trade, daily loss cap, minimum reserve, hard stop-loss

---

## 📈 Performance

| Stage | Time |
|---|---|
| Token detection (PumpPortal WS) | ~200–400ms after creation |
| Buy transaction confirmed | **~340ms** |
| Sell transaction confirmed | **~638ms** |

**Key optimisations:**
- `processed` commitment eliminates the 2/3 validator supermajority wait (~15s → sub-second)
- Prefetched bonding curve = no RPC read on buy path
- Parallel blast to multiple endpoints — first confirmation wins, network deduplicates
- Frankfurt VPS co-located with QuikNode + NextBlock Frankfurt endpoints

> **Note:** `logsSubscribe` @ processed was tested for faster token detection but floods shared RPC connections with hundreds of pump.fun events/sec, throttling `signatureSubscribe` and adding 8–10s confirmation penalty. Pumpportal WS is used instead. `find_token_logs_ws()` is available for dedicated RPC setups.

---

## 🚀 Quick Start

See [QUICKSTART.md](QUICKSTART.md) to be running in 5 minutes.

---

## ⚙️ Configuration

Copy `wallet_config.example.json` → `wallet_config.json` and fill in your values.

```json
{
  "private_key": [1, 2, 3, ...],
  "rpc_url": "https://your-node.quiknode.pro/YOUR_KEY/",
  "max_slippage_bps": 500,
  "priority_fee_lamports": 200000,
  "buy_compute_unit_limit": 200000,
  "sell_compute_unit_limit": 70000,

  "use_jito_mev_protection": false,
  "jito_tip_lamports": 100000,
  "jito_block_engine_url": "https://mainnet.block-engine.jito.wtf",

  "nextblock_api_key": "",
  "nextblock_region": "frankfurt",
  "nextblock_tip_lamports": 1000000,
  "nextblock_only": false,

  "use_tpu_direct": true
}
```

| Key | Default | Description |
|---|---|---|
| `private_key` | — | 64-byte keypair as JSON int array |
| `rpc_url` | — | Private RPC endpoint (QuikNode, Helius, etc.) |
| `max_slippage_bps` | 500 | Max slippage in basis points (500 = 5%) |
| `priority_fee_lamports` | 200000 | Compute budget priority fee |
| `buy_compute_unit_limit` | 200000 | CU cap for buy (Token-2022 needs 200k) |
| `use_jito_mev_protection` | false | Route via Jito block engine |
| `nextblock_api_key` | — | NextBlock API key for fast-lane relay |
| `nextblock_region` | frankfurt | NextBlock region (`frankfurt`, `ny`, `tokyo`) |
| `nextblock_tip_lamports` | 1000000 | NextBlock tip — minimum ~1M lamports |
| `use_tpu_direct` | true | Fire-and-forget QUIC to validator TPU |

> **Never commit `wallet_config.json`** — it contains your private key. It is in `.gitignore`.

---

## 🛡️ Transaction Delivery

The bot blasts every transaction to multiple endpoints simultaneously:

| Endpoint | Type | Notes |
|---|---|---|
| RPC (`sendTransaction`) | Standard | Always active |
| Jito block engine | MEV-protected | Optional, free |
| NextBlock Frankfurt | Fast-lane relay | Requires API key, min 1M lamport tip |
| TPU direct (QUIC) | Validator direct | No fee, bypasses RPC relay |

All endpoints run in parallel — the fastest confirmation wins. The network deduplicates the same transaction signature automatically.

---

## 🌐 RPC Recommendations

Use a **private RPC** to avoid public mainnet rate limits. The free public endpoint is not suitable for sniping.

| Provider | Notes |
|---|---|
| [QuikNode](https://quicknode.com) | Recommended — Frankfurt endpoint co-located with NextBlock |
| [Helius](https://helius.xyz) | Good alternative, generous free tier |
| [Triton](https://triton.one) | High-performance, dedicated nodes available |

---

## 🧪 Sanity Test (CLI)

Before running the full bot, verify the buy/sell pipeline end-to-end:

```bash
python sanity_test.py --sol 0.001 --slippage 0.20 --wait 5
```

Waits for the next new pump.fun token, buys it, waits, and immediately sells. Reports buy/sell confirmation times and net SOL cost.

---

## 🔧 Tech Stack

- Python 3.11+
- [solana-py](https://github.com/michaelhly/solana-py) + [solders](https://github.com/kevinheavey/solders)
- [websockets](https://websockets.readthedocs.io/) — async WS for detection + confirmation
- [aioquic](https://github.com/aiortc/aioquic) — QUIC/TPU direct send
- [PumpPortal](https://pumpportal.fun) — new token event stream
- Jito block engine (optional) — MEV-protected submission
- NextBlock (optional) — Frankfurt fast-lane relay

---

## ⚠️ Disclaimer

This software is for educational purposes. Trading newly launched tokens on pump.fun carries extreme risk — most tokens lose value immediately after launch. Never trade with funds you cannot afford to lose. The authors are not responsible for any financial losses.
