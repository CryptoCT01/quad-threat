---
name: e4_btc_eth_maker
description: Engine 4 - Hummingbot V2 controller pmm_quad: two-sided maker quotes on BTC-USDT and ETH-USDT perps with inventory-aware exits.
when_to_use: Any tick that deploys, checks or sizes the quad-e4 bot, or judges whether E4 is healthy.
created: 2026-09-29
source: quad_threat
---

# E4 BTC/ETH maker (pmm_quad)

## What it trades
Bot `quad-e4` with `conf_e4_quad_btc.yml` + `conf_e4_quad_eth.yml`. Controller `pmm_quad` (a renamed copy of our pmm_simple so it never overwrites the stock controller).

## Entries
LIMIT buy and sell each `buy_spreads` / `sell_spreads` (8 bps) from mid, refreshed every `executor_refresh_time`. `crash_halt_pct` pauses quoting on a fast move.

## Sizing and venue minimums
`total_amount_quote` split across sides; each level must clear Bitget minimums (BTC 0.0001, ETH 0.01, $5). Below-minimum levels are refused loudly ("below the venue minimum"), never silently.

## TP / SL
LIMIT TP 6 bps, SL 15 bps, time limit. SL and time exits execute as MARKET (PositionExecutor).

## Rate limits
Refresh no faster than 30s per pair; two pairs only.

## Healthy vs broken
- Healthy: both sides quoted, real exchange order ids, fills both ways, inventory small.
- Broken: one-sided quoting for long periods, repeated cancels/rejects, inventory beyond one level, container restarts.

## When to stop
Deploy drawdown guard stops it; the orchestrator never stops it unless the human asks or the E4 drawdown share is exhausted.
