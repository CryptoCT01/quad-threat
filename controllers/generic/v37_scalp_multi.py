"""
v37_scalp_multi.py — Multi-pair directional controller (crypto + stock perps)

- Scans full universe from conf/universe.yml
- Respects dashboard strategy toggles (conf/active_strategy.json)
- Ranks READY opportunities and opens best trades
- Hard cap: max_open_positions (default 3)
- Per-symbol leverage from universe map
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yaml
from pydantic import Field, field_validator

from hummingbot.core.data_type.common import MarketDict, OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TrailingStop,
    TripleBarrierConfig,
)
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction
from hummingbot.strategy_v2.utils.common import parse_enum_value

_CONF_ROOTS = [
    Path("/home/hummingbot/conf"),
    Path(__file__).resolve().parents[2] / "conf",
]
_E4_PAIRS = {"BTC-USDT", "ETH-USDT"}
_E2_BASKET = {"XAU-USDT", "CL-USDT", "DOGE-USDT", "NEAR-USDT", "LTC-USDT"}
_STOCK_PAIRS = {
    "TSLA-USDT", "NVDA-USDT", "AAPL-USDT", "AMZN-USDT", "META-USDT", "MSFT-USDT",
    "GOOGL-USDT", "COIN-USDT", "MSTR-USDT", "HOOD-USDT", "PLTR-USDT", "AMD-USDT",
}
_E2_FILL_PATHS = [
    Path("/home/hummingbot/data/e2_fills.jsonl"),
    Path(__file__).resolve().parents[2] / "data" / "e2_fills.jsonl",
]


def _first_existing(*rels: str) -> Optional[Path]:
    for root in _CONF_ROOTS:
        for rel in rels:
            p = root / rel
            if p.is_file():
                return p
    return None


def load_universe() -> Dict[str, Any]:
    path = _first_existing("universe.yml")
    default_crypto = [
        {"symbol": "SOL-USDT", "leverage": 10, "tier": 2},
        {"symbol": "XRP-USDT", "leverage": 10, "tier": 1},
        {"symbol": "BNB-USDT", "leverage": 5, "tier": 2},
        {"symbol": "ADA-USDT", "leverage": 5, "tier": 2},
        {"symbol": "AVAX-USDT", "leverage": 5, "tier": 2},
    ]
    default_commodities: list = []
    default_stocks = [
        {"symbol": "TSLA-USDT", "leverage": 5, "tier": 2},
        {"symbol": "NVDA-USDT", "leverage": 5, "tier": 2},
        {"symbol": "AAPL-USDT", "leverage": 5, "tier": 2},
        {"symbol": "AMZN-USDT", "leverage": 5, "tier": 2},
        {"symbol": "META-USDT", "leverage": 5, "tier": 2},
        {"symbol": "MSFT-USDT", "leverage": 5, "tier": 2},
        {"symbol": "GOOGL-USDT", "leverage": 5, "tier": 2},
        {"symbol": "COIN-USDT", "leverage": 5, "tier": 3},
        {"symbol": "MSTR-USDT", "leverage": 5, "tier": 3},
        {"symbol": "HOOD-USDT", "leverage": 5, "tier": 3},
        {"symbol": "PLTR-USDT", "leverage": 5, "tier": 3},
        {"symbol": "AMD-USDT", "leverage": 5, "tier": 3},
    ]
    data: Dict[str, Any] = {
        "max_open_positions": 3,
        "position_size_quote": 10,
        "connector_name": "bitget_perpetual",
        "crypto": default_crypto,
        "commodities": default_commodities,
        "stocks": default_stocks,
        "stop_loss": 0.008,
        "take_profit": 0.016,
        "time_limit": 10800,
        "trailing_stop": "0.005,0.003",
        "cooldown_time": 1500,
        "score_threshold": 0.66,
    }
    if path:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            if raw:
                data.update(raw)
        except Exception:
            pass
    crypto = data.get("crypto") or default_crypto
    commodities = data.get("commodities") or default_commodities
    stocks = data.get("stocks") or default_stocks
    all_rows = list(crypto) + list(commodities) + list(stocks)
    data["leverage_map"] = {r["symbol"]: int(r.get("leverage") or 5) for r in all_rows if r.get("symbol")}
    data["tier_map"] = {r["symbol"]: int(r.get("tier") or 3) for r in all_rows if r.get("symbol")}
    data["symbols"] = [r["symbol"] for r in all_rows if r.get("symbol")]
    data["crypto"] = crypto
    data["stocks"] = stocks
    return data


def load_enabled_strategies() -> Set[str]:
    default = {"SUPER_A", "ROC_RSI", "BB_VOL"}
    path = _first_existing("active_strategy.json")
    if not path:
        return default
    try:
        data = json.loads(path.read_text())
        enabled = data.get("enabled") or data.get("enabled_strategies")
        if isinstance(enabled, list) and enabled:
            return {str(x).upper() for x in enabled}
        flags = set()
        for k in ("SUPER_A", "ROC_RSI", "BB_VOL"):
            if data.get(k) is True:
                flags.add(k)
            st = (data.get("strategies") or {}).get(k) or {}
            if st.get("active") is True:
                flags.add(k)
        return flags or default
    except Exception:
        return default


def parse_trailing(v) -> Optional[TrailingStop]:
    if v is None or v == "":
        return None
    if isinstance(v, TrailingStop):
        return v
    if isinstance(v, str):
        a, b = v.split(",")
        return TrailingStop(activation_price=Decimal(a.strip()), trailing_delta=Decimal(b.strip()))
    return None


class V37ScalpMultiConfig(ControllerConfigBase):
    controller_name: str = "v37_scalp_multi"
    controller_type: str = "generic"
    connector_name: str = Field(default="bitget_perpetual")
    # Comma-separated or list — filled from universe if empty
    trading_pairs: List[str] = Field(default_factory=list)
    max_open_positions: int = Field(default=3, ge=1, le=10)
    position_size_quote: Decimal = Field(default=Decimal("10"))
    total_amount_quote: Decimal = Field(default=Decimal("30"))  # size * max slots
    leverage_default: int = Field(default=5)
    # JSON string or dict: {"BTC-USDT": 5, ...}
    leverage_map: Dict[str, int] = Field(default_factory=dict)
    position_mode: PositionMode = Field(default="ONEWAY")
    stop_loss: Optional[Decimal] = Field(default=Decimal("0.008"))
    take_profit: Optional[Decimal] = Field(default=Decimal("0.016"))
    time_limit: Optional[int] = Field(default=10800)
    trailing_stop: Optional[TrailingStop] = Field(
        default=TrailingStop(activation_price=Decimal("0.005"), trailing_delta=Decimal("0.003"))
    )
    cooldown_time: int = Field(default=1500)
    # 2/3 engines ≈ 0.667; 0.66 rejects the old solo ceiling (1/3 + 0.25 = 0.58)
    score_threshold: float = Field(default=0.66)
    # signal params
    roc_period: int = 5
    roc_min: float = 0.2
    rsi_period: int = 14
    rsi_oversold: float = 40.0
    rsi_overbought: float = 60.0
    bb_period: int = 20
    bb_std: float = 2.0
    vol_ratio_min: float = 1.3
    super_a_m15_roc_period: int = 8
    super_a_h1_roc_period: int = 5
    super_a_min_combined_roc: float = 0.40

    @field_validator("trading_pairs", mode="before")
    @classmethod
    def _pairs(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return list(v)

    @field_validator("leverage_map", mode="before")
    @classmethod
    def _lev(cls, v):
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            return {str(k): int(val) for k, val in json.loads(v).items()}
        return {str(k): int(val) for k, val in dict(v).items()}

    @field_validator("trailing_stop", mode="before")
    @classmethod
    def _ts(cls, v):
        return parse_trailing(v)

    @field_validator("position_mode", mode="before")
    @classmethod
    def _pm(cls, v):
        return parse_enum_value(PositionMode, v, "position_mode")

    @field_validator("stop_loss", "take_profit", "position_size_quote", "total_amount_quote", mode="before")
    @classmethod
    def _dec(cls, v):
        if v is None or v == "":
            return None
        return Decimal(str(v))

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            time_limit=self.time_limit,
            trailing_stop=self.trailing_stop,
            open_order_type=OrderType.MARKET,
            take_profit_order_type=OrderType.LIMIT,
            stop_loss_order_type=OrderType.MARKET,
            time_limit_order_type=OrderType.MARKET,
        )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        pairs = self.trading_pairs or []
        if not pairs:
            uni = load_universe()
            pairs = [x["symbol"] for x in (uni.get("crypto") or []) + (uni.get("commodities") or []) + (uni.get("stocks") or [])]
        for p in pairs:
            markets = markets.add_or_update(self.connector_name, p)
        return markets


class V37ScalpMultiController(ControllerBase):
    def __init__(self, config: V37ScalpMultiConfig, *args, **kwargs):
        # hydrate pairs/leverage from universe if needed
        uni = load_universe()
        if not config.trading_pairs:
            config.trading_pairs = [
                x["symbol"]
                for x in (uni.get("crypto") or []) + (uni.get("commodities") or []) + (uni.get("stocks") or [])
                if x.get("symbol") and x["symbol"] not in _E4_PAIRS and x["symbol"] not in _E2_BASKET
            ]
        if not config.leverage_map:
            config.leverage_map = {
                x["symbol"]: int(x.get("leverage") or 5)
                for x in (uni.get("crypto") or []) + (uni.get("commodities") or []) + (uni.get("stocks") or [])
            }
        if uni.get("max_open_positions"):
            config.max_open_positions = int(uni["max_open_positions"])
        if uni.get("position_size_quote"):
            config.position_size_quote = Decimal(str(uni["position_size_quote"]))
        if uni.get("cooldown_time") is not None:
            config.cooldown_time = int(uni["cooldown_time"])
        if uni.get("stop_loss") is not None:
            config.stop_loss = Decimal(str(uni["stop_loss"]))
        if uni.get("take_profit") is not None:
            config.take_profit = Decimal(str(uni["take_profit"]))
        if uni.get("time_limit") is not None:
            config.time_limit = int(uni["time_limit"])
        if uni.get("trailing_stop"):
            ts = parse_trailing(uni["trailing_stop"])
            if ts is not None:
                config.trailing_stop = ts
        if uni.get("score_threshold") is not None:
            config.score_threshold = float(uni["score_threshold"])
        # Keep total_amount_quote aligned with slots × margin
        try:
            config.total_amount_quote = Decimal(str(config.position_size_quote)) * Decimal(
                str(config.max_open_positions)
            )
        except Exception:
            pass

        super().__init__(config, *args, **kwargs)
        self.config = config
        self._enabled: Set[str] = {"SUPER_A", "ROC_RSI", "BB_VOL"}
        self._enabled_ts = 0.0
        self._last_entry_ts: Dict[str, float] = {}
        # Set leverage on exchange for all pairs BEFORE any positions open
        self._set_all_leverage()
        # rate sources for all pairs
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=p)
            for p in config.trading_pairs
        ])
        self.processed_data = {
            "candidates": [],
            "open_slots": config.max_open_positions,
            "enabled": sorted(self._enabled),
            "universe": config.trading_pairs,
            "leverage_map": config.leverage_map,
        }

    def _set_all_leverage(self):
        """Set leverage on the exchange for all pairs via Hummingbot API + connector."""
        import urllib.request
        import json as _json
        ok = 0
        fail = 0
        for pair, lev in self.config.leverage_map.items():
            if self._ensure_leverage(pair, int(lev)):
                ok += 1
            else:
                fail += 1
        try:
            self.logger().info(f"Leverage set ok={ok} fail={fail} (target map size={len(self.config.leverage_map)})")
        except Exception:
            pass

    def _ensure_leverage(self, pair: str, lev: int) -> bool:
        """Force exchange leverage for one pair. Returns True on success.

        Critical: PositionExecutorConfig.leverage is metadata only. If Bitget
        stays at a lower leverage than we size for, margin locked = notional/actual_lev
        (e.g. SOL sized for 10x/$10 margin opens $100 notional → $20 margin at 5x).
        """
        lev = int(lev)
        # 1) Hummingbot API first — returns verified success/failure
        try:
            import urllib.request
            import json as _json
            data = _json.dumps({"trading_pair": pair, "leverage": lev}).encode()
            req = urllib.request.Request(
                f"http://localhost:8000/trading/master_account/{self.config.connector_name}/leverage",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Basic YWRtaW46YWRtaW4="},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode()
                if "success" in body.lower() or resp.status == 200:
                    # Also nudge connector if available (async; best-effort)
                    try:
                        mdp = getattr(self, "market_data_provider", None)
                        connectors = getattr(mdp, "connectors", None) or {}
                        conn = connectors.get(self.config.connector_name)
                        if conn is not None and hasattr(conn, "set_leverage"):
                            conn.set_leverage(pair, lev)
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        # 2) Connector-only fallback
        try:
            mdp = getattr(self, "market_data_provider", None)
            connectors = getattr(mdp, "connectors", None) or {}
            conn = connectors.get(self.config.connector_name)
            if conn is not None and hasattr(conn, "set_leverage"):
                conn.set_leverage(pair, lev)
                return True
        except Exception:
            pass
        return False

    def _position_amount(self, pair: str, price) -> Decimal:
        """Size position so MARGIN ≈ position_size_quote (default $10).

        amount = margin * leverage / price
        notional = margin * leverage
        """
        margin = Decimal(str(self.config.position_size_quote))
        lev = Decimal(str(self._lev(pair)))
        px = Decimal(str(price))
        if px <= 0:
            raise ValueError("price must be > 0")
        # Hard cap: never size above margin * lev (exactly the budget)
        amount = (margin * lev) / px
        return amount

    def get_candles_config(self) -> List[CandlesConfig]:
        out: List[CandlesConfig] = []
        for p in self.config.trading_pairs:
            out.append(CandlesConfig(
                connector=self.config.connector_name, trading_pair=p, interval="15m", max_records=100
            ))
            out.append(CandlesConfig(
                connector=self.config.connector_name, trading_pair=p, interval="1h", max_records=60
            ))
        return out

    def _refresh_enabled(self):
        now = time.time()
        if now - self._enabled_ts < 2:
            return
        self._enabled = load_enabled_strategies()
        self._enabled_ts = now

    def _lev(self, pair: str) -> int:
        return int(self.config.leverage_map.get(pair) or self.config.leverage_default or 5)

    def _e2_claimed_pairs(self) -> Set[str]:
        """Engine 2 REST fills that are STILL on Bitget — never occupy E1 slots."""
        out: Set[str] = set()
        path = next((p for p in _E2_FILL_PATHS if p.is_file()), None)
        if path is None:
            return out
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if not d.get("ok"):
                    continue
                pair = d.get("pair") or d.get("trading_pair")
                if pair:
                    out.add(str(pair))
        except Exception:
            return out
        live: Set[str] = set()
        try:
            mdp = getattr(self, "market_data_provider", None)
            connectors = getattr(mdp, "connectors", None) or {}
            conn = connectors.get(self.config.connector_name)
            acc = getattr(conn, "account_positions", None) or {}
            for pos in acc.values() if hasattr(acc, "values") else []:
                amt = float(getattr(pos, "amount", 0) or 0)
                tp = getattr(pos, "trading_pair", None)
                if tp and abs(amt) > 1e-12:
                    live.add(str(tp))
        except Exception:
            live = set()
        if live:
            return out & live
        return set()

    def _active_pairs(self) -> Set[str]:
        """Pairs that currently occupy an Engine 1 slot.

        Live scalp executors + in-universe exchange inventory (keep_position
        leftover inventory). Engine 4 and Engine 2 pairs never occupy Engine 1 slots.
        """
        active = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active,
        )
        pairs: Set[str] = set()
        for e in active:
            tp = getattr(e, "trading_pair", None)
            if not tp and getattr(e, "config", None) is not None:
                tp = getattr(e.config, "trading_pair", None)
            if tp:
                pairs.add(str(tp))
        held: Set[str] = set()
        for rec in getattr(self, "positions_held", []) or []:
            tp = getattr(rec, "trading_pair", None)
            if tp:
                held.add(str(tp))
        live_ex: Set[str] = set()
        try:
            mdp = getattr(self, "market_data_provider", None)
            connectors = getattr(mdp, "connectors", None) or {}
            conn = connectors.get(self.config.connector_name)
            acc = getattr(conn, "account_positions", None) or {}
            for pos in acc.values() if hasattr(acc, "values") else []:
                amt = float(getattr(pos, "amount", 0) or 0)
                tp = getattr(pos, "trading_pair", None)
                if tp and abs(amt) > 1e-12:
                    live_ex.add(str(tp))
        except Exception:
            pass
        try:
            import json as _json
            import urllib.request
            payload = _json.dumps({
                "account_names": ["master_account"],
                "connector_names": [self.config.connector_name],
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8000/trading/positions",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Basic YWRtaW46YWRtaW4="},
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                body = _json.loads(resp.read().decode())
            for p in body.get("data") or []:
                tp = p.get("trading_pair")
                amt = float(p.get("amount") or 0)
                if tp and abs(amt) > 1e-12:
                    live_ex.add(str(tp))
        except Exception:
            pass
        # Ghost keep_position rows (TSLA amount 140000 with no Bitget leg) must not eat slots.
        if live_ex:
            pairs |= (held & live_ex)
            pairs |= live_ex
        else:
            pairs |= held
        universe = set(self.config.trading_pairs or [])
        if universe:
            pairs = {p for p in pairs if p in universe}
        pairs -= _E4_PAIRS
        pairs -= _E2_BASKET
        pairs -= self._e2_claimed_pairs()
        return pairs

    def _slots_used(self) -> int:
        return len(self._active_pairs())

    def _closed_df(self, df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Drop the forming candle. All indicators must use the last closed bar."""
        if df is None or len(df) < 2:
            return None
        return df.iloc[:-1]

    def _signals_for_df(self, df: pd.DataFrame, h1: Optional[pd.DataFrame], enabled: Set[str]) -> Tuple[int, Dict[str, int], float]:
        """Return combined signal, votes, score (0..1).

        Rules (live audit Aug 27):
        - Closed 15m/1h candles only (forming-bar flicker was the <15m −$36 bucket).
        - ROC_RSI is momentum + RSI timing, not mean-reversion (old rsi<40 AND roc>0 never fired).
        - Need ≥2 engines in the same direction. Solo SUPER_A at 0.58 is not READY.
        - Longs: H1 EMA12>EMA26. Shorts: H1 EMA12<EMA26 AND close < H1 EMA21.
        """
        d = self._closed_df(df)
        if d is None or len(d) < 32:
            return 0, {}, 0.0
        d = d.copy()
        d["roc"] = d["close"].pct_change(self.config.roc_period) * 100
        d["roc8"] = d["close"].pct_change(self.config.super_a_m15_roc_period) * 100
        delta = d["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / self.config.rsi_period, min_periods=self.config.rsi_period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / self.config.rsi_period, min_periods=self.config.rsi_period, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        d["rsi"] = 100 - 100 / (1 + rs)
        d["bb_mid"] = d["close"].rolling(self.config.bb_period).mean()
        d["bb_std"] = d["close"].rolling(self.config.bb_period).std()
        d["bb_lower"] = d["bb_mid"] - d["bb_std"] * self.config.bb_std
        d["bb_upper"] = d["bb_mid"] + d["bb_std"] * self.config.bb_std
        d["vol_avg"] = d["volume"].rolling(self.config.bb_period).mean()
        d["vol_ratio"] = d["volume"] / d["vol_avg"]
        d["ema21"] = d["close"].ewm(span=21, adjust=False).mean()
        latest = d.iloc[-1]
        votes: Dict[str, int] = {}
        roc_min = float(self.config.roc_min or 0.2)

        if "ROC_RSI" in enabled:
            # Momentum direction + RSI timing (70/30). Inner band rejects
            # oversold dumps / overbought chases that SUPER_A still allows.
            roc = float(latest["roc"] or 0)
            rsi = float(latest["rsi"] or 50)
            if roc > roc_min and 40.0 <= rsi < 70.0:
                votes["ROC_RSI"] = 1
            elif roc < -roc_min and 30.0 < rsi <= 60.0:
                votes["ROC_RSI"] = -1
            else:
                votes["ROC_RSI"] = 0

        # BB_VOL is a volume FILTER on momentum engines, not a fade voter.
        vol_ok = True
        if "BB_VOL" in enabled:
            vr = float(latest["vol_ratio"] or 0)
            vol_ok = vr >= float(self.config.vol_ratio_min or 0)

        h = self._closed_df(h1)
        if "SUPER_A" in enabled:
            sig = 0
            if h is not None and len(h) >= self.config.super_a_h1_roc_period + 2:
                hh = h.copy()
                hh["roc_h1"] = hh["close"].pct_change(self.config.super_a_h1_roc_period) * 100
                roc_m = float(latest["roc8"] or 0)
                roc_h = float(hh.iloc[-1]["roc_h1"] or 0)
                rsi = float(latest["rsi"] or 50)
                ema = float(latest["ema21"])
                close = float(latest["close"])
                agree = (roc_m > 0 and roc_h > 0) or (roc_m < 0 and roc_h < 0)
                comb = abs(roc_m) + abs(roc_h)
                if agree and comb >= self.config.super_a_min_combined_roc:
                    if roc_m > 0 and rsi < 70 and close > ema:
                        sig = 1
                    elif roc_m < 0 and rsi > 30 and close < ema:
                        sig = -1
            votes["SUPER_A"] = sig

        if not vol_ok:
            for k in list(votes):
                votes[k] = 0

        vals = [v for v in votes.values() if v != 0]
        # Two engines must agree. One vote (even a strong SUPER_A) is not a trade.
        if len(vals) < 2:
            signal = 0
        elif all(v > 0 for v in vals):
            signal = 1
        elif all(v < 0 for v in vals):
            signal = -1
        else:
            signal = 0

        # Longs with H1 trend. Shorts need a clearer bear (12/26 + close < EMA21).
        if signal != 0 and h is not None and len(h) >= 30:
            try:
                h1_close = h["close"]
                h1_fast = float(h1_close.ewm(span=12, adjust=False).mean().iloc[-1])
                h1_slow = float(h1_close.ewm(span=26, adjust=False).mean().iloc[-1])
                h1_ema21 = float(h1_close.ewm(span=21, adjust=False).mean().iloc[-1])
                h1_last = float(h1_close.iloc[-1])
                h1_bullish = h1_fast > h1_slow
                h1_bearish_strong = (h1_fast < h1_slow) and (h1_last < h1_ema21)
                if signal > 0 and not h1_bullish:
                    signal = 0
                elif signal < 0 and not h1_bearish_strong:
                    signal = 0
            except Exception:
                pass

        if signal == 0:
            score = 0.0
        else:
            agree_n = sum(1 for v in votes.values() if v == signal)
            total = max(len(votes), 1)
            score = agree_n / total
            score = min(1.0, score + min(abs(float(latest.get("roc") or 0)) / 2.0, 0.25))
        return signal, votes, float(score)

    async def update_processed_data(self):
        self._refresh_enabled()
        enabled = set(self._enabled)
        candidates = []
        if not enabled:
            self.processed_data = {
                "candidates": [],
                "open_slots": self.config.max_open_positions - self._slots_used(),
                "enabled": [],
                "universe": self.config.trading_pairs,
                "leverage_map": self.config.leverage_map,
                "active_pairs": sorted(self._active_pairs()),
            }
            self._dump_signals()
            return

        active = self._active_pairs()
        self.processed_data = {
            "candidates": [],
            "open_slots": max(0, self.config.max_open_positions - len(active)),
            "enabled": sorted(enabled),
            "universe": self.config.trading_pairs,
            "leverage_map": self.config.leverage_map,
            "active_pairs": sorted(active),
            "max_open_positions": self.config.max_open_positions,
        }
        self._dump_signals()
        for pair in self.config.trading_pairs:
            try:
                m15 = self.market_data_provider.get_candles_df(
                    self.config.connector_name, pair, "15m", 100
                )
                h1 = self.market_data_provider.get_candles_df(
                    self.config.connector_name, pair, "1h", 60
                )
                signal, votes, score = self._signals_for_df(m15, h1, enabled)
                if signal == 0:
                    continue
                thr = float(getattr(self.config, "score_threshold", 0.55) or 0.55)
                # Tag which engines voted with the final direction (for journal)
                agreeing = sorted([k for k, v in votes.items() if v == signal and v != 0])
                candidates.append({
                    "symbol": pair,
                    "signal": signal,
                    "direction": "LONG" if signal > 0 else "SHORT",
                    "score": round(score, 3),
                    "votes": votes,
                    "strategies": agreeing,
                    "strategy_tag": "+".join(agreeing) if agreeing else "NONE",
                    "leverage": self._lev(pair),
                    "has_position": pair in active,
                    "ready": score >= thr and signal != 0,
                })
            except Exception:
                continue

        candidates.sort(key=lambda x: (not x["has_position"], x["score"]), reverse=True)
        free = max(0, self.config.max_open_positions - len(active))
        self.processed_data = {
            "candidates": candidates,
            "open_slots": free,
            "enabled": sorted(enabled),
            "universe": self.config.trading_pairs,
            "leverage_map": self.config.leverage_map,
            "active_pairs": sorted(active),
            "max_open_positions": self.config.max_open_positions,
        }
        self._dump_signals()

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        actions.extend(self.create_actions_proposal())
        return actions

    def _dump_signals(self) -> None:
        """Write bot-authoritative signals for the dashboard (cli.engine has no format_status)."""
        try:
            d = self.processed_data or {}
            used = len(d.get("active_pairs") or [])
            mx = int(d.get("max_open_positions") or getattr(self.config, "max_open_positions", 3) or 3)
            try:
                fs = "\n".join(self.to_format_status())
            except Exception:
                fs = ""
            payload = {
                "ts": time.time(),
                "available": True,
                "source": "controller",
                "slots": {
                    "used": used,
                    "max": mx,
                    "free": max(0, mx - used),
                    "over": used > mx,
                },
                "enabled": list(d.get("enabled") or []),
                "open_pairs": list(d.get("active_pairs") or []),
                "score_threshold": float(getattr(self.config, "score_threshold", 0.66) or 0.66),
                "candidates": list(d.get("candidates") or []),
                "format_status": fs,
            }
            raw = json.dumps(payload)
            for p in (
                Path("/home/hummingbot/data/e1_signals.json"),
                Path(__file__).resolve().parents[2] / "data" / "e1_signals.json",
            ):
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_suffix(".json.tmp")
                    tmp.write_text(raw, encoding="utf-8")
                    tmp.replace(p)
                except Exception:
                    try:
                        p.write_text(raw, encoding="utf-8")
                    except Exception:
                        continue
        except Exception:
            return

    def _barrier_for(self, pair: str) -> TripleBarrierConfig:
        """Stocks get a wider trail so 15m crypto geometry does not scalp pennies."""
        base = self.config.triple_barrier_config
        if pair not in _STOCK_PAIRS:
            return base
        return TripleBarrierConfig(
            stop_loss=self.config.stop_loss,
            take_profit=self.config.take_profit,
            time_limit=self.config.time_limit,
            trailing_stop=TrailingStop(
                activation_price=Decimal("0.008"),
                trailing_delta=Decimal("0.005"),
            ),
            open_order_type=OrderType.MARKET,
            take_profit_order_type=OrderType.LIMIT,
            stop_loss_order_type=OrderType.MARKET,
            time_limit_order_type=OrderType.MARKET,
        )

    def _effective_cooldown(self, pair: str) -> float:
        """Adaptive cooldown: 25m normal, 50m after STOP_LOSS on this pair."""
        base = float(getattr(self.config, "cooldown_time", 1500) or 1500)
        if self._last_close_was_sl(pair):
            return base * 2.0
        return base

    def _executor_pair(self, e) -> str:
        tp = getattr(e, "trading_pair", None)
        if not tp:
            cfg = getattr(e, "config", None)
            if cfg is not None:
                tp = getattr(cfg, "trading_pair", None)
                if not tp and isinstance(cfg, dict):
                    tp = cfg.get("trading_pair")
        return str(tp or "")

    def _closed_executors_for(self, pair: str) -> list:
        try:
            return list(self.filter_executors(
                executors=self.executors_info,
                filter_func=lambda e: (
                    self._executor_pair(e) == pair
                    and not getattr(e, "is_active", True)
                ),
            ) or [])
        except Exception:
            return []

    def _last_close_ts(self, pair: str) -> float:
        ts = 0.0
        for e in self._closed_executors_for(pair):
            try:
                ts = max(ts, float(getattr(e, "close_timestamp", 0) or 0))
            except Exception:
                continue
        return ts

    def _last_close_was_sl(self, pair: str) -> bool:
        """True if this pair's most recent closed executor exited via STOP_LOSS."""
        closed = self._closed_executors_for(pair)
        if not closed:
            return False
        closed.sort(key=lambda e: float(getattr(e, "close_timestamp", 0) or 0), reverse=True)
        cc = getattr(closed[0], "close_type", None)
        if cc is None:
            cc = getattr(getattr(closed[0], "config", None), "close_type", None)
        return "STOP_LOSS" in str(getattr(cc, "name", cc))

    def create_actions_proposal(self) -> List[ExecutorAction]:
        out: List[ExecutorAction] = []
        data = self.processed_data or {}
        candidates = data.get("candidates") or []
        active = set(data.get("active_pairs") or self._active_pairs())
        free = int(data.get("open_slots") or 0)
        if free <= 0:
            return out
        now = self.market_data_provider.time()
        opened = 0
        e2_block = self._e2_claimed_pairs()
        for c in candidates:
            if opened >= free:
                break
            pair = c["symbol"]
            if pair in active or pair in e2_block or pair in _E4_PAIRS or pair in _E2_BASKET or not c.get("ready"):
                continue
            # Cooldown from last CLOSE (or last entry if never closed). SL → 50m.
            gate_ts = max(float(self._last_entry_ts.get(pair, 0) or 0), self._last_close_ts(pair))
            if now - gate_ts < self._effective_cooldown(pair):
                continue
            try:
                price = self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.MidPrice
                )
                if price is None or price <= 0:
                    continue
                lev = self._lev(pair)
                # Ensure exchange leverage matches config BEFORE sizing/opening.
                # If exchange is at 5x but we size for 10x, margin doubles ($20 not $10).
                if not self._ensure_leverage(pair, lev):
                    try:
                        self.logger().warning(f"Could not set leverage {lev}x on {pair} — skip entry")
                    except Exception:
                        pass
                    continue
                # position_size_quote = MARGIN per trade ($10). notional = margin × lev
                amount = self._position_amount(pair, price)
                # Safety: reject if computed notional would need > $10.50 margin at this lev
                notional = amount * Decimal(str(price))
                margin_est = notional / Decimal(str(lev)) if lev else notional
                max_margin = Decimal(str(self.config.position_size_quote)) * Decimal("1.05")
                if margin_est > max_margin:
                    try:
                        self.logger().warning(
                            f"Skip {pair}: margin_est={margin_est:.2f} > max {max_margin} "
                            f"(amt={amount} px={price} lev={lev})"
                        )
                    except Exception:
                        pass
                    continue
                side = TradeType.BUY if c["signal"] > 0 else TradeType.SELL
                # Persist strategy attribution on the executor via level_id
                # Format: STRAT1+STRAT2|score|DIR  e.g. SUPER_A+ROC_RSI|0.58|LONG
                strat_tag = str(c.get("strategy_tag") or "SCALP")
                score_s = f"{float(c.get('score') or 0):.2f}"
                dir_s = "L" if c["signal"] > 0 else "S"
                level_id = f"{strat_tag}|{score_s}|{dir_s}"[:64]
                try:
                    self.logger().info(
                        f"ENTRY {pair} {dir_s} lev={lev}x score={score_s} "
                        f"strats={strat_tag} votes={c.get('votes')}"
                    )
                except Exception:
                    pass
                out.append(CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=PositionExecutorConfig(
                        timestamp=now,
                        connector_name=self.config.connector_name,
                        trading_pair=pair,
                        side=side,
                        entry_price=price,
                        amount=amount,
                        triple_barrier_config=self._barrier_for(pair),
                        leverage=lev,
                        level_id=level_id,
                    ),
                ))
                self._last_entry_ts[pair] = now
                active.add(pair)
                opened += 1
            except Exception:
                continue
        return out

    def to_format_status(self) -> List[str]:
        d = self.processed_data or {}
        thr = float(getattr(self.config, "score_threshold", 0.55) or 0.55)
        lines = [
            "═══ v3.7 SCALP MULTI ═══",
            f"Universe: {len(d.get('universe') or [])} pairs · slots {len(d.get('active_pairs') or [])}/{d.get('max_open_positions', 3)}",
            f"Strategies ON: {', '.join(d.get('enabled') or []) or 'NONE'} · thr≥{thr:.2f} · 2 engines · closed 15m",
            f"Open: {', '.join(d.get('active_pairs') or []) or '—'}",
            "Top candidates:",
        ]
        for c in (d.get("candidates") or [])[:8]:
            ready = " READY" if c.get("ready") else ""
            lines.append(
                f"  {c['symbol']:12} {c['direction']:5} score={c['score']:.2f} "
                f"lev={c['leverage']}x {c.get('strategy_tag') or c.get('votes')}"
                f"{' [OPEN]' if c.get('has_position') else ''}{ready}"
            )
        if not d.get("candidates"):
            lines.append("  (no ready signals)")
        return lines
