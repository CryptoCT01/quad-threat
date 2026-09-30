# Quad Threat — Condor agent for Bitget USDT-M perps

One Condor agent (`agents/quad_threat_orchestrator/`) runs three engines on one Bitget account,
using only Condor's standard built-in tools (`manage_bots`, `manage_controllers`, executor tools, journal).

| Engine | Where | What |
|---|---|---|
| **E1** scalp | loop `orchestrate` → bot `quad-e1` | Hummingbot V2 controller `v37_scalp_multi` (multi-pair ROC/RSI/BB scalp) |
| **E2** breakout | loop `orchestrate` + skill `e2_llm_breakout` | LLM trader, position executors on DOGE/NEAR/LTC at 20x (stock executor tools) |
| **E4** maker | loop `orchestrate` → bot `quad-e4` | Hummingbot V2 controller `pmm_quad` on BTC/ETH (renamed so it never overwrites stock `pmm_simple`) |

## Layout
```
agents/quad_threat_orchestrator/
  AGENT.md                 # agent definition (server_name: local, openrouter:deepseek/deepseek-v4.1-flash)
  loops/orchestrate/       # ONE loop: shared-capital allocation, E1/E4 bots, E2 decisions
  skills/                  # one SKILL.md per engine (E1, E2, E4)
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
4. Start the single `orchestrate` loop from Condor; it allocates the shared capital pool (see AGENT.md).

## Sizing (this pack is sized for an ~$800 book)
- **E1:** `position_size_quote: 20` margin × `max_open_positions: 3` (`total_amount_quote: 60`), leverage from `universe.yml`
  (SOL/XRP 10x, else 5x). The controller's code default (`10`) is only used if the config omits the field; the YAML is authoritative.
- **E2:** $20 margin per position, max 3, 0.4% TP / 0.5% SL floor. Stock Condor has no SL-amend tool, so profit protection exits at market via `stop_executor`.
- **E4:** BTC/ETH at 100x, 8 bps each side (one-side + inventory skew), LIMIT TP 6 bps, SL 15 bps, TIME 3 min, `total_amount_quote: 500` per pair, refresh 60s.
Scale all sizes and the deploy drawdown caps down on a small live book.

## Tests
```
pip install hummingbot pytest pyyaml
pytest -q tests
```

## Safety
No API keys, passwords or chat ids are stored here. If you find a secret in a clone, rotate it.
