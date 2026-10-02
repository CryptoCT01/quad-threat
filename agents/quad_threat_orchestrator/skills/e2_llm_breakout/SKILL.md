---
name: e2_llm_breakout
description: Engine 2 hybrid — order-executor open, ATR-style hard stop on the venue (1.5× mean 15m range, 0.5%–1%), margin scaled to ≤ $2.50 risk per trade, agent closes/partials from live PnL.
when_to_use: Any tick where the orchestrator decides whether to open, manage or close an Engine 2 position.
created: 2026-09-29
source: quad_threat
---

# E2 hybrid (stock Condor + hard stop)

**POSITION MODE: ONEWAY.** Connector must match.

**CONTROLLER_ID:** top-level **and** inside `executor_config`.

## Basket
**XAU-USDT, CL-USDT, DOGE-USDT, NEAR-USDT, LTC-USDT** at **20×**. Never BTC/ETH, stocks, SOL, XRP, or a pair already open.

## Why hybrid
A 5-minute LLM tick **cannot** catch a wick. So every open **must** have a **hard stop on Bitget**. The agent still **closes early / partial** from live PnL. That is how TP/SL “change”: new close orders, not `modify_tpsl` (stock Condor has no such action).

## Stop size and margin (ATR-style)
1. Pull the pair's last ~24h of **15m candles** (≈ 96) with the stock candles tool.
2. `mean_range_15m` = mean of `(high − low) / close` over those candles (a fraction, e.g. 0.004 = 0.4%).
3. `stop_pct = clamp(1.5 × mean_range_15m, 0.5%, 1.0%)`. No candles → use 1.0%.
4. `margin = min($20, $2.50 / (stop_pct × 20))` → risk per trade ≤ **$2.50**. Examples: stop 0.5% → $20 (risk $2.00); 0.75% → $16.66; 1.0% → $12.50.
The `e2_order_sizer` routine does steps 3–4 for you when you pass `mean_range_15m`.

## Open (same tick, in order)

**NEVER pass `amount='$20'` or `amount=20`.** `$20` is a string (gate fail). `20` is **20 coins** — on DOGE that is ~$1.88 notional and the executor **TERMINATED volume 0**. Margin is not `amount`.

0. **MUST run `e2_order_sizer` first** for that one pair: `config={"pairs": "<PAIR>", "mean_range_15m": <fraction>}`. Use the table `amount` (base coins) and `stop_pct` verbatim.
1. **OPEN** `create_order_executor` MARKET, `position_action=OPEN`, `leverage=20`, `amount` = sizer number only. No TP/SL fields on this order. Confirm `executor_id` + fill. No id / FAILED = nothing opened.
2. **HARD STOP (required).** Same tick after fill: `create_order_executor` `position_action=CLOSE`, **STOP** (or stop-market) at `stop_pct` from fill:
   - LONG: `stop_price = fill × (1 − stop_pct)`
   - SHORT: `stop_price = fill × (1 + stop_pct)`
   Amount = **full** position. This sits on the venue. **Do not skip.** If the stop create fails, CLOSE the position market immediately — never hold a naked 20× leg.
3. Confirm the stop working. Journal fill, stop_pct, stop price, margin, both executor ids.

Do **not** use `modify_tpsl`. Do **not** skip the stop because “you will watch PnL”.

## Manage (every tick)

Run `e2_position_board` then, from breakeven vs mark:

| Trigger | Action |
|---|---|
| Thesis invalid | MARKET CLOSE full |
| Unrealised ≤ **−stop_pct** (the leg's journaled stop) | MARKET CLOSE full (if the hard stop has not already filled) |
| Unrealised ≥ **+0.4%** | MARKET CLOSE full **or** PARTIAL_CLOSE |
| Was ≥ +0.25% then back to ≤ +0.10% | MARKET CLOSE full (decay) |
| Else | HOLD — **hard stop stays** |

Partial close: CLOSE order with amount < size. **Replace the hard stop** for the remaining size at the same stop_pct from **original** breakeven (or tighter: breakeven if you are locking). Never leave remainder without a stop.

If you MARKET CLOSE full, cancel any leftover stop if the tool allows; else it will 404/expire — journal it.

## Sizing
Max **3** legs, **$20** margin cap per leg (scaled down by the stop rule above), 20×, one new open per tick, one retry. E2 share $120; E2 drawdown cap $15 — stop opening when used.

## Visualize
```
manage_routines(action="run", name="e2_position_board", config={})
manage_routines(action="run", name="market_analysis", config={"connector_name": "bitget_perpetual", "pairs": "ALL"})
```

## Overlay
Flatten E2 on invalid thesis or when the global **$80 daily kill switch** (AGENT.md) fires.

## Errors
One retry then STOP. Never report OPEN if the stop was not placed — flatten instead.
