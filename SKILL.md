---
name: okx-onchain-swing-trader
description: "Event-driven ETH swing trading skill combining OKX OnchainOS whale signals, holder distribution, funding rate, open interest, and long/short ratio to identify high-conviction entries. Activates when you want to run a disciplined, low-frequency swing strategy using on-chain + derivatives data fusion. Only enters on multi-signal confluence with confidence ≥ 0.65. Exits are handled exclusively by OKX OCO TP/SL algo orders."
license: Apache-2.0
metadata:
  version: "1.2.0"
---

# OKX Onchain Swing Trader

A low-frequency, event-driven swing trading skill for ETH perpetual futures on OKX.
Powered by OKX OnchainOS DEX API signals fused with OKX Futures derivatives data, analyzed by Claude API.

---

## Strategy Philosophy

This skill is built around a single principle: **do nothing most of the time, act decisively when signals align.**

Most 30-minute cycles will produce a `hold` verdict. The bot does not chase momentum or fill time with noise trades. It waits for a confluence of on-chain whale behavior and derivatives market structure to shift simultaneously — a rare but high-conviction setup.

When the market is in equilibrium (neutral funding, stable OI, no whale flow), the bot stays flat. When multiple signals break out of their normal range in the same direction at the same time, it enters with a sized position and immediately sets a confidence-scaled TP/SL via OCO algo order.

Target cadence: **1–2 trades per day at most.**

---

## Data Sources

### OKX OnchainOS DEX API (`web3.okx.com/api/v6/dex`)

| Signal | Endpoint | What it captures |
|--------|----------|-----------------|
| Whale / smart-money flow | `POST /market/signal/list` | Large-wallet accumulation or distribution on Ethereum mainnet |
| On-chain index price | `POST /index/current-price` | Real-time reference price independent of the order book |
| Token holder distribution | `GET /market/token/cluster/overview` | Top-holder concentration, dispersion level |

> Note: Native BTC and ETH have no contract address. WBTC (`0x2260...`) and WETH (`0xc02a...`) on Ethereum mainnet are used as proxies for whale signal detection.

### OKX Futures API (`www.okx.com/api/v5`)

| Signal | Endpoint | What it captures |
|--------|----------|-----------------|
| Open Interest (OI) | `GET /public/open-interest` | Total unsettled contracts across all participants |
| Funding Rate | `GET /public/funding-rate` | Long/short cost imbalance — proxy for market bias |
| Long/Short Ratio | `GET /rubik/stat/contracts/long-short-account-ratio` | Proportion of accounts holding long vs. short |

---

## Entry Conditions

The bot requires **at least one primary trigger** before Claude is asked to score confidence and decide direction. A single signal in isolation is not sufficient; the final decision always incorporates the full multi-signal context.

### Long Entry Triggers (on-chain based — 2+ signals required)

| # | Trigger | Threshold |
|---|---------|-----------|
| 1 | Funding rate negative or near zero | `fundingRate ≤ 0` or close to zero — shorts over-positioned, rebound likely |
| 2 | Whale accumulation detected | Signal list returns non-empty buy-side events for WETH |
| 3 | Long/short ratio at short extreme | Ratio `≤ 0.6` — contrarian long setup |
| 4 | OI surge with low funding | OI change `≥ +2%` AND `fundingRate ≤ +0.005%` — new long positions entering without longs overpaying |

### Short Entry Triggers (2+ signals required)

| # | Trigger | Threshold |
|---|---------|-----------|
| 1 | Reversal short | L/S ratio `≥ 1.8` AND OI declining AND `premium_pct < -0.02%` |
| 2 | Funding spike short | `fundingRate ≥ +0.02%` AND `premium_pct < -0.02%` |
| 3 | Whale distribution detected | Signal list returns non-empty sell-side events for WETH |

> **CRITICAL:** L/S ratio 1.2–1.7 is normal in a bull market — do NOT treat it as a short signal alone.

**Hard block conditions (code-level, not overridable by Claude):**
- `confidence < 0.65` → forced `hold`
- Same asset position already open → no new entry (one position per asset at a time)
- `asset` not in `{BTC, ETH}` → skip (parse guard)
- `confidence is None` → skip (parse guard)

---

## Position Sizing & Leverage

Position sizing uses a **two-frame leverage model** that works universally regardless of account size.

### Frame 1 — Relative (preserves original philosophy)

```
base_leverage = clamp(round(INITIAL_BALANCE / current_wallet), min=1, max=5)
```

- `INITIAL_BALANCE`: wallet balance captured once at bot startup (fixed reference point)
- As the wallet shrinks (losses), leverage increases to help recover
- As the wallet grows (profits), leverage decreases to protect gains

### Frame 2 — Absolute (ensures minimum order is always reachable)

```
notional        = current_wallet × confidence × base_leverage
min_notional    = entry_price × CONTRACT_SIZE[inst_id]   # cost of 1 contract

if notional < min_notional:
    required_leverage = ceil(min_notional / (current_wallet × confidence))
    final_leverage    = min(5, required_leverage)
else:
    final_leverage = base_leverage
```

Frame 2 only activates when Frame 1 cannot fund even a single contract. It boosts leverage just enough to clear the minimum order threshold, capped at 5×.

### Contract Count

```
margin    = current_wallet × confidence
notional  = margin × final_leverage
contracts = floor(notional / (entry_price × CONTRACT_SIZE[inst_id]))
```

| Instrument | CONTRACT_SIZE | Min notional at $2,000 ETH |
|------------|--------------|---------------------------|
| BTC-USDT-SWAP | 0.01 BTC | ~$950 (at $95,000 BTC) |
| ETH-USDT-SWAP | 0.1 ETH | ~$200 |

---

## Dynamic TP/SL

Immediately after order fill, an OCO algo order is placed via `/api/v5/trade/order-algo`.
Both TP and SL execute at market price (`tpOrdPx: -1`, `slOrdPx: -1`).
`reduceOnly: true` prevents the OCO from opening a reverse position if the original is already closed.

### Formulas

| Direction | Take Profit | Stop Loss |
|-----------|-------------|-----------|
| Long | `entry × (1 + confidence × 0.06)` | `entry × (1 − ((1 − confidence) × 0.05 + 0.01))` |
| Short | `entry × (1 − confidence × 0.06)` | `entry × (1 + (1 − confidence) × 0.05 + 0.01)` |

### Interpretation

- **Higher confidence** → wider TP (capture more upside), tighter SL (cut losses fast)
- **Lower confidence** → narrower TP (take profit early), wider SL (tolerate noise)

### Example — ETH Long at $2,000

| confidence | TP | SL | TP % | SL % |
|-----------|----|----|------|------|
| 0.9 | $2,108 | $1,985 | +5.4% | −0.75% |
| 0.8 | $2,096 | $1,970 | +4.8% | −1.50% |
| 0.7 | $2,084 | $1,955 | +4.2% | −2.25% |

### Position Lifecycle

Positions are closed **exclusively by OKX OCO algo orders** (TP or SL). Claude does not issue `close` decisions. Once either leg of the OCO fires, the position is fully exited at market price. A new entry can be taken on the very next cycle if signals qualify.

---

## Minimum Balance Requirements

These figures are approximate and depend on the current market price of the underlying asset.

### ETH-USDT-SWAP (1 contract = 0.1 ETH)

| Scenario | Minimum wallet | Leverage | confidence assumption |
|----------|---------------|----------|-----------------------|
| Bare minimum (1 contract just reachable) | ~50 USDT | 5× | 0.8 |
| Recommended (comfortable 1× operation) | ~300 USDT | 1× | 0.8 |

> At ETH = $2,000: 1 contract min notional = $200. With 5× and confidence 0.8: `50 × 0.8 × 5 = 200`.

### BTC-USDT-SWAP (1 contract = 0.01 BTC)

| Scenario | Minimum wallet | Leverage | confidence assumption |
|----------|---------------|----------|-----------------------|
| Bare minimum (1 contract just reachable) | ~240 USDT | 5× | 0.8 |
| Recommended (comfortable 1× operation) | ~1,200 USDT | 1× | 0.8 |

> At BTC = $95,000: 1 contract min notional = $950. With 5× and confidence 0.8: `237.5 × 0.8 × 5 = 950`.

**If the wallet is below the bare minimum for a given asset at 5× leverage, the bot logs:**
```
Minimum order not reachable — <INST_ID> minimum balance required for 1 contract: XXX USDT (at 5x leverage)
```
and skips the trade without placing an order.

---

## Execution Flow

```
Every 30 minutes
│
├── 1. Collect on-chain data (OKX OnchainOS DEX API)
│       ├── BTC/ETH index price
│       ├── WBTC/WETH whale signals
│       └── WBTC/WETH holder distribution
│
├── 2. Collect derivatives data (OKX Futures API)
│       ├── Open Interest (BTC, ETH)
│       ├── Funding Rate (BTC, ETH)
│       └── Long/Short Ratio (BTC, ETH)
│
├── 3. Detect TP/SL fills (PREV_OPEN_INST_IDS diff)
│       └── if a position disappeared since last cycle:
│               → fetch closeAvgPx via positions-history API
│               → send Telegram notification with actual close price
│
├── 4. Analyze with Claude API (claude-opus-4-6)
│       └── Returns: { action, asset, confidence, reason_open }
│           action is one of: long | short | hold  (close is not available)
│
├── 5. Execute decision
│       ├── long / short  → resolve_leverage() → calc_contracts()
│       │                 → place market order → place OCO TP/SL algo order
│       │                 → translate reason_open to Korean (claude-haiku)
│       │                 → send Telegram notification
│       └── hold          → no action
│
│   [OKX — async, outside the 30-min loop]
│       └── OCO algo order fires when TP or SL is hit → position auto-closed
│           detected on next cycle via PREV_OPEN_INST_IDS diff
│
└── 5. Wait for next cycle
```

**One-way mode** is used throughout (`posSide` omitted). Long entry uses `side: buy`; short entry uses `side: sell`. Position direction is inferred from the sign of the `pos` field returned by OKX (`pos > 0` = long, `pos < 0` = short).

---

## Safety Notes

1. **One position per asset at a time.** The bot checks open positions before every entry decision. If a position for the target asset already exists, the entry is blocked at the code level regardless of Claude's verdict.

2. **Confidence gate is enforced twice.** Claude is instructed to output `hold` when `confidence < 0.65`, and the execution layer independently rejects any `long` or `short` decision where confidence falls below this threshold.

3. **TP/SL are reduce-only.** The `reduceOnly: true` flag on OCO algo orders ensures they can only shrink or close an existing position — never open a new one in the opposite direction accidentally.

4. **Exits are fully delegated to OKX.** Claude never issues a `close` action. All position exits happen via the OCO algo order placed at entry time. This eliminates whipsaw behaviour where a marginal signal reversal would cause Claude to close a position one cycle after opening it.

5. **Leverage is bounded at 5×.** Neither the relative frame nor the absolute frame can push leverage above 5×. If 5× is still insufficient to place a minimum order, the trade is skipped entirely with a descriptive log message.

6. **API keys are separated.** OKX OnchainOS DEX API (`web3.okx.com`) requires a separate key from the OKX Futures API (`www.okx.com`). Using the wrong key causes `50105` authentication errors. The bot uses `OKX_WEB3_*` and `OKX_*` environment variable pairs independently.

7. **Single-call timestamp.** All HMAC signatures use a single `datetime.now()` call captured once per request to avoid race conditions where seconds and milliseconds are drawn from different clock reads.

8. **Actual close price in notifications.** When a TP/SL fill is detected, the bot calls `GET /api/v5/account/positions-history` and reads `closeAvgPx` to show the real execution price in the Telegram message. Falls back to "close price unavailable" if the API call fails.

9. **Korean Telegram notifications.** Entry reason text (`reason_open`) is generated by Claude in English and translated to Korean via a separate `claude-haiku` call immediately before the Telegram message is sent. The original English text is preserved in logs.
