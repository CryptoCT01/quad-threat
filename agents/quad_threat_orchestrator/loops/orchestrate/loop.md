# Quad Threat — deploy E1 + E4 from Condor

Objective: judged run starts **inside Condor**. Hummingbot V2 controllers still do the trading; this loop only deploys them.

## Tick

1. `manage_bots(action="status")`.
2. Deploy `quad-e1` / `quad-e4` if missing (see AGENT.md).
3. Do not flatten. Do not open extra pairs.
4. HOLD when both bots show running.

Paper first. Controllers must already exist on the Hummingbot API (`conf_v37_scalp_multi.yml`, `conf_e4_quad_btc.yml`, `conf_e4_quad_eth.yml`).
