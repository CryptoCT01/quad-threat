"""E2 position board — breakeven, size, live PnL for order-executor management.

Stock Condor path: after an order_executor fills you have a Position.
This routine renders it so the agent (and Condor UI HTML table) can decide
CLOSE / PARTIAL_CLOSE from PnL, not from exchange TP/SL.
Places nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

CATEGORY = "Engine 2"

E2_BASKET = ("XAU-USDT", "CL-USDT", "DOGE-USDT", "NEAR-USDT", "LTC-USDT")


class Config(BaseModel):
    connector_name: str = Field(default="bitget_perpetual")


def _pair(raw: str) -> str:
    s = str(raw or "").upper().replace("/", "-")
    if s.endswith("USDT") and "-" not in s:
        s = s[:-4] + "-USDT"
    return s


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _rows_from_payload(data: Any) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("positions", "data", "perp_positions", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict) and isinstance(v.get("positions"), list):
                return [x for x in v["positions"] if isinstance(x, dict)]
    return []


async def _fetch_positions(client: Any) -> list[dict]:
    errors: list[str] = []
    attempts = [
        ("trading.get_positions", lambda: client.trading.get_positions(connector_name="bitget_perpetual")),
        ("trading.get_positions_unfiltered", lambda: client.trading.get_positions()),
        ("accounts.get_positions", lambda: client.accounts.get_positions(account_name="master_account")),
    ]
    for name, fn in attempts:
        try:
            meth = fn()
            raw = await meth if hasattr(meth, "__await__") else meth
            rows = _rows_from_payload(raw)
            if rows:
                return rows
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.debug("e2_position_board %s failed: %s", name, e)
    if errors:
        logger.info("e2_position_board fetch misses: %s", "; ".join(errors[:4]))
    return []


def _normalize(p: dict) -> dict | None:
    pair = _pair(p.get("trading_pair") or p.get("symbol") or p.get("pair") or "")
    if pair not in E2_BASKET:
        return None
    amt = abs(_f(p.get("amount") or p.get("position") or p.get("total") or p.get("size")))
    if amt < 1e-12:
        return None
    side_raw = str(p.get("position_side") or p.get("side") or p.get("holdSide") or "").lower()
    if "short" in side_raw or side_raw in ("sell", "s"):
        side = "SHORT"
    else:
        side = "LONG"
    entry = _f(
        p.get("breakeven_price")
        or p.get("break_even_price")
        or p.get("entry_price")
        or p.get("openPriceAvg")
        or p.get("avg_price")
    )
    mark = _f(p.get("mark_price") or p.get("markPrice") or p.get("current_price") or p.get("close"))
    pnl = _f(p.get("unrealized_pnl") or p.get("unrealizedPL") or p.get("pnl") or p.get("unrealized_pnl_quote"))
    notional = amt * mark if mark else amt * entry
    pct = 0.0
    if entry > 0 and mark > 0:
        pct = (mark - entry) / entry * 100.0
        if side == "SHORT":
            pct = -pct
    elif notional > 0 and pnl:
        pct = pnl / notional * 100.0
    return {
        "pair": pair,
        "side": side,
        "amount": round(amt, 6),
        "breakeven": round(entry, 6),
        "mark": round(mark, 6),
        "pnl": round(pnl, 4),
        "pnl_pct": round(pct, 3),
        "notional": round(notional, 2),
    }


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(text="No Hummingbot server available")

    raw = await _fetch_positions(client)
    rows = []
    for p in raw:
        n = _normalize(p)
        if n:
            rows.append(n)
    rows.sort(key=lambda r: r["pair"])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"E2 POSITION BOARD — {now}",
        "Order-executor path: OPEN fills into a Position. CLOSE = reduce order (full or partial).",
        "Use breakeven + live PnL. Do not wait for exchange TP/SL. Do not call modify_tpsl.",
        "",
    ]
    if not rows:
        lines.append("No E2-basket positions. 0/3 slots. Eligible to OPEN on 2-factor impulse.")
        return RoutineResult(
            text="\n".join(lines),
            table_data=[],
            table_columns=["pair", "side", "amount", "breakeven", "mark", "pnl", "pnl_pct"],
        )

    for r in rows:
        hint = "HOLD"
        if r["pnl_pct"] <= -0.5:
            hint = "CLOSE stop"
        elif r["pnl_pct"] >= 0.4:
            hint = "CLOSE tp or PARTIAL"
        lines.append(
            f"**{r['pair']}** {r['side']} amt={r['amount']}  be={r['breakeven']}  "
            f"mark={r['mark']}  pnl=${r['pnl']:+.2f} ({r['pnl_pct']:+.2f}%)  → {hint}"
        )
    lines.append("")
    lines.append(f"Open E2 legs: {len(rows)}/3")

    return RoutineResult(
        text="\n".join(lines),
        table_data=rows,
        table_columns=["pair", "side", "amount", "breakeven", "mark", "pnl", "pnl_pct", "notional"],
    )
