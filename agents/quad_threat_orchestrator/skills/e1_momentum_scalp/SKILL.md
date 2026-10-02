---
name: e1_momentum_scalp
description: Engine 1 - Hummingbot V2 controller v37_scalp_multi: multi-pair momentum scalp (SUPER_A / ROC_RSI / BB_VOL ensemble) on Bitget USDT-M perps.
when_to_use: Any tick that deploys, checks or sizes the quad-e1 bot, or judges whether E1 is healthy.
created: 2026-09-29
source: quad_threat
---

# E1 momentum scalp (v37_scalp_multi)

## What it trades
Bot `quad-e1` with controller config `conf_v37_scalp_multi.yml`. Universe from `conf/universe.yml` (crypto, commodities, stock perps),
excluding the E2 basket and BTC/ETH (E4). Strategy toggles in `conf/active_strategy.json`.

## Entries
Long/short when the ensemble score >= `score_threshold` (0.66) with at least two strategies agreeing. `cooldown_time` 1500s per pair.

## Sizing and venue minimums
- `position_size_quote` is MARGIN per position; notional = margin x leverage (`leverage_map`, else `leverage_default`).
- `max_open_positions` slots; `total_amount_quote` = slots x margin.
- The controller quantizes DOWN to Bitget `sizeMultiplier`, bumps to `minTradeNum` / `minTradeUSDT` ($5) only within 5% of budget, and refuses (logs "below the venue minimum") otherwise.

## TP / SL
Triple barrier: SL 0.8%, TP 1.6%, trailing 0.5% / 0.3%, time limit 3h.

## Rate limits
Expect bursts of 429 on `GET /api/v2/public/time` (network check). Harmless if orders still fill. A 429 on order endpoints or >50 per minute is broken.

## Healthy vs broken
- Healthy: bot `running`, real exchange order ids in logs, <= max_open_positions legs, no venue-minimum rejects.
- Broken: container restarting, repeated "Failed to submit order", positions > slots, leverage mismatch (margin > 1.05 x budget).

## Who manages open E1 legs
The E1 controller's triple barrier manages each leg. On top of that, **E2 is the risk manager for E1** (skill `e2_llm_breakout`,
section "Risk manager"): it protects naked/orphaned legs, closes invalidated legs, locks profit at +1% / +1.5%, and never opens E1 legs.

## When to stop
- E1 day loss (realised + unrealised, UTC day) reaches **$30**: E2's risk manager stops `quad-e1*` and closes all E1 legs; no redeploy until the next UTC day.
- The $80 daily kill switch (AGENT.md), the deploy drawdown guard (`max_global_drawdown_quote: 30`), or the human.
