# Agent Builders Cup — product copy (Bitget)

Short paste-ready description of the **live Quad Threat** desk. Do not invent PnL or volume on forms. Confirm dates on Bitget’s current Cup page (sandbox target: early October).

---

## Strategy type

Agent — AI / autonomous trading agent

---

## Summary

**Quad Threat** is a live Bitget USDT-M stack on one account: Hummingbot ensemble scalp (Engine 1), Condor LLM breakout + risk overlay (Engine 2), Hummingbot `pmm_simple` maker on BTC+ETH (Engine 4), and a custom Cup dashboard. There is **no Engine 3 controller** — E4 is the maker.

Margin and slot caps are enforced in conf/code, not by hoping the model behaves.

---

## How the pieces play together

| Piece | Role |
|---|---|
| Engine 1 `v37_scalp_multi` | Directional P&L — 15m ensemble (SUPER_A / ROC_RSI / BB_VOL), capped scalp slots |
| Engine 2 Condor `v37_risk_manager` | 300s loop — opens only a small high-leverage basket; can flatten book on invalid thesis / 3% DD |
| Engine 4 `pmm_simple` | Two-sided quotes BTC-USDT + ETH-USDT for maker volume / inventory |
| Dashboard `:8770` | Observe stack, slots, journal; start/stop controls |

**Ownership:** E2 must not open BTC/ETH while E4 quotes them. E1 scalp slots are separate from E4 inventory. E2 overlay does not “clip” E1 greens that already have SL/TP/trail.

---

## Markets

- Venue: Bitget USDT-M (`bitget_perpetual`), ONEWAY
- E4 maker: BTC-USDT, ETH-USDT (pack); live script may also reference XRP/DOGE
- E1 universe: crypto + commodities (XAU, CL) + stock perps in `conf/universe.yml`
- E2 opens (snapshot): XAU, CL, DOGE, NEAR, APT

---

## Parameters (snapshot)

| Piece | Value |
|---|---|
| Scalp margin | $10 / position |
| Scalp slots | max 3 |
| Score | 0.66 (≥2 of 3 engines) |
| Scalp SL / TP / trail | 0.8% / 1.6% / 1.2%·0.8% |
| Condor tick | 300s · max 3 E2 · 3% daily DD halt |
| E4 spreads (BTC/ETH YAML) | 5 bps (`0.0005`) |
| LLM | `openrouter:YOUR_MODEL_HERE` (configure locally) |

---

## Status

Architecture is live-proven on Bitget USDT-M. This public repo is a **scrubbed pack** for sandbox cloning — install upstream Hummingbot + Condor, copy files, paper first. See root `README.md`.
