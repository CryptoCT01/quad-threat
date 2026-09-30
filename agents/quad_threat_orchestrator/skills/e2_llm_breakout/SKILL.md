---
name: e2_llm_breakout
description: Engine 2 hybrid — order-executor open, hard −0.5% stop on the venue, agent closes/partials from live PnL.
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

## Open (same tick, in order)

1. **OPEN** `create_order_executor` MARKET, `position_action=OPEN`, `leverage=20`, `amount` from `e2_order_sizer` (verbatim). No TP/SL fields on this order. Confirm `executor_id` + fill. No id / FAILED = nothing opened.
2. **HARD STOP (required).** Same tick after fill: `create_order_executor` `position_action=CLOSE`, **STOP** (or stop-market) at **−0.5%** from fill:
   - LONG: `stop_price = fill * 0.995`
   - SHORT: `stop_price = fill * 1.005`
   Amount = **full** position. This sits on the venue. **Do not skip.** If the stop create fails, CLOSE the position market immediately — never hold a naked 20× leg.
3. Confirm the stop working. Journal fill, stop price, both executor ids.

Do **not** use `modify_tpsl`. Do **not** skip the stop because “you will watch PnL”.

## Manage (every tick)

Run `e2_position_board` then, from breakeven vs mark:

| Trigger | Action |
|---|---|
| Thesis invalid | MARKET CLOSE full |
| Unrealised ≤ **−0.5%** | MARKET CLOSE full (if the hard stop has not already filled) |
| Unrealised ≥ **+0.4%** | MARKET CLOSE full **or** PARTIAL_CLOSE |
| Was ≥ +0.25% then back to ≤ +0.10% | MARKET CLOSE full (decay) |
| Else | HOLD — **hard stop stays** |

Partial close: CLOSE order with amount < size. **Replace the hard stop** for the remaining size at the same −0.5% from **original** breakeven (or tighter: breakeven if you are locking). Never leave remainder without a stop.

If you MARKET CLOSE full, cancel any leftover stop if the tool allows; else it will 404/expire — journal it.

## Sizing
**$20** margin, max **3** legs, one new open per tick, one retry.

## Visualize
```
manage_routines(action="run", name="e2_position_board", config={})
manage_routines(action="run", name="market_analysis", config={"connector_name": "bitget_perpetual", "pairs": "ALL"})
```

## Overlay
Flatten E1/E4/YOU only on invalid thesis or **3% daily DD**.

## Errors
One retry then STOP. Never report OPEN if the stop was not placed — flatten instead.
