"""Run the E2 bridge against REAL upstream Condor modules (hummingbot/condor HEAD).

Usage (needs a Condor checkout with its venv):
    cd <condor> && PYTHONPATH=. .venv/bin/python <quad-threat>/tests/condor_integration_check.py <quad-threat>

The Hummingbot API client is faked (no network, no keys, no orders); everything
Condor-side (typed create tool, trading-rule pre-check, read-back) is the real code.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

QT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
BITGET_LTC = {"min_order_size": 0.001, "max_order_size": None, "min_price_increment": 0.01,
              "min_base_amount_increment": 0.001, "min_quote_amount_increment": 1e-20,
              "min_notional_size": 0.0, "min_order_value": 5.0}


class FakeExecutors:
    def __init__(self, script):
        self.script, self.created = list(script), []

    async def create_executor(self, executor_config, account_name=None, controller_id=None):
        self.created.append({"config": executor_config, "controller_id": controller_id})
        return self.script.pop(0)

    async def get_executor(self, executor_id):
        return {"id": executor_id, "status": "RUNNING", "close_type": None, "filled_amount_quote": 5.3}


def client(script):
    c = types.SimpleNamespace()
    c.executors = FakeExecutors(script)
    async def get_trading_rules(connector_name, trading_pairs):
        return {trading_pairs[0]: dict(BITGET_LTC)}
    async def get_prices(connector_name, trading_pairs):
        return {"prices": {trading_pairs: 97.37}}
    c.connectors = types.SimpleNamespace(get_trading_rules=get_trading_rules)
    c.market_data = types.SimpleNamespace(get_prices=get_prices)
    return c


async def main():
    from mcp_servers.hummingbot_api import hummingbot_client
    from mcp_servers.hummingbot_api.tools import executor_create

    executor_create.READ_BACK_INTERVAL_SECONDS = 0.01
    ok = True

    # 0) the old file cannot even be imported by current Condor
    try:
        compile((QT / "condor-mcp" / "executors.py").read_text(), "new", "exec")
        print("new condor-mcp/executors.py compiles: OK")
    except SyntaxError as e:
        ok = False; print("new file syntax error", e)
    try:
        from mcp_servers.hummingbot_api.schemas import ManageExecutorsRequest  # noqa: F401
        print("ManageExecutorsRequest still exists (unexpected)")
    except ImportError:
        print("upstream: schemas.ManageExecutorsRequest is GONE -> old executors.py cannot import: CONFIRMED")

    # 1) stock pre-check ignores Bitget's min_order_value: a $4.92 order passes it
    hummingbot_client.trading_rules_cache.clear()
    v = await executor_create._trading_rule_violation(
        client([]), "position_executor",
        {"connector_name": "bitget_perpetual", "trading_pair": "LTC-USDT", "amount": 0.0505, "entry_price": 97.37})
    print(f"stock Condor pre-check on 0.0505 LTC (~$4.92 after rounding to 0.050): violation={v!r}")

    spec = importlib.util.spec_from_file_location("qt_e2", QT / "condor-mcp" / "executors.py")
    e2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(e2)

    # 2) id=None on first submit -> failure + one retry -> live
    hummingbot_client.trading_rules_cache.clear()
    c = client([{"executor_id": None}, {"executor_id": "EX-OK"}])
    r = await e2.create_e2_position(c, trading_pair="LTC-USDT", side=1)
    cfg = c.executors.created[0]["config"]
    print("bridge id=None->retry:", r["ok"], r.get("attempts"), r.get("executor_id"),
          "| amount", cfg["amount"], "notional", round(cfg["amount"] * 97.37, 4),
          "| controller_id", c.executors.created[0]["controller_id"], "| tbc", cfg.get("triple_barrier_config"))
    ok &= bool(r["ok"] and r["attempts"] == 2 and cfg["amount"] * 97.37 >= 5.25)
    ok &= isinstance(cfg["amount"], float) and cfg["side"] == 1 and cfg["leverage"] == 20

    # 3) id=None twice -> reported as NOT open
    hummingbot_client.trading_rules_cache.clear()
    r = await e2.create_e2_position(client([{"executor_id": None}, {"executor_id": None}]), trading_pair="LTC-USDT", side=2)
    print("bridge id=None x2:", r["ok"], "|", r["error"])
    ok &= not r["ok"]

    # 4) budget below venue minimum refused before any POST
    hummingbot_client.trading_rules_cache.clear()
    c = client([])
    r = await e2.create_e2_position(c, trading_pair="LTC-USDT", side=1, margin_quote=0.2)
    print("bridge $0.2 margin x20 = $4 < $5:", r["ok"], "|", r["error"], "| POSTs:", len(c.executors.created))
    ok &= (not r["ok"]) and not c.executors.created

    print("ALL OK" if ok else "FAILURES")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
