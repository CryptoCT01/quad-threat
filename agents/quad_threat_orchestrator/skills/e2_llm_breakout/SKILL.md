---
name: e2_llm_breakout
description: Engine 2 — LLM breakout via stock Condor order executors. Open fills into a Position; close with a reduce order using live PnL. No exchange TP/SL amend.
when_to_use: Any tick where the orchestrator decides whether to open, manage or close an Engine 2 position.
created: 2026-09-29
source: quad_threat
---

# E2 LLM breakout — order-executor path (stock Condor)

**POSITION MODE: ONEWAY** on Bitget. Connector must match or every open fails with "Failed to submit order".

**CONTROLLER_ID:** pass `controller_id` **both** as the top-level arg **and** inside `executor_config`. The stock risk gate cancels creates that omit it.

## What it trades
Stock Condor **order executors** only. 20×. Basket: **XAU-USDT, CL-USDT, DOGE-USDT, NEAR-USDT, LTC-USDT**.
Never BTC/ETH (E4). Never stocks / SOL / XRP. Never a pair that already has an exchange position.

Do **not** use `create_position_executor` with exchange TP/SL. Do **not** call `modify_tpsl` — it does not exist on stock Condor.

## How an Engine-2 trade works (admin path)

1. **Open:** `create_order_executor` MARKET, `position_action=OPEN`, `leverage=20`, `amount` from `e2_order_sizer` (verbatim). No `take_profit`, no `stop_loss`, no `entry_price`.
2. **When filled** you have a **Position**: breakeven price, amount, **real-time PnL**. Read it every tick via `get_portfolio_overview` (perp positions) and `list_executors`.
3. **Close** is just another order: `create_order_executor` MARKET, `position_action=CLOSE` (or reduce). Amount = full size **or partial**. That *is* TP and SL.
4. Confirm fill (`executor_id` + filled amount). No id / error / FAILED at start = **nothing opened**. Never `place_order`.

## Sizing
- **$20 margin** per new leg on an ~$800 book. Max **3** concurrent E2 positions.
- Notional = margin × 20. Base amount from `manage_routines(action="run", name="e2_order_sizer", config={"pairs": "<PAIR>"})`.
- One create per tick, at most one retry.

## When to close (you decide from live PnL)

Every tick, for each E2 position, compute `unrealised_pct` from breakeven vs mark (sign-correct for long/short):

| Trigger | Action |
|---|---|
| Thesis invalid (broke level, opposing 1h close) | CLOSE full, market |
| Unrealised ≤ **−0.5%** | CLOSE full (stop) |
| Unrealised ≥ **+0.4%** | CLOSE full (take profit) **or** PARTIAL_CLOSE (leave a runner) |
| Was ≥ +0.25% then gave it back (now ≤ +0.10%) | CLOSE full (momentum decay) |
| None of the above | HOLD |

Partial close: `CLOSE` order with amount < position size. Journal remaining size.

**15-minute same-coin cooldown** after a stop. Do not re-OPEN that name next tick.

## Visualize
Every tick (or on demand in Condor UI):
```
manage_routines(action="run", name="e2_position_board", config={})
```
HTML/table of pair, side, amount, breakeven, mark, PnL, PnL%. Use it before OPEN/CLOSE.

Also run:
```
manage_routines(action="run", name="market_analysis", config={"connector_name": "bitget_perpetual", "pairs": "ALL"})
```
for impulse — not to hunt MACD.

## Entry (new opens only)
Open when **≥2** of: impulse through recent high/low, clear 1h direction, expanding range / impulsive candle.
If **0/3** slots and one basket name has 2-factor impulse → **OPEN**. Empty book is not a reason to HOLD.

## Overlay (E1 / E4 / YOU)
CLOSE those **only** on invalid thesis or **3% daily DD**. Never clip E1 at +$0.80. Never flatten E4 because it is slightly green.

## Errors
One retry in the same tick, then STOP. Journal `e2 action failed: <msg>`. Telegram chat `0`. Do not pretend a failed create filled.

## Journal (one object per tick)
action OPEN_LONG | OPEN_SHORT | CLOSE | PARTIAL_CLOSE | HOLD · symbol · size · breakeven · pnl · notes (rule walk).
