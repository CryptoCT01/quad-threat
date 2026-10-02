---
name: e4_btc_eth_maker
description: Engine 4 - Hummingbot V2 controller pmm_quad: two-sided maker quotes on BTC-USDT and ETH-USDT perps with inventory-aware exits, plus a one-hour loss pause.
when_to_use: Any tick that deploys, checks, pauses, resumes or sizes the quad-e4 bot, or judges whether E4 is healthy.
created: 2026-09-29
source: quad_threat
---

# E4 BTC/ETH maker (pmm_quad)

## What it trades
Bot `quad-e4` with `conf_e4_quad_btc.yml` + `conf_e4_quad_eth.yml`. Controller `pmm_quad` (a renamed copy of our pmm_simple so it never overwrites the stock controller).

## Entries
LIMIT buy and sell each `buy_spreads` / `sell_spreads` (8 bps) from mid, refreshed every `executor_refresh_time`. `crash_halt_pct` (0.6%) pauses quoting on a fast move.

## Sizing and venue minimums
Finals: `total_amount_quote: 500` per pair at 100x, inside the E4 ~$400 share of the $800 book. Split across sides; each level must clear Bitget minimums (BTC 0.0001, ETH 0.01, $5). Below-minimum levels are refused loudly ("below the venue minimum"), never silently.

## TP / SL and fees (Bitget VIP 5: maker 0.01%, taker 0.032%)
LIMIT TP 6 bps, SL 15 bps, time limit 180s, refresh 60s. One-side quoting with inventory skew when inventory is held. SL and time exits execute as MARKET (PositionExecutor).
- Maker round trip ≈ 2 bps. Real TP fills averaged ≈ 5.6 bps gross (replay), so a TP nets ≈ **3.6 bps** (≈ $0.18 per $500).
- Stop-out ≈ 15 bps move + 1 bp maker entry + 3.2 bps taker exit ≈ 19 bps (≈ $0.96 per $500). Break-even win rate on TP/SL alone ≈ 84%.
- TIME exits are taker: small loss (replay ≈ −2.8 bps gross + 4.2 bps fees).
- **Refresh never closes a fill.** `executor_refresh_time` and the crash/one-side pulls cancel only executors with zero fill; any filled or partly filled executor exits only by LIMIT TP, SL or TIME (`pmm_quad.executors_to_refresh`). This removes the EARLY_STOP taker leak seen in the replay.

## Pause rule (one-hour loss)
Every tick the orchestrator journals E4 realised PnL. If E4's **realised loss over the last 60 minutes is $14 or more**:
1. `manage_bots` stop `quad-e4*` (resting quotes cancelled).
2. Journal `E4 PAUSED until <now+30min>` with the loss figure.
3. Do not redeploy while paused. After 30 minutes, deploy `quad-e4` once with the same two configs and journal "E4 resumed". The one-hour window restarts from the resume time.
The global daily kill switch ($80, AGENT.md) overrides this: if it fires, E4 stays stopped for the day.

## Rate limits
Refresh no faster than 60s per pair; two pairs only.

## Healthy vs broken
- Healthy: both sides quoted, real exchange order ids, fills both ways, inventory small.
- Broken: one-sided quoting for long periods, repeated cancels/rejects, inventory beyond one level, container restarts.

## When to stop
The deploy drawdown guard (E4 cap $40 global / $20 per controller) stops it. The orchestrator stops it only for the pause rule above, the daily kill switch, or when the human asks.
