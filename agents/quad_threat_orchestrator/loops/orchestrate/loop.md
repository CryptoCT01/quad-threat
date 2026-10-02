---
name: Orchestrate
description: The one Quad Threat loop. E2 is the risk manager for E1 + E2. Allocates the shared capital pool across E1, E2 and E4, keeps the E1/E4 bots alive, enforces the daily kill switch and the E4 pause rule, and makes the E2 trade decisions.
agent_key: openrouter:deepseek/deepseek-v4.1-flash
skills: [e1_momentum_scalp, e2_llm_breakout, e4_btc_eth_maker]
default_config: {}
---

# Quad Threat orchestrate loop

Every tick, in order:

1. **Read the book.** `get_portfolio_overview` (equity, free margin, positions) and `manage_bots(action="status")`.
2. **Daily kill switch (first, before anything opens).** On the first tick of each UTC day journal `day_start_equity`. If equity (realised + unrealised) is **$80 or more below** `day_start_equity`: stop all engines — `manage_bots` stop `quad-e1*` and `quad-e4*`, MARKET CLOSE every E2 leg (cancel its stop), open nothing, journal "DAILY KILL", alert the human. Stay flat until the next UTC day or until the human restarts.
3. **Allocate capital** with the fixed split in AGENT.md (buffer $120 never deployed, E4 ~$400, E1 $160, E2 $120).
4. **E1** (skill `e1_momentum_scalp`): if no bot named `quad-e1*` is present, deploy `quad-e1` once. If present, adopt it. Journal health.
5. **E4** (skill `e4_btc_eth_maker`):
   - **E4 pause rule.** Journal E4 realised PnL every tick. If E4's realised loss over the last 60 minutes is **$14 or more**, stop the `quad-e4*` bot, journal `E4 PAUSED until <now+30min>`, and do not redeploy before then. After 30 minutes, redeploy `quad-e4` once with the same two configs and journal "E4 resumed".
   - Otherwise (not paused): if no `quad-e4*` bot is present, deploy `quad-e4` once; if present, adopt it.
6. **E2 risk manager for E1 + E2** (skill `e2_llm_breakout`, section "Risk manager"): read ALL venue positions and active executors; classify E1 (`v37_scalp_multi` / `quad-e1*`), E2 (this agent), E4 (never touch) and unowned (never touch). For E1 legs: E1 day PnL ≤ −$30 → stop `quad-e1*` and reduce-only close all E1 legs; naked/orphaned → reduce-only STOP at 0.8% (else close); invalidated (bot down, held > 3h10m, or ≥ 0.8% against) → close; ≥ +1% → breakeven stop; ≥ +1.5% → close 50%. Never open E1 legs. Journal every action.
7. **E2 trading** (skill `e2_llm_breakout`): `e2_position_board`; every open **must** have a hard venue stop at the ATR-style stop% (1.5 × mean 15m range, clamped 0.5%–1%), margin scaled so stop% × 20 × margin ≤ $2.50; CLOSE/PARTIAL from live PnL; at most one new OPEN per tick if a slot is free.
8. **Risk:** if an engine's drawdown cap (AGENT.md) is used, stop opening for that engine and journal it. The agent stops bots only for the daily kill switch, the E4 pause rule, or when the human asks.
9. **Journal** one line per engine (status, legs, realised/unrealised PnL), the allocation used, day PnL vs the $80 limit and E4's last-hour realised PnL vs the $14 limit, and every risk-manager action.
