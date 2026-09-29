"""Finals fixes: venue-safe sizing, entry failure surfacing, config compatibility.

Runs against the real Hummingbot package (tested with hummingbot==20260920 from
PyPI). Trading rules mimic bitget_perpetual: min_order_value = minTradeUSDT,
min_notional_size left at 0, min_order_size = minTradeNum, increment = sizeMultiplier.
"""
import asyncio
import importlib.util
import logging
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

pytest.importorskip("hummingbot")

from hummingbot.connector.trading_rule import TradingRule  # noqa: E402
from hummingbot.core.data_type.common import TradeType  # noqa: E402
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction  # noqa: E402

from controllers.generic import v37_scalp_multi as v37  # noqa: E402
from controllers.market_making import pmm_quad as pmm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AG = ROOT / "agents" / "quad_threat_orchestrator"


def bitget_rule(pair, min_trade_num, size_mult, price_place, min_usdt="5"):
    """What bitget_perpetual_derivative builds from /api/v2/mix/market/contracts."""
    return TradingRule(
        trading_pair=pair,
        min_order_value=Decimal(min_usdt),
        min_order_size=Decimal(min_trade_num),
        min_price_increment=Decimal(f"1e-{price_place}"),
        min_base_amount_increment=Decimal(size_mult),
    )


LTC = bitget_rule("LTC-USDT", "0.001", "0.001", 2)
BTC = bitget_rule("BTC-USDT", "0.0001", "0.0001", 1)
TSLA = bitget_rule("TSLA-USDT", "0.01", "0.01", 2)


def connector_accepts(amount, price, rule):
    """Replay ExchangePyBase._create_order's client-side checks + Bitget's minTradeUSDT."""
    step = rule.min_base_amount_increment
    q = (Decimal(amount) / step).to_integral_value(rounding="ROUND_DOWN") * step
    notional = q * Decimal(price)
    return (
        q >= rule.min_order_size
        and notional >= rule.min_notional_size
        and notional >= rule.min_order_value  # venue-side (Bitget rejects < minTradeUSDT)
    )


HELPER_MODULES = [v37, pmm]



def _sizer():
    # The routine imports Condor-only modules at top level; stub just those.
    for name in ("telegram", "telegram.ext", "config_manager", "routines", "routines.base"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["telegram.ext"].ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["config_manager"].get_client = None
    sys.modules["routines.base"].RoutineResult = dict
    path = AG / "routines" / "e2_order_sizer.py"
    spec = importlib.util.spec_from_file_location("qt_sizer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ALL_HELPERS = HELPER_MODULES + [_sizer()]


# ---------------------------------------------------------------- blocker 4
@pytest.mark.parametrize("mod", ALL_HELPERS, ids=lambda m: m.__name__)
def test_min_notional_checked_after_rounding(mod):
    price = Decimal("97.37")
    raw = Decimal("5") / price                  # exactly the venue minimum, 0.05135...
    naive = (raw / LTC.min_base_amount_increment).to_integral_value("ROUND_DOWN") * LTC.min_base_amount_increment
    assert not connector_accepts(naive, price, LTC)   # old behaviour: 0.051 -> $4.97 rejected
    amount, bumped = mod.venue_safe_amount(raw, price, LTC, budget_quote=Decimal("5"))
    assert bumped
    assert amount % LTC.min_base_amount_increment == 0
    assert amount * price >= Decimal("5") * Decimal("1.05")
    assert connector_accepts(amount, price, LTC)
    # bump costs at most one increment above 105% of the minimum
    assert amount * price - Decimal("5.25") <= LTC.min_base_amount_increment * price


@pytest.mark.parametrize("mod", ALL_HELPERS, ids=lambda m: m.__name__)
def test_large_order_only_quantized_down(mod):
    price = Decimal("64123.4")
    amount, bumped = mod.venue_safe_amount(Decimal("400") / price, price, BTC, budget_quote=Decimal("400"))
    assert not bumped
    assert amount == Decimal("0.0062")
    assert connector_accepts(amount, price, BTC)


@pytest.mark.parametrize("mod", ALL_HELPERS, ids=lambda m: m.__name__)
def test_min_order_size_dominates(mod):
    price = Decimal("250")                    # TSLA: 0.01 * 250 = $2.5 < $5, so notional rules
    amount, bumped = mod.venue_safe_amount(Decimal("0.005"), price, TSLA, budget_quote=Decimal("6"))
    assert bumped and amount == Decimal("0.03")  # ceil(5.25/250 / 0.01) * 0.01
    big = bitget_rule("X-USDT", "1", "1", 4, min_usdt="5")
    amount, bumped = mod.venue_safe_amount(Decimal("0.4"), Decimal("20"), big, budget_quote=Decimal("21"))
    assert bumped and amount == Decimal("1")


# ---------------------------------------------------------------- blocker 3
@pytest.mark.parametrize("mod", ALL_HELPERS, ids=lambda m: m.__name__)
def test_budget_below_venue_minimum_refused(mod):
    with pytest.raises(mod.VenueMinimumError, match="below the venue minimum"):
        mod.venue_safe_amount(Decimal("0.04"), Decimal("100"), LTC, budget_quote=Decimal("4"))


@pytest.mark.parametrize("mod", ALL_HELPERS, ids=lambda m: m.__name__)
def test_dict_rules_from_hummingbot_api(mod):
    rule = {"min_order_size": 0.001, "min_base_amount_increment": 0.001, "min_price_increment": 0.01,
            "min_notional_size": 0.0, "min_order_value": 5.0}
    amount, bumped = mod.venue_safe_amount(Decimal("5") / Decimal("97.37"), Decimal("97.37"), rule,
                                           budget_quote=Decimal("5"))
    assert bumped and amount * Decimal("97.37") >= Decimal("5.25")


def test_no_rules_passes_through():
    assert v37.venue_safe_amount(Decimal("0.123456"), Decimal("10"), None) == (Decimal("0.123456"), False)


def test_quantize_price_side_aware():
    assert pmm.quantize_price(Decimal("64123.456"), BTC, TradeType.BUY) == Decimal("64123.4")
    assert pmm.quantize_price(Decimal("64123.456"), BTC, TradeType.SELL) == Decimal("64123.5")


# ---------------------------------------------------------------- config compat (latest HB)
def _yaml(rel):
    return yaml.safe_load((AG / rel).read_text())


@pytest.mark.parametrize("rel", ["conf/conf_e4_quad_btc.yml", "conf/conf_e4_quad_eth.yml"])
def test_e4_configs_load_on_latest_hummingbot(rel):
    cfg = pmm.PMMQuadConfig(**_yaml(rel))
    assert cfg.id and cfg.trading_pair in {"BTC-USDT", "ETH-USDT"}
    assert cfg.connector_name == "bitget_perpetual"


def test_e1_config_loads_on_latest_hummingbot():
    cfg = v37.V37ScalpMultiConfig(**_yaml("conf/conf_v37_scalp_multi.yml"))
    assert cfg.id == "v37_scalp_multi"
    cfg.triple_barrier_config  # validators in PositionExecutor data types accept it


# ---------------------------------------------------------------- pmm_quad integration
class FakeMDP:
    def __init__(self, rule, price):
        self.rule, self.price, self.connectors = rule, Decimal(price), {}

    def time(self):
        return 1_790_000_000.0

    def get_trading_rules(self, connector, pair):
        return self.rule

    def get_price_by_type(self, *a, **k):
        return self.price

    def get_candles_df(self, *a, **k):
        return None


def _pmm(rel, rule, price, **overrides):
    data = _yaml(rel)
    data.update(overrides)
    ctrl = object.__new__(pmm.PMMQuadController)
    ctrl.config = pmm.PMMQuadConfig(**data)
    ctrl.market_data_provider = FakeMDP(rule, price)
    ctrl.processed_data = {"reference_price": Decimal(price), "spread_multiplier": Decimal("1")}
    ctrl.executors_info = []
    ctrl.positions_held = []
    ctrl._lev_set = True
    return ctrl


def test_pmm_e4_live_config_quotes_both_sides():
    ctrl = _pmm("conf/conf_e4_quad_btc.yml", BTC, "64123.4")
    actions = ctrl.determine_executor_actions()
    creates = [a for a in actions if isinstance(a, CreateExecutorAction)]
    assert {a.executor_config.side for a in creates} == {TradeType.BUY, TradeType.SELL}
    for a in creates:
        c = a.executor_config
        assert isinstance(c, PositionExecutorConfig) and a.controller_id == "e4_quad_btc"
        assert c.amount % BTC.min_base_amount_increment == 0
        assert c.entry_price % BTC.min_price_increment == 0
        assert connector_accepts(c.amount, c.entry_price, BTC)


def test_pmm_total_amount_equal_to_venue_minimum_still_quotes():
    # buy 50% + sell 50% of total -> each level gets exactly the $5 venue minimum.
    ctrl = _pmm("conf/conf_e4_quad_eth.yml", LTC, "97.37", total_amount_quote=10)
    creates = [a for a in ctrl.determine_executor_actions() if isinstance(a, CreateExecutorAction)]
    assert len(creates) == 2
    for a in creates:
        assert connector_accepts(a.executor_config.amount, a.executor_config.entry_price, LTC)


def test_pmm_total_amount_below_venue_minimum_refused_loudly(caplog):
    ctrl = _pmm("conf/conf_e4_quad_eth.yml", LTC, "97.37", total_amount_quote=8)
    with caplog.at_level(logging.ERROR):
        creates = [a for a in ctrl.determine_executor_actions() if isinstance(a, CreateExecutorAction)]
    assert creates == []
    assert "below the venue minimum" in caplog.text


# ---------------------------------------------------------------- v37 integration
def _v37(position_size, rule, price, pair="SOL-USDT"):
    cfg = v37.V37ScalpMultiConfig(**_yaml("conf/conf_v37_scalp_multi.yml"))
    cfg.trading_pairs = [pair]
    cfg.leverage_map = {pair: 10}
    cfg.position_size_quote = Decimal(str(position_size))
    ctrl = object.__new__(v37.V37ScalpMultiController)
    ctrl.config = cfg
    ctrl.market_data_provider = FakeMDP(rule, price)
    ctrl.executors_info = []
    ctrl.positions_held = []
    ctrl._last_entry_ts = {}
    ctrl._ensure_leverage = lambda p, lev: True
    ctrl._e2_claimed_pairs = lambda: set()
    ctrl.processed_data = {
        "candidates": [{"symbol": pair, "ready": True, "signal": 1, "score": 0.7,
                        "strategy_tag": "SUPER_A+ROC_RSI", "votes": 2}],
        "active_pairs": [], "open_slots": 1,
    }
    return ctrl


SOL = bitget_rule("SOL-USDT", "0.1", "0.1", 3)


def test_v37_entry_places_venue_safe_order():
    ctrl = _v37(25, SOL, "151.234")
    (action,) = ctrl.create_actions_proposal()
    c = action.executor_config
    assert action.controller_id == "v37_scalp_multi"
    assert c.amount == Decimal("1.6")  # $250 notional / 151.234 = 1.653 -> 1.6 (down, no bump)
    assert connector_accepts(c.amount, c.entry_price, SOL)


def test_v37_budget_just_above_min_trade_num_quantizes_to_min():
    # $1.6 margin x10 = $16 notional; raw 0.1058 SOL -> 0.1 (= minTradeNum, $15.12 >= $5)
    ctrl = _v37("1.6", SOL, "151.234")
    (action,) = ctrl.create_actions_proposal()
    assert action.executor_config.amount == Decimal("0.1")
    assert connector_accepts(action.executor_config.amount, action.executor_config.entry_price, SOL)


ADA = bitget_rule("ADA-USDT", "1", "1", 5)


def test_v37_budget_equal_to_venue_min_notional_bumps_not_dies():
    # $0.5 margin x10 = $5 = minTradeUSDT. raw 41.67 ADA -> 41 ($4.92, rejected by Bitget);
    # fixed: rounded UP to 44 ADA ($5.28) and still passes the 1.05x margin guard (+1 step).
    ctrl = _v37("0.5", ADA, "0.12", pair="ADA-USDT")
    (action,) = ctrl.create_actions_proposal()
    assert not connector_accepts(Decimal("41"), Decimal("0.12"), ADA)
    assert action.executor_config.amount == Decimal("44")
    assert connector_accepts(action.executor_config.amount, action.executor_config.entry_price, ADA)


def test_v37_budget_below_minimum_refused_with_log(caplog):
    ctrl = _v37("1", SOL, "151.234")       # $10 notional < 0.1 SOL ($15.12)
    with caplog.at_level(logging.WARNING):
        assert ctrl.create_actions_proposal() == []
    assert "below the venue minimum" in caplog.text


def test_v37_entry_exception_is_logged_not_silent(caplog):
    ctrl = _v37(25, SOL, "151.234")
    ctrl.market_data_provider.get_price_by_type = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        assert ctrl.create_actions_proposal() == []
    assert "not submitted" in caplog.text and "boom" in caplog.text


def test_v37_failed_entry_retries_fast(caplog):
    from hummingbot.strategy_v2.models.executors import CloseType
    ctrl = _v37(25, SOL, "151.234")
    failed = types.SimpleNamespace(id="ex1", trading_pair="SOL-USDT", is_active=False,
                                   close_type=CloseType.FAILED, close_timestamp=1.0,
                                   filled_amount_quote=Decimal("0"), config=None)
    ctrl.executors_info = [failed]
    ctrl.filter_executors = lambda executors, filter_func: [e for e in executors if filter_func(e)]
    with caplog.at_level(logging.ERROR):
        assert ctrl._effective_cooldown("SOL-USDT") == v37.ENTRY_RETRY_SEC
    assert "no order reached the exchange" in caplog.text
    failed.close_type = CloseType.STOP_LOSS
    assert ctrl._effective_cooldown("SOL-USDT") == 2 * 1500
