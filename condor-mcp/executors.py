"""Quad Threat Engine-2 executor bridge for the CURRENT Condor (FEAT-062 tool layout).

Why this file changed (finals blocker 1)
----------------------------------------
The old ``condor-mcp/executors.py`` was a (truncated) copy of Condor's former
``manage_executors`` mega-tool. Latest Condor removed that tool and everything it
imported (``schemas.ManageExecutorsRequest``, ``format_executor_schema_table``,
``get_flow_stage()``): executors are now created by typed tools
(``create_position_executor`` ...) and read/stopped by ``list_executors`` /
``get_executor`` / ``stop_executor``. The old file no longer imports (and the
committed copy did not even parse), so nothing that relied on it could run.

This module targets the new layout and adds the three guarantees the reviewers
asked for, on top of stock Condor:

* blocker 2 - an entry that does not produce a live executor is a FAILURE, never
  a success: a missing ``executor_id`` (``id=None``), an API error, or an executor
  that terminated at start (``INSUFFICIENT_BALANCE`` / ``FAILED``) is surfaced,
  retried once, and then reported loudly. Nothing is reported as "open" unless
  it is running.
* blocker 3 - a margin budget equal to the venue minimum no longer produces an
  executor that shuts down at once: the size is placed a safety margin above the
  minimum, and a budget BELOW the minimum is refused with a clear error.
* blocker 4 - the amount is quantized to the venue increment FIRST and the
  rounded order must clear ``min_order_size`` and the venue minimum notional
  (``max(min_notional_size, min_order_value)`` - Bitget perps publish
  ``minTradeUSDT`` as ``min_order_value`` and leave ``min_notional_size`` at 0,
  which stock Condor's pre-check does not look at); if short it rounds UP.

Everything goes through Hummingbot executors (``controller_id`` = the agent), so
the E2 book is attributable inside Condor. Numbers are sent as numbers, enums as
ints (side 1/2, order types 1=MARKET 2=LIMIT), no extra fields.

Install: copy this file into a Condor checkout as
``mcp_servers/hummingbot_api/tools/quad_threat_e2.py`` (it must NOT replace
Condor's own ``tools/executors.py``). Then, e.g. from ``run_code``::

    from mcp_servers.hummingbot_api.tools import quad_threat_e2 as e2
    result = await e2.create_e2_position(client, trading_pair="LTC-USDT", side=1)

or keep old prompt shapes working with ``e2.manage_executors(client, action=...)``.
With stock Condor and no copy, the agent gets the same sizing from the
``e2_order_sizer`` routine and places via ``create_position_executor``.
The sizing helpers (``venue_safe_amount``) are pure and import-safe anywhere.
"""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Optional, Tuple

logger = logging.getLogger("hummingbot-mcp")

# ---- Engine 2 policy (unchanged from the live desk) ---------------------------------
CONNECTOR = "bitget_perpetual"
E4_PAIRS = frozenset({"BTC-USDT", "ETH-USDT"})
E2_BASKET = frozenset({"XAU-USDT", "CL-USDT", "DOGE-USDT", "NEAR-USDT", "LTC-USDT"})
E2_LEVERAGE = 20
E2_CONTROLLER_ID = "v37_risk_manager"
E2_DEFAULT_MARGIN_QUOTE = 25.0     # $25 margin per E2 leg on the $800 book
E2_STOP_LOSS = 0.005               # 0.5% safety stop (fraction)
E2_TAKE_PROFIT = 0.004             # 0.4% LIMIT bank (fraction)
ORDER_MARKET, ORDER_LIMIT = 1, 2

VENUE_MIN_SAFETY = Decimal("0.05")  # size >= 105% of the venue minimum notional
MAX_SUBMIT_ATTEMPTS = 2             # one retry, then stop and report (strategy rule 15)


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


def plan_e2_amount(margin_quote: float, leverage: int, price: float, rule: Any) -> dict[str, Any]:
    """Base amount for ``margin_quote`` of margin at ``leverage``, venue-safe."""
    budget = Decimal(str(margin_quote)) * Decimal(int(leverage))
    raw = budget / Decimal(str(price))
    amount, bumped = venue_safe_amount(raw, Decimal(str(price)), rule, budget_quote=budget)
    return {
        "amount": float(amount),          # number, not string (Condor typed tool)
        "bumped": bumped,
        "notional": float(amount * Decimal(str(price))),
        "budget_notional": float(budget),
        "venue_min_notional": float(venue_min_notional(rule)),
        "min_order_size": float(_rule_dec(rule, "min_order_size")),
        "amount_increment": float(_rule_dec(rule, "min_base_amount_increment")),
    }


def _side_int(side: Any) -> int:
    s = str(side).strip().upper()
    if s in ("1", "BUY", "LONG", "TRADETYPE.BUY"):
        return 1
    if s in ("2", "SELL", "SHORT", "TRADETYPE.SELL"):
        return 2
    raise ValueError(f"side must be 1/BUY/LONG or 2/SELL/SHORT, got {side!r}")


def _is_live(result: dict[str, Any]) -> bool:
    """A create only counts when an executor id came back and it did not die at start."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    if not (result.get("executor_id") or result.get("id")):
        return False  # id=None: nothing was placed
    status = str(result.get("status") or "").upper()
    close_type = str(result.get("close_type") or "").upper()
    return not (status == "TERMINATED" and close_type in ("INSUFFICIENT_BALANCE", "FAILED"))


# ---- Condor-bound calls (imports resolved lazily inside a Condor checkout) -----------
async def _rules_and_price(client: Any, pair: str) -> tuple[Any, Optional[float]]:
    from mcp_servers.hummingbot_api.hummingbot_client import trading_rules_cache

    rules = await trading_rules_cache.get(client, CONNECTOR, pair)
    price = None
    try:
        from condor.fetchers.market_data import fetch_current_price

        price = await fetch_current_price(client, CONNECTOR, pair)
    except Exception as exc:  # price outage -> refuse (cannot size safely)
        logger.warning("E2 price for %s unavailable: %s", pair, exc)
    return rules, (float(price) if price else None)


async def create_e2_position(
    client: Any,
    *,
    trading_pair: str,
    side: Any,
    margin_quote: float = E2_DEFAULT_MARGIN_QUOTE,
    stop_loss: float = E2_STOP_LOSS,
    take_profit: float = E2_TAKE_PROFIT,
    controller_id: str = E2_CONTROLLER_ID,
    account_name: Optional[str] = None,
) -> dict[str, Any]:
    """Open one Engine-2 leg as a Hummingbot PositionExecutor via Condor's typed tool."""
    from mcp_servers.hummingbot_api.tools import executor_create

    pair = str(trading_pair or "").upper()
    if pair in E4_PAIRS:
        return {"ok": False, "error": f"{pair} belongs to Engine 4 (BTC/ETH maker); E2 may not open it"}
    if pair not in E2_BASKET:
        return {"ok": False, "error": f"{pair} not in E2 basket {sorted(E2_BASKET)}"}
    side_i = _side_int(side)
    sl = min(max(float(stop_loss), 0.003), 0.05)

    rules, price = await _rules_and_price(client, pair)
    if not price:
        return {"ok": False, "error": f"no price for {pair}; refusing to size blind"}
    try:
        plan = plan_e2_amount(margin_quote, E2_LEVERAGE, price, rules)
    except VenueMinimumError as err:
        return {"ok": False, "error": f"{pair}: {err}. Nothing was created."}

    attempts: list[dict[str, Any]] = []
    for attempt in range(1, MAX_SUBMIT_ATTEMPTS + 1):
        result = await executor_create.create_position_executor(
            client,
            connector_name=CONNECTOR,
            trading_pair=pair,
            side=side_i,
            amount=plan["amount"],
            leverage=E2_LEVERAGE,
            stop_loss=sl,
            take_profit=float(take_profit),
            open_order_type=ORDER_MARKET,
            take_profit_order_type=ORDER_LIMIT,
            stop_loss_order_type=ORDER_MARKET,
            time_limit_order_type=ORDER_MARKET,
            account_name=account_name,
            controller_id=controller_id,
        )
        attempts.append(result if isinstance(result, dict) else {"raw": result})
        if _is_live(result):
            return {"ok": True, "attempts": attempt, "plan": plan,
                    "executor_id": result.get("executor_id") or result.get("id"), "result": result}
        err = (result or {}).get("error") if isinstance(result, dict) else None
        logger.error("E2 create %s side=%s attempt %d/%d not live: %s",
                     pair, side_i, attempt, MAX_SUBMIT_ATTEMPTS,
                     err or f"executor_id={result.get('executor_id') if isinstance(result, dict) else None}")
        # A venue/balance refusal will not change on an immediate retry.
        if err and ("minimum" in err or "Insufficient balance" in err):
            break
    last = attempts[-1] if attempts else {}
    return {
        "ok": False,
        "plan": plan,
        "attempts": len(attempts),
        "error": (last.get("error") if isinstance(last, dict) else None)
        or "executor was not created (executor_id=None) or terminated at start - NO position is open",
        "last_result": last,
    }


async def manage_executors(client: Any, action: str = "list", **kw: Any) -> dict[str, Any]:
    """Back-compat shim: old ``manage_executors(action=...)`` calls on the new tool layout."""
    from mcp_servers.hummingbot_api.tools import executors as executor_tools

    action = (action or "list").lower()
    if action == "create":
        cfg = dict(kw.get("executor_config") or {})
        return await create_e2_position(
            client,
            trading_pair=cfg.get("trading_pair") or kw.get("trading_pair"),
            side=cfg.get("side", kw.get("side", 1)),
            margin_quote=float(cfg.get("margin_usd") or kw.get("margin_quote") or E2_DEFAULT_MARGIN_QUOTE),
            stop_loss=float((cfg.get("triple_barrier_config") or {}).get("stop_loss") or cfg.get("stop_loss") or E2_STOP_LOSS),
            controller_id=kw.get("controller_id") or E2_CONTROLLER_ID,
            account_name=kw.get("account_name"),
        )
    if action == "stop":
        if not kw.get("executor_id"):
            return {"ok": False, "error": "stop needs executor_id (use list_executors to find it)"}
        return await executor_tools.stop_executor(
            client, executor_id=kw["executor_id"], keep_position=bool(kw.get("keep_position", False)))
    if action in ("get", "detail"):
        return await executor_tools.get_executor(client, executor_id=kw["executor_id"])
    if action in ("list", "search"):
        return await executor_tools.list_executors(
            client,
            connector_names=[CONNECTOR],
            trading_pairs=kw.get("trading_pairs"),
            controller_ids=kw.get("controller_ids") or [E2_CONTROLLER_ID],
            status=kw.get("status"),
        )
    if action in ("modify_tpsl", "modify-tpsl"):
        return {
            "ok": False,
            "error": "modify_tpsl is not a Condor tool. Executor barriers are fixed at create; "
                     "use stop_executor for profit protection / invalidation (strategy rules a, d).",
        }
    return {"ok": False, "error": f"unknown action {action!r}"}
