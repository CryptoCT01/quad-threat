# Quad Threat

**Bitget USDT-M multi-engine desk** — one account, three trading engines + Cup dashboard.

Public snapshot for Michael / Bitget early-October sandbox. Clone this pack, install upstream Hummingbot + Condor, copy controllers/agent/conf in, paper first.

| Piece | What it is |
|---|---|
| **Engine 1** | Hummingbot controller `v37_scalp_multi` (momentum / ensemble scalp) |
| **Engine 2** | Condor agent `v37_risk_manager` / `disciplined_perps_strategy` (LLM breakout + risk overlay) |
| **Engine 4 (Maker)** | Hummingbot `pmm_simple` BTC+ETH two-sided quotes |
| **Dashboard** | Custom Cup UI (`hbot_dash.py` + `hbot-dashboard.html` + `hbot_server.py`) on `:8770` |

### No Engine 3 controller

The live desk has **three** trading engines. In code and on the dashboard the maker is **Engine 4 (E4)**. People sometimes say “engine 3” casually meaning “the third runnable after E1+E2.” **There is no separate Engine 3 controller file.** Title the four Quad Threat pieces as **Engine 1 / Engine 2 / Engine 4 (Maker) / Dashboard**.

```
Engine 1 ──► directional P&L (scalp slots)
Engine 2 ──► LLM opens on basket + can flatten book
Engine 4 ──► maker volume on BTC/ETH (own inventory)
Dashboard ─► watches all three, start/stop, journal
```

See `quad-threat-flowchart.png` if present.

---

## What Quad Threat is

- **Venue:** Bitget USDT-M perpetuals (`bitget_perpetual`), one-way mode
- **Account model:** one live account shared by E1 + E2 + E4
- **Built on:** upstream [Hummingbot](https://github.com/hummingbot/hummingbot) V2 controllers + [Condor](https://github.com/hummingbot/condor)
- **This repo is not a full fork** — it is the custom controllers, Condor agent pack, conf snapshots, helper scripts, and Cup dashboard

---

## How the four pieces play together

### Pair ownership

| Who | Opens | Closes / manages | Must not open |
|---|---|---|---|
| **E1** | Ensemble scalp across `conf/universe.yml` | Own SL/TP/trail | — |
| **E2** | Breakout basket only (see live snapshot) | Own E2 legs + overlay flatten of E1/E4 on invalid thesis or 3% DD | BTC/ETH (E4), stocks, SOL, XRP |
| **E4** | Two-sided quotes BTC-USDT + ETH-USDT | Inventory TP/SL / rebalance | Extra pairs unless you add confs |

### Slots

- **E1 scalp slots:** `max_open_positions` from universe / conf (live snapshot: **3**). Maker inventory does **not** consume E1 slots.
- **E2 slots:** up to **3** Engine-2 executors (`max_open_executors: 3`).
- **E4:** separate bot/container (`hummingbot-e4`) with its own script conf; does not take E1 slots.

### Who opens / closes

1. **E1** opens when ≥2 of 3 signal engines agree above `score_threshold` and a scalp slot is free.
2. **E2** ticks every `frequency_sec` (live: 300s). Opens only via Condor `manage_executors` on the allowed basket. May flatten E1/E4 when thesis dies or daily DD hits 3% — not for “take $0.80 profit” on E1 (E1 already has barriers).
3. **E4** continuously quotes; fills become inventory worked with limit TP + stop.
4. **Dashboard** is observe + orchestration UI. It does not replace the engines.

---

## Engine 1 — `v37_scalp_multi`

- Path: `controllers/generic/v37_scalp_multi.py`
- Conf: `conf/controllers/conf_v37_scalp_multi.yml` + `conf/universe.yml` + `conf/active_strategy.json`
- Connector: `bitget_perpetual`
- Signals: SUPER_A, ROC_RSI, BB_VOL (toggles in `active_strategy.json`)
- Geometry (live snapshot): SL **0.8%**, TP **1.6%**, trail **1.2% / 0.8%**, cooldown **1500s**, time limit **3h**, score **0.66**
- Margin: `$10` per position (`position_size_quote`); leverage from universe map (BTC/ETH 15x, SOL/XRP 10x, else 5x)

Install into a Hummingbot tree under `controllers/generic/` and matching `conf/`, then import/start with your usual V2 + controllers flow (`scripts/v2_with_controllers.py` included as the wrapper used live).

---

## Engine 2 — Condor `v37_risk_manager`

- Pack: `condor-agent/v37_risk_manager/`
  - `AGENT.md`
  - `strategies/disciplined_perps_strategy/{strategy.md,config.yml,learnings.md}`
  - `routines/market_analysis.py` + `routines/__init__.py`
- Optional MCP tool: `condor-mcp/executors.py` (copy into Condor’s hummingbot_api tools if you use the custom executor helpers)
- LLM: set `agent_key: openrouter:YOUR_MODEL_HERE (placeholder — no live model id committed)
- Needs OpenRouter key in Condor `.env` (see `.env.example`)

Copy the agent folder into your Condor `agents/` tree. Start Condor per upstream docs; start the strategy in **paper** first.

---

## Engine 4 (Maker) — `pmm_simple`

- Controller: `controllers/market_making/pmm_simple.py`
- Live confs in pack:
  - `conf/e4/controllers/conf_e4_pmm_btc.yml`
  - `conf/e4/controllers/conf_e4_pmm_eth.yml`
  - `conf/e4/scripts/conf_e4_pmm.yml`
- Live BTC/ETH snapshot from YAML: spreads **`0.0008` (5 bps)** each side, refresh **60s**, `total_amount_quote: 200`, leverage **100**, SL **0.3%**, TP **0.06%**, ONEWAY
- Script conf also lists XRP/DOGE controller files on the live desk; those YAML files are **not** in this public pack (BTC+ETH only). Add your own if you need them.

---

## Dashboard (Cup)

- `dashboard/hbot_dash.py` — launcher
- `dashboard/hbot_server.py` — API + static server (live desk uses **:8770**)
- `dashboard/hbot-dashboard.html` — UI (Engine stack, slots, Condor, E4 controls, journal)

Point `HBOT_*` paths at your local Hummingbot/Condor installs. No keys in the HTML.

---

## Current live snapshot (synced from desk)

- **E2 OPEN basket:** XAU-USDT, CL-USDT, DOGE-USDT, NEAR-USDT, LTC-USDT (20×)
- **E2 model:** set `agent_key` in strategy.md (placeholder `openrouter:YOUR_MODEL_HERE` — live desk uses DeepSeek V4.1 Flash via OpenRouter)
- **E4:** BTC-USDT + ETH-USDT only · spreads **8 bps** (`0.0008`) · TP **6 bps LIMIT** · SL **15 bps** · `time_limit` **180s** · leverage **100** · `total_amount_quote` 200
- **E1:** max 3 slots · $10 margin · score_threshold 0.66 · closed 15m · does not open E2 basket or BTC/ETH


## Repo layout

```
quad-threat/
├── README.md
├── CUP-APPLICATION.md
├── LICENSE
├── .env.example
├── .gitignore
├── quad-threat-flowchart.png          # if present
├── controllers/
│   ├── generic/v37_scalp_multi.py     # Engine 1
│   ├── market_making/pmm_simple.py    # Engine 4
│   └── directional_trading/           # optional; empty init (no Engine 3)
├── conf/
│   ├── universe.yml
│   ├── active_strategy.json
│   ├── controllers/conf_v37_scalp_multi.yml
│   └── e4/...
├── condor-agent/v37_risk_manager/     # Engine 2
├── condor-mcp/executors.py            # optional Condor MCP helper
├── scripts/                           # helpers only (acct, cancel, place, V2 wrapper)
└── dashboard/                         # Cup UI + server
```

---

## Security

- **No** `.env`, connector confs, keystores, sqlite, sessions, journals, or Telegram chat IDs
- `HBOT_PASSWORD` defaults in helper scripts are `CHANGE_ME`
- `CONDOR_CHAT_ID=0` in `.env.example`
- `agent_key` is always `openrouter:YOUR_MODEL_HERE`
- Upstream Hummingbot/Condor licenses still apply to their code (see `LICENSE`)

If you find a secret in a clone, rotate it and open an issue — do not paste keys into GitHub.

---

## Quick start (paper first)

1. Install upstream **Hummingbot** and **Condor** (Docker or native — follow their docs).
2. Copy this pack’s controllers into Hummingbot `controllers/…` and confs into `conf/…` (mirror the layout above). For E4, use a separate instance/data dir as you would for `hummingbot-e4`.
3. Copy `condor-agent/v37_risk_manager` into Condor `agents/`. Optionally install `condor-mcp/executors.py` into the hummingbot_api MCP tools path.
4. Copy `.env.example` → `.env` for Condor / dashboard. Fill **placeholders only you control**.
5. Configure Bitget **test/paper** credentials in Hummingbot’s connector flow (never commit them).
6. Start dashboard: `python dashboard/hbot_dash.py` (or your usual `:8770` entrypoint) and confirm UI loads with engines **stopped**.
7. Paper-start Engine 1, then Engine 4, then Engine 2. Confirm pair ownership before any live keys.

**Do not** paste production API keys into this repo. **Do not** assume live sizing is safe for a fresh account — start tiny.

---

## Sandbox notes (early October)

- Goal: Michael / Bitget can clone and reproduce the **architecture** in a sandbox before any competition window.
- Prefer paper / reduced size; verify E2 cannot open BTC/ETH while E4 quotes.
- Expect to retune spreads, `total_amount_quote`, and basket as markets move — YAML in this pack is a snapshot, not a promise.
- Registration / Cup logistics: see `CUP-APPLICATION.md` (product copy; dates may have moved — check Bitget’s current page).

---

## Missing from live (called out)

These were requested if present on the desk; not included here:

- `scripts/_bitget_positions.py` — not recovered into this pack
- `controllers/directional_trading/v37_scalp.py` — optional legacy; live P&L path is `v37_scalp_multi` (generic). No Engine 3.
- E4 XRP/DOGE controller YAML — referenced by live `conf_e4_pmm.yml` but not shipped in this public BTC+ETH pack

---

## Provenance

Assembled as a public, secret-scrubbed snapshot of Crypto T’s live Quad Threat desk for Bitget sandbox use. Controllers/conf/agent text match the live humming-bot layout (`v37_scalp_multi`, Condor `v37_risk_manager`, E4 `pmm_simple`, Cup dashboard).
