# Quad Threat — Condor agent for Bitget USDT-M perps

One Condor agent (`agents/quad_threat_orchestrator/`) runs three engines on one Bitget account,
using only Condor's standard built-in tools (`manage_bots`, `manage_controllers`, executor tools, journal).

| Engine | Where | What |
|---|---|---|
| **E1** scalp | loop `orchestrate` → bot `quad-e1` | Hummingbot V2 controller `v37_scalp_multi` (multi-pair ROC/RSI/BB scalp) |
| **E2** breakout | loop `e2_breakout` | LLM trader, position executors on XAU/CL/DOGE/NEAR/LTC at 20x |
| **E4** maker | loop `orchestrate` → bot `quad-e4` | Hummingbot V2 controller `pmm_quad` on BTC/ETH (renamed so it never overwrites stock `pmm_simple`) |

## Layout
```
agents/quad_threat_orchestrator/
  AGENT.md                 # agent definition (server_name: local, openrouter:deepseek/deepseek-v4.1-flash)
  loops/orchestrate/       # deploys + adopts quad-e1 / quad-e4 (never duplicates)
  loops/e2_breakout/       # Engine 2 playbook + config
  routines/                # e2_order_sizer (venue-safe sizing), market_analysis
  controllers/             # controller code: generic/v37_scalp_multi.py, market_making/pmm_quad.py
  conf/                    # controller configs + E1 universe / strategy toggles
tests/                     # pytest against the real hummingbot package
docs/
```

## Install
1. Copy `agents/quad_threat_orchestrator` into Condor `agents/`.
2. Copy `controllers/*` into the Hummingbot API `bots/controllers/` and upload `conf/conf_*.yml` with `manage_controllers`
   (put `universe.yml` / `active_strategy.json` next to the controller configs).
3. Set `.env` from `.env.example` (OpenRouter key, Hummingbot API URL/credentials). Never commit real values.
4. Start the `orchestrate` and `e2_breakout` loops from Condor.

## Sizing (this pack is sized for an ~$800 book)
- **E1:** `position_size_quote: 25` margin × `max_open_positions: 3` (`total_amount_quote: 75`), leverage from `universe.yml`
  (SOL/XRP 10x, else 5x). The controller's code default (`10`) is only used if the config omits the field; the YAML is authoritative.
- **E2:** $25 margin per position, max 3, 0.4% TP / 0.5% SL floor. Stock Condor has no SL-amend tool, so profit protection exits at market via `stop_executor`.
- **E4:** BTC/ETH, 8 bps each side, LIMIT TP 6 bps, SL 15 bps.
Scale all sizes and the deploy drawdown caps down on a small live book.

## Tests
```
pip install hummingbot pytest pyyaml
pytest -q tests
```

## Safety
No API keys, passwords or chat ids are stored here. If you find a secret in a clone, rotate it.
