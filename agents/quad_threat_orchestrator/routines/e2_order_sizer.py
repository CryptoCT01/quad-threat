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


class Config(BaseModel):
    connector_name: str = Field(default="bitget_perpetual")
    pairs: str = Field(default="ALL", description="ALL = the 5-name E2 basket, or a comma list from it")
    margin_quote: float = Field(default=20.0, description="Margin per E2 leg in USDT (not notional)")


class VenueMinimumError(ValueError):
    """The configured budget cannot clear the venue minimum for this pair."""


# ---- pure sizing (no Condor / Hummingbot imports) ------------------------------------
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
    budget = Decimal(str(config.margin_quote)) * Decimal(E2_LEVERAGE)
    rows, lines = [], [f"E2 sizing: ${config.margin_quote} margin x {E2_LEVERAGE}x = ${budget} notional"]
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
            })
        except Exception as err:
            lines.append(f"{pair}: REFUSED - {err}")
    lines.append(
        "Use `amount` verbatim in create_position_executor(connector_name, trading_pair, side=1|2, "
        "amount=<number>, leverage=20, stop_loss=0.005, take_profit=0.004, open_order_type=1, "
        "take_profit_order_type=2, controller_id=<this agent>). If the create returns no executor_id or "
        "an error, NOTHING is open - report it (rule 15)."
    )
    return RoutineResult(
        text="\n".join(lines),
        table_data=rows,
        table_columns=["pair", "amount", "price", "notional", "venue_min", "step", "bumped"],
    )
