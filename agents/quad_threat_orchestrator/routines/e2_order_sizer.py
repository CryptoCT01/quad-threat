"""Engine 2 order sizer: venue-safe amount for create_order_executor. Read-only.

Finals fix (blockers 3 + 4). Turns "$20 margin at 20x" into a base ``amount``
that is quantized to Bitget's size increment FIRST and still clears
min_order_size and the venue minimum notional (max(min_notional_size,
min_order_value) - Bitget perps publish minTradeUSDT as min_order_value) with
a 5% margin, rounding UP. A budget below the venue minimum is refused with a
clear error instead of creating an executor that dies at start. Places nothing.
"""

CATEGORY = "Engine 2"

import logging
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

E2_BASKET = ("XAU-USDT", "CL-USDT", "DOGE-USDT", "NEAR-USDT", "LTC-USDT")
E2_LEVERAGE = 20
VENUE_MIN_SAFETY = Decimal("0.05")
# Finals ATR-style stop: 1.5x the mean 15m candle range, clamped to [0.5%, 1%];
# margin scaled so stop_pct x 20 x margin <= $2.50 per trade.
E2_MAX_MARGIN = Decimal("20")
E2_RISK_CAP_QUOTE = Decimal("2.5")
E2_ATR_MULT = Decimal("1.5")
E2_STOP_MIN = Decimal("0.005")
E2_STOP_MAX = Decimal("0.01")


class Config(BaseModel):
    connector_name: str = Field(default="bitget_perpetual")
    pairs: str = Field(default="ALL", description="ALL = the 5-name E2 basket, or a comma list from it")
    margin_quote: float = Field(default=20.0, description="Max margin per E2 leg in USDT (not notional); scaled down by the stop rule")
    mean_range_15m: float = Field(
        default=0.0,
        description="Mean (high-low)/close of the pair's last ~96 15m candles, as a fraction (0.004 = 0.4%). 0 = unknown -> 1% stop",
    )


class VenueMinimumError(ValueError):
    """The configured budget cannot clear the venue minimum for this pair."""


# ---- pure sizing (no Condor / Hummingbot imports) ------------------------------------
def e2_stop_pct(mean_range_15m: Any) -> Decimal:
    """ATR-style stop: 1.5 x mean 15m range, clamped to [0.5%, 1%]. Unknown (<=0) -> 1%."""
    try:
        r = Decimal(str(mean_range_15m or 0))
    except Exception:
        r = Decimal("0")
    if not r.is_finite() or r <= 0:
        return E2_STOP_MAX
    return min(max(r * E2_ATR_MULT, E2_STOP_MIN), E2_STOP_MAX)


def e2_margin_for_stop(stop_pct: Any, max_margin: Any = E2_MAX_MARGIN,
                       risk_cap: Decimal = E2_RISK_CAP_QUOTE) -> Decimal:
    """Largest margin (<= max_margin, cents rounded down) with stop_pct x 20 x margin <= risk_cap."""
    cap = Decimal(str(max_margin))
    s = Decimal(str(stop_pct))
    if s <= 0:
        return cap
    m = min(cap, risk_cap / (s * Decimal(E2_LEVERAGE)))
    return m.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _rule_dec(rule: Any, name: str) -> Decimal:
    if rule is None:
        return Decimal("0")
    raw = rule.get(name) if isinstance(rule, dict) else getattr(rule, name, None)
    try:
        v = Decimal(str(raw or 0))
    except Exception:
        return Decimal("0")
    return v if v.is_finite() and v > 0 else Decimal("0")


def venue_min_notional(rule: Any) -> Decimal:
    """Largest quote minimum the venue states (min_notional_size / min_order_value)."""
    return max(_rule_dec(rule, "min_notional_size"), _rule_dec(rule, "min_notional"),
               _rule_dec(rule, "min_order_value"))


def venue_safe_amount(
    amount: Decimal,
    price: Decimal,
    rule: Any,
    budget_quote: Optional[Decimal] = None,
    safety: Decimal = VENUE_MIN_SAFETY,
) -> Tuple[Decimal, bool]:
    """Quantize to the increment, then make the ROUNDED order clear the venue minimums.

    Returns ``(amount, bumped)``; raises :class:`VenueMinimumError` when
    ``budget_quote`` is below the venue floor (so we refuse instead of dying).
    """
    amount = Decimal(str(amount))
    price = Decimal(str(price))
    if rule is None or price <= 0 or amount <= 0:
        return amount, False
    step = _rule_dec(rule, "min_base_amount_increment")
    if step < Decimal("1e-12"):
        step = Decimal("0")
    q = (amount / step).to_integral_value(rounding=ROUND_DOWN) * step if step > 0 else amount
    min_size = _rule_dec(rule, "min_order_size")
    min_notional = venue_min_notional(rule)
    venue_floor_quote = max(min_notional, min_size * price)
    if budget_quote is not None and venue_floor_quote > 0 and Decimal(str(budget_quote)) < venue_floor_quote:
        raise VenueMinimumError(
            f"budget {Decimal(str(budget_quote)):.4f} quote is below the venue minimum "
            f"{venue_floor_quote:.4f} quote (min_notional={min_notional}, "
            f"min_order_size={min_size}, price={price})"
        )
    need = min_size
    if min_notional > 0:
        need = max(need, (min_notional * (Decimal("1") + safety)) / price)
    if q >= need and q > 0:
        return q, False
    if step > 0:
        q = (need / step).to_integral_value(rounding=ROUND_UP) * step
        if q <= 0:
            q = step
    else:
        q = need
    return q, True


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(text="No Hummingbot server available")
    from condor.fetchers.market_data import fetch_current_price
    from mcp_servers.hummingbot_api.hummingbot_client import trading_rules_cache

    if config.pairs.upper() == "ALL":
        want = list(E2_BASKET)
    else:
        want = []
        for p in (x.strip().upper() for x in config.pairs.split(",") if x.strip()):
            pair = p if p.endswith("-USDT") else f"{p}-USDT"
            if pair in E2_BASKET:
                want.append(pair)
    stop_pct = e2_stop_pct(config.mean_range_15m)
    margin = e2_margin_for_stop(stop_pct, config.margin_quote)
    budget = margin * Decimal(E2_LEVERAGE)
    risk = stop_pct * budget
    rows, lines = [], [
        f"E2 sizing: stop {stop_pct * 100:.3f}% (1.5x mean 15m range, clamped 0.5-1%), "
        f"${margin} margin x {E2_LEVERAGE}x = ${budget} notional, risk ${risk:.2f} (cap $2.50)"
    ]
    for pair in want:
        try:
            rule = await trading_rules_cache.get(client, config.connector_name, pair)
            price = await fetch_current_price(client, config.connector_name, pair)
            if not price:
                raise ValueError("no price")
            px = Decimal(str(price))
            amount, bumped = venue_safe_amount(budget / px, px, rule, budget_quote=budget)
            rows.append({
                "pair": pair,
                "amount": float(amount),
                "price": float(px),
                "notional": round(float(amount * px), 4),
                "venue_min": float(venue_min_notional(rule)),
                "step": float(_rule_dec(rule, "min_base_amount_increment")),
                "bumped": bumped,
                "stop_pct": float(stop_pct),
                "margin": float(margin),
                "risk_quote": round(float(stop_pct * amount * px), 4),
            })
        except Exception as err:
            lines.append(f"{pair}: REFUSED - {err}")
    lines.append(
        "Use `amount` verbatim in a MARKET create_order_executor OPEN (leverage=20, controller_id=<this agent>), "
        "then place the hard STOP close at `stop_pct` from fill (LONG fill*(1-stop_pct), SHORT fill*(1+stop_pct)). "
        "If the create returns no executor_id or an error, NOTHING is open - report it."
    )
    return RoutineResult(
        text="\n".join(lines),
        table_data=rows,
        table_columns=["pair", "amount", "price", "notional", "venue_min", "step", "bumped", "stop_pct", "margin", "risk_quote"],
    )
