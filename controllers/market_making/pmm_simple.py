from decimal import Decimal
from typing import List, Optional, Tuple

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType


class PMMSimpleConfig(MarketMakingControllerConfigBase):
    controller_name: str = "pmm_simple"

    # Inventory-priced quote (mini Avellaneda–Stoikov) knobs.
    # Skew amount = alpha * half_spread * (inventory_quote / cap_quote), capped.
    # 0 alpha or missing keys = legacy fixed-spread behaviour.
    skew_alpha: float = 0.6
    skew_deadband_quote: float = 10.0
    skew_max_frac_of_half_spread: float = 0.9
    # Skip new quotes (and pull unfilled) if last closed 1m range >= this.
    crash_halt_pct: float = 0.006


class PMMSimpleController(MarketMakingControllerBase):
    """Fixed-spread PMM with inventory one-siding (bleed control).

    When short: buy-only (cover) — never add to the short.
    When long: sell-only (distribute) — never add to the long.
    When flat: two-sided quotes, still hard-capped at 90% of total_amount_quote.
    """

    def __init__(self, config: PMMSimpleConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._lev_set = False

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        tbc = self.config.triple_barrier_config
        inv = self._net_inventory_quote()
        band = self._inventory_deadband_quote()
        # User spec: SL/TIME from YAML (PositionExecutor closes those via MARKET).
        # While skewed, drop TIME so the timer does not MARKET-dump inventory;
        # one-siding + LIMIT TP (+ SL) still manage the book.
        if abs(inv) >= band:
            tbc = TripleBarrierConfig(
                stop_loss=tbc.stop_loss,
                take_profit=tbc.take_profit,
                time_limit=None,
                trailing_stop=getattr(tbc, "trailing_stop", None),
                open_order_type=OrderType.LIMIT,
                take_profit_order_type=OrderType.LIMIT,
                stop_loss_order_type=OrderType.MARKET,
                time_limit_order_type=OrderType.MARKET,
            )
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=tbc,
            leverage=self.config.leverage,
            side=trade_type,
        )

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """Cancel/refresh first, then place.

        Base queues create before stop. Combined with is_active==False as soon as
        an executor is SHUTTING_DOWN, the next tick places a new LIMIT while the
        old 15s quote is still on the book (dual bids / dual asks).
        """
        actions: List[ExecutorAction] = []
        actions.extend(self.stop_actions_proposal())
        actions.extend(self.create_actions_proposal())
        return actions

    def _ensure_leverage(self) -> None:
        if self._lev_set:
            return
        lev = int(self.config.leverage or 10)
        pair = self.config.trading_pair
        try:
            mdp = getattr(self, "market_data_provider", None)
            connectors = getattr(mdp, "connectors", None) or {}
            conn = connectors.get(self.config.connector_name)
            if conn is not None and hasattr(conn, "set_leverage"):
                conn.set_leverage(pair, lev)
                self._lev_set = True
        except Exception:
            pass

    def _net_inventory_quote(self) -> Decimal:
        """Signed inventory in quote. Long > 0, short < 0.

        Bitget / Hummingbot often store amount as abs() with position_side set.
        Treat SHORT/SELL as negative so one-siding actually fires.
        """
        pair = self.config.trading_pair
        mid = Decimal("0")
        try:
            mid = Decimal(str((self.processed_data or {}).get("reference_price") or 0))
        except Exception:
            mid = Decimal("0")
        try:
            mdp = getattr(self, "market_data_provider", None)
            connectors = getattr(mdp, "connectors", None) or {}
            conn = connectors.get(self.config.connector_name)
            acc = getattr(conn, "account_positions", None) or {}
            for pos in (acc.values() if hasattr(acc, "values") else []):
                tp = str(getattr(pos, "trading_pair", "") or "")
                if tp != pair:
                    continue
                raw = Decimal(str(getattr(pos, "amount", 0) or 0))
                if raw == 0:
                    continue
                px = Decimal(str(getattr(pos, "entry_price", 0) or 0)) or mid
                if px <= 0:
                    continue
                side = str(
                    getattr(pos, "position_side", None)
                    or getattr(pos, "side", None)
                    or ""
                ).upper()
                if raw < 0 or "SHORT" in side or side.endswith("SELL") or side == "SELL":
                    signed = -abs(raw)
                else:
                    signed = abs(raw)
                return signed * px
        except Exception:
            pass
        return Decimal("0")

    def _inventory_deadband_quote(self) -> Decimal:
        """Ignore dust below ~5% of inventory cap (or $5)."""
        cap = Decimal(str(self.config.total_amount_quote or 100))
        return max(cap * Decimal("0.05"), Decimal("5"))

    def get_candles_config(self) -> List[CandlesConfig]:
        return [CandlesConfig(
            connector=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            interval="1m",
            max_records=8,
        )]

    def _crash_halt(self) -> bool:
        """True if last *closed* 1m bar moved >= crash_halt_pct. Skip forming candle."""
        pct = float(getattr(self.config, "crash_halt_pct", 0) or 0)
        if pct <= 0:
            return False
        try:
            df = self.market_data_provider.get_candles_df(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                interval="1m",
                max_records=8,
            )
            if df is None or len(df) < 3:
                return False
            row = df.iloc[-2]
            o = float(row["open"])
            if o <= 0:
                return False
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            move = max(abs(c - o), h - l) / o
            return move >= pct
        except Exception:
            return False

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        """Pull wrong-side quotes immediately when inventory is skewed."""
        inv = self._net_inventory_quote()
        band = self._inventory_deadband_quote()
        if self._crash_halt():
            def _unfilled(x) -> bool:
                if not x.is_active:
                    return False
                try:
                    filled = float(getattr(x, "filled_amount_quote", 0) or 0)
                except (TypeError, ValueError):
                    filled = 0.0
                return filled <= 0.5
            bad = self.filter_executors(executors=self.executors_info, filter_func=_unfilled)
            return [
                StopExecutorAction(controller_id=self.config.id, executor_id=e.id)
                for e in bad
            ]
        block_prefix = None
        if inv <= -band:
            # Short: cancel sells (would add to short)
            block_prefix = "sell"
        elif inv >= band:
            # Long: cancel buys (would add to long)
            block_prefix = "buy"
        if not block_prefix:
            return []

        def _wrong_side(x) -> bool:
            if not x.is_active:
                return False
            # Filled cover still has buy_/sell_ level_id. Do not EARLY_STOP it
            # or the LIMIT TP never prints.
            try:
                filled = float(getattr(x, "filled_amount_quote", 0) or 0)
            except (TypeError, ValueError):
                filled = 0.0
            if filled > 0.5:
                return False
            info = getattr(x, "custom_info", None) or {}
            lid = info.get("level_id") if isinstance(info, dict) else None
            if not lid:
                # fallback: executor config side
                try:
                    side = str(getattr(getattr(x, "config", None), "side", "") or "").upper()
                    if block_prefix == "sell":
                        return "SELL" in side
                    if block_prefix == "buy":
                        return "BUY" in side
                    return False
                except Exception:
                    return False
            return str(lid).startswith(block_prefix)

        bad = self.filter_executors(
            executors=self.executors_info,
            filter_func=_wrong_side,
        )
        out = [
            StopExecutorAction(controller_id=self.config.id, executor_id=e.id)
            for e in bad
        ]
        # Do NOT early-stop filled cover executors. That paid ~$0.04 fees every
        # refresh and never let the LIMIT TP print (ETH 79/79 EARLY_STOP after
        # the 20:15 tune). Wrong-side quotes still get pulled above.
        return out

    def get_levels_to_execute(self) -> List[str]:
        """Keep a level occupied until the executor is fully terminated.

        Inventory one-side (bleed control):
          short → buy only (cover)
          long  → sell only (distribute)
          flat  → both sides

        Hard cap: still block the side that would push |inv| past 90% of
        total_amount_quote.
        """
        self._ensure_leverage()
        if self._crash_halt():
            return []
        now = self.market_data_provider.time()
        cooldown = self.config.cooldown_time

        def _holds_level(x) -> bool:
            if x.is_active:
                return True
            if getattr(x, "status", None) == RunnableStatus.SHUTTING_DOWN:
                return True
            if x.close_type == CloseType.STOP_LOSS and x.close_timestamp is not None:
                return (now - x.close_timestamp) < cooldown
            return False

        working_levels = self.filter_executors(
            executors=self.executors_info,
            filter_func=_holds_level,
        )
        working_levels_ids = []
        for executor in working_levels:
            info = getattr(executor, "custom_info", None) or {}
            lid = info.get("level_id") if isinstance(info, dict) else None
            if lid:
                working_levels_ids.append(lid)
        levels = self.get_not_active_levels_ids(working_levels_ids)
        cap = Decimal(str(self.config.total_amount_quote or 100))
        inv = self._net_inventory_quote()
        band = self._inventory_deadband_quote()

        # One-side until flat/skewed back inside deadband
        if inv <= -band:
            levels = [x for x in levels if str(x).startswith("buy")]
        elif inv >= band:
            levels = [x for x in levels if str(x).startswith("sell")]

        # Hard cap still applies when flat or after partial cover
        if inv >= cap * Decimal("0.90"):
            levels = [x for x in levels if not str(x).startswith("buy")]
        if inv <= -cap * Decimal("0.90"):
            levels = [x for x in levels if not str(x).startswith("sell")]
        return levels

    # ---------- Inventory-priced quotes (mini A–S) ----------

    def _skew_shift_frac(self) -> Decimal:
        """How far to shift the *mid* (as fraction of price) given current inventory.

        Positive inventory → shift mid up (encourage selling, discourage buying).
        Negative inventory → shift mid down (encourage covering, discourage adding short).

        magnitude = alpha * half_spread * |inv_quote|/cap_quote, clamped to skew_max_frac_of_half_spread.
        Inside deadband → 0.
        """
        try:
            inv = self._net_inventory_quote()
        except Exception:
            return Decimal("0")
        band = Decimal(str(getattr(self.config, "skew_deadband_quote", 10) or 10))
        if abs(inv) < band:
            return Decimal("0")
        cap = Decimal(str(self.config.total_amount_quote or 100))
        if cap <= 0:
            return Decimal("0")
        # Use the smaller of buy/sell half-spreads as the reference scale.
        try:
            buy_spreads, _ = self.config.get_spreads_and_amounts_in_quote(TradeType.BUY)
            sell_spreads, _ = self.config.get_spreads_and_amounts_in_quote(TradeType.SELL)
            half_ref = min(
                Decimal(str(abs(float(buy_spreads[0])))),
                Decimal(str(abs(float(sell_spreads[0])))),
            )
        except Exception:
            half_ref = Decimal("0.0008")
        alpha = Decimal(str(float(getattr(self.config, "skew_alpha", 0.6) or 0.0)))
        max_frac = Decimal(str(float(getattr(self.config, "skew_max_frac_of_half_spread", 0.9) or 0.9)))
        inv_frac = (inv / cap)
        raw = alpha * half_ref * inv_frac  # signed
        # clamp magnitude
        cap_mag = half_ref * max_frac
        if raw > cap_mag:
            raw = cap_mag
        elif raw < -cap_mag:
            raw = -cap_mag
        return raw

    def get_price_and_amount(self, level_id: str) -> Tuple[Decimal, Decimal]:
        """Override base: shift the *mid* by inventory before applying the half-spread.

        Result: both bid and ask move together by the skew amount, preserving the
        half-spread width per side.  Combined with the one-side / hard-cap logic
        above, this is a mini Avellaneda–Stoikov reservation price.
        """
        level = self.get_level_from_level_id(level_id)
        trade_type = self.get_trade_type_from_level_id(level_id)
        spreads, amounts_quote = self.config.get_spreads_and_amounts_in_quote(trade_type)
        try:
            mid = Decimal(self.processed_data["reference_price"])
        except Exception:
            mid = Decimal("0")
        spread_in_pct = Decimal(spreads[int(level)])
        side_multiplier = Decimal("-1") if trade_type == TradeType.BUY else Decimal("1")

        # Inventory-priced mid (shift BEFORE applying side spread).
        shift = self._skew_shift_frac()
        shifted_mid = mid * (Decimal("1") + shift)

        order_price = shifted_mid * (Decimal("1") + side_multiplier * spread_in_pct)
        amount = Decimal(amounts_quote[int(level)]) / order_price if order_price > 0 else Decimal("0")
        return order_price, amount
