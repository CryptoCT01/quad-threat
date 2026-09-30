---
name: Orchestrate
description: The one Quad Threat loop. Allocates the shared capital pool across E1, E2 and E4, keeps the E1/E4 bots alive, and makes the E2 trade decisions.
agent_key: openrouter:deepseek/deepseek-v4.1-flash
skills: [e1_momentum_scalp, e2_llm_breakout, e4_btc_eth_maker]
default_config: {}
---

# Quad Threat orchestrate loop

Every tick, in order:

1. **Read the book.** `get_portfolio_overview` (equity, free margin, positions) and `manage_bots(action="status")`.
2. **Allocate capital** with the rule in AGENT.md. Compute each engine's margin budget and drawdown share from current equity.
3. **E1** (skill `e1_momentum_scalp`): if no bot named `quad-e1*` is present, deploy `quad-e1` once. If present, adopt it. Journal health.
4. **E4** (skill `e4_btc_eth_maker`): same for `quad-e4*`.
5. **E2** (skill `e2_llm_breakout`): run `e2_position_board`, manage open E2 **positions** from live PnL (CLOSE / PARTIAL_CLOSE via order executor), then open at most one new E2 **order executor** if a slot is free.
6. **Risk:** if total drawdown hits the whole-book limit, stop opening E2, journal it, and alert the human. Never stop E1/E4 bots unless the human asked.
7. **Journal** one line per engine (status, legs, PnL) and the allocation used.
