# Agent Builders Cup — product copy (Bitget)

Do not invent PnL or volume on forms. Confirm dates on Bitget’s current Cup page.

---

## Strategy type

Agent — AI / autonomous trading agent

---

## Summary

**Quad Threat** is a Bitget USDT-M stack on one account, started from **Condor**: Engine 1 Hummingbot ensemble scalp, Engine 2 Condor LLM breakout (20× basket), Engine 4 Hummingbot `pmm_simple` maker on BTC+ETH, plus a Cup dashboard. There is **no Engine 3 controller** — E3 is optional manual trading. Slot caps and pair ownership are enforced in conf/code.

---

## How the pieces play together

| Piece | Role | How it starts |
|---|---|---|
| Orchestrator `quad_threat_orchestrator` | Condor loop that **deploys** E1 + E4 via `manage_bots` | Condor strategy |
| Engine 1 `v37_scalp_multi` | Directional P&L — 15m ensemble, 3 slots | Hummingbot bot `quad-e1` |
| Engine 2 `v37_risk_manager` | 300s LLM breakout + risk overlay | Condor strategy |
| Engine 4 `pmm_quad` | Two-sided quotes BTC+ETH | Hummingbot bot `quad-e4` |
| Dashboard `:8770` | Observe stack, journal | Host UI |

**Ownership:** E2 must not open BTC/ETH. E1 skips E2 basket + BTC/ETH. Personal stock perps (e.g. AVGO) are Engine Free, not E1.

---

## Markets

- Venue: Bitget USDT-M (`bitget_perpetual`), ONEWAY
- E4: BTC-USDT, ETH-USDT only
- E2 opens: XAU, CL, DOGE, NEAR, LTC (20×)
- E1: `conf/universe.yml` minus E2 basket and BTC/ETH

---

## Parameters ($800 book)

| Piece | Value |
|---|---|
| E1 margin | **$20** / position · max 3 · total $60 |
| E2 margin | **$20** / position · max 3 · total $60 · 20× |
| E2 bank | LIMIT TP 0.4% · SL 0.5% |
| E2 tick | 300s · 3% daily DD halt |
| E4 quote / pair | **500** @ 100× · 8 bps · refresh **60s** · time_limit **180s** |
| E4 DD | global 80 / controller 40 |
| LLM | `openrouter:deepseek/deepseek-v4.1-flash` (configure locally) |

Do **not** paste these sizes onto a ~$60 live account.

---

## Status

Architecture is live-proven on Bitget USDT-M. This public repo is a **scrubbed pack** for sandbox cloning — install upstream Hummingbot + Condor, copy files, paper first. See root `README.md`.
