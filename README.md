# Quad Threat — Condor agent for Bitget USDT-M perps

One Condor agent (`agents/quad_threat_orchestrator/`) runs three engines on one Bitget account,
using only Condor's standard built-in tools (`manage_bots`, `manage_controllers`, executor tools, journal).

| Engine | Where | What |
|---|---|---|
| **E1** scalp | loop `orchestrate` → bot `quad-e1` | Hummingbot V2 controller `v37_scalp_multi` (multi-pair ROC/RSI/BB scalp) |
| **E2** breakout | loop `orchestrate` + skill `e2_llm_breakout` | LLM: **order executor** open → Position (breakeven + live PnL) → close/partial with a reduce order. No exchange TP/SL amend. |
| **E4** maker | loop `orchestrate` → bot `quad-e4` | Hummingbot V2 controller `pmm_quad` on BTC/ETH (renamed so it never overwrites stock `pmm_simple`) |

## Finals setup ($800)

**How we are scored:** 40% PnL, 40% trading volume, 20% public vote. So the book is built to trade a lot of volume
cheaply (E4 maker), while keeping every loss small and capped. Fees are Bitget VIP 5: **0.01% maker, 0.032% taker**.

**How the $800 is split** (one Bitget account, one shared pool — engines do not borrow from each other):

| Engine | Share | Setup |
|---|---|---|
| Cash buffer | **$120** | Never deployed. Spare margin so nothing gets liquidated. |
| **E4** BTC/ETH maker | **~$400** | Up to $500 quoted per pair (BTC, ETH) at 100x, 8 bps from mid |
| **E1** momentum scalp | **$160** | 4 positions × $28 margin = $112, leverage from `universe.yml` (5–10x) |
| **E2** LLM breakout | **$120** | 3 legs × up to $20 margin at 20x |

**Stops and risk caps**
- **Daily kill switch:** if the day's loss reaches **$80** (10% of the book), the agent stops all three engines for the rest of the day.
- **E4:** take profit 6 bps (limit), stop loss 15 bps, max hold 3 minutes, quoting halts on a 0.6% crash move.
  If E4 loses **$14 or more in one hour** (about the 95th-percentile hourly loss in our replay) (realised), the agent pauses E4 for **30 minutes** and then restarts it.
- **E1:** stop 0.8%, take profit 1.6%, trailing stop, max 4 positions.
- **E2:** every trade gets a hard stop on the exchange the moment it opens. The stop is sized to the market:
  1.5 × the pair's typical 15-minute move (mean 15m candle range over ~24h), never tighter than 0.5% or wider than 1%.
  Margin is scaled down so one stop-out costs at most **$2.50** (stop% × 20x × margin ≤ $2.50).
- Per-engine daily caps: E4 $40, E1 $30, E2 $15. The $80 kill switch is the binding limit for the whole book.
- E4 quote refresh only cancels **unfilled** quotes; a filled quote is never market-closed by a refresh, it exits only by take profit, stop or the 3-minute timer.

**E4 fee maths (why the maker engine makes sense)**
- Entry and take-profit are both maker orders: 0.01% + 0.01% ≈ **2 bps** round trip.
  Real take-profit fills averaged about 5.6 bps before fees in our replay, so a win nets about **3.6 bps** (≈ $0.18 per $500 order).
- A stop-out costs about **19 bps**: the 15 bps move + 1 bp maker entry + 3.2 bps taker exit (≈ $0.96 per $500).
  E4 needs roughly 5 winners per loser (≈ 84% win rate) to break even, which is why the 1-hour pause and the daily kill switch exist.

**Expected volume:** E4 should trade roughly **$8–12k per active hour** at $500 per order. This is an estimate: our replay
of real September fills showed about $15–25k per active hour at $1,000 per order, halved here. Real fills depend on market activity.
E1 and E2 add smaller, directional volume on top.

**How the engines share capital:** one agent, one loop (`orchestrate`). Each tick it reads account equity, checks the
kill switch and the E4 pause, then gives each engine its fixed share (scaled if equity moves away from $800).
An engine that is flat does not lend its share to another, and the $120 buffer is never used.

## E2 is the risk manager for E1 + E2

Every tick, E2 reads **all** open positions and executors and acts on E1's legs as well as its own (stock Condor tools only:
positions read, reduce-only order-executor CLOSE / STOP, `stop_executor`, stop bot):

| E1 check | Action |
|---|---|
| E1 day loss reaches **$30** | Stop the E1 bot and close all E1 legs until the next day |
| Leg is **naked or orphaned** (no stop / TP working) | Add a reduce-only stop at E1's 0.8% SL; if that fails, close it |
| Leg is **invalidated** (E1 bot down, held past 3h10m, or already 0.8% against) | Close it |
| Unrealised profit **≥ +1%** of notional | Move the stop to breakeven |
| Unrealised profit **≥ +1.5%** | Close half, keep a breakeven stop on the rest |

- E2 **never opens** E1 legs; E1's own controller still opens them and runs its normal SL / TP / trailing stop.
- E2 manages its own legs as before (hard venue stop on every open).
- E2 **never touches E4's maker orders** (only the daily kill switch or the E4 pause stop E4), and **never touches positions
  Quad Threat did not open** (manual trades, other bots).
- Every action is written to the journal.

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
- **E1:** `position_size_quote: 28` margin × `max_open_positions: 4` (`total_amount_quote: 112`; set in both the conf and `universe.yml`, which the controller reads), leverage from `universe.yml`
  (SOL/XRP 10x, else 5x). The controller's code default (`10`) is only used if the config omits the field; the YAML is authoritative.
- **E2:** up to $20 × 3 at 20x. **Hybrid:** run `e2_order_sizer` first (amount is **base coins**, never `20`; it also returns the ATR-style `stop_pct` and scaled margin). MARKET order-executor OPEN → **hard STOP close on the venue** (1.5× mean 15m range, 0.5%–1%, ≤ $2.50 risk) → agent CLOSE/PARTIAL from live PnL. No `modify_tpsl`. Viz: `e2_position_board`.
- **E4:** BTC/ETH at 100x, 8 bps each side (one-side + inventory skew), LIMIT TP 6 bps, SL 15 bps, TIME 3 min, `total_amount_quote: 500` per pair, refresh 60s (unfilled quotes only). Pauses 30 min after a $14 realised loss in one hour.
Scale all sizes and the deploy drawdown caps down on a small live book.

## Tests
```
pip install hummingbot pytest pyyaml
pytest -q tests
```

## Safety
No API keys, passwords or chat ids are stored here. If you find a secret in a clone, rotate it.
