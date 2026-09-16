"""Engine 2 live open via Bitget REST inside the hummingbot container.

Never print keys. stdin JSON:
  trading_pair, side (1=buy/long 2=sell/short), leverage, margin_usd,
  stop_loss (frac), take_profit (frac), controller_id, dry_run (bool)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.security import Security

E4 = {"BTC-USDT", "ETH-USDT"}
E2_BASKET = {"XAU-USDT", "CL-USDT", "DOGE-USDT", "NEAR-USDT", "LTC-USDT"}
LEV_CAP = {p: 20 for p in E2_BASKET}
MARGIN_CAP = 10.0
MAX_E2_SLOTS = 3
FILLS_PATH = "/home/hummingbot/data/e2_fills.jsonl"


def _keys():
    sm = ETHKeyFileSecretManger(os.environ.get("HBOT_PASSWORD", "CHANGE_ME"))
    if not Security.login(sm):
        raise SystemExit(json.dumps({"ok": False, "error": "security_login_failed"}))
    keys = Security.api_keys("bitget_perpetual") or {}
    api_key = keys.get("bitget_perpetual_api_key")
    secret = keys.get("bitget_perpetual_secret_key")
    passphrase = keys.get("bitget_perpetual_passphrase")
    if not (api_key and secret and passphrase):
        raise SystemExit(json.dumps({"ok": False, "error": "missing_keys"}))
    return {"key": api_key, "secret": secret, "pass": passphrase}


def _bitget(keys, method: str, path: str, body: dict | None = None) -> dict:
    ts = str(int(time.time() * 1000))
    payload = json.dumps(body) if body is not None else ""
    msg = ts + method + path + payload
    sign = base64.b64encode(
        hmac.new(keys["secret"].encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    req = urllib.request.Request(
        "https://api.bitget.com" + path,
        data=payload.encode() if body is not None else None,
        method=method,
        headers={
            "ACCESS-KEY": keys["key"],
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": keys["pass"],
            "Content-Type": "application/json",
            "locale": "en-US",
        },
    )
    try:
        raw = urllib.request.urlopen(req, timeout=12).read()
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"code": str(e.code), "msg": str(e)}
        return err


def _sym(pair: str) -> str:
    return pair.replace("-", "")


def _round_size(n: float, volume_place: int, min_trade: float) -> float:
    q = 10 ** volume_place
    out = max(min_trade, int(n * q) / q)
    return out


def _hold_side_oneway(hold: str) -> str:
    """Bitget one-way TPSL holdSide: buy = long, sell = short."""
    h = (hold or "").lower()
    if h in ("sell", "short"):
        return "sell"
    return "buy"


def _price_place(keys, symbol: str) -> int:
    contracts = _bitget(
        keys,
        "GET",
        f"/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol={symbol}",
    )
    crow = next((c for c in (contracts.get("data") or []) if c.get("symbol") == symbol), {}) or {}
    try:
        return int(crow.get("pricePlace") or 2)
    except (TypeError, ValueError):
        return 2


def _pending_tpsl(keys, symbol: str) -> list:
    """Pending TP/SL plans. planType=profit_loss covers profit/loss/pos_*."""
    d = _bitget(
        keys,
        "GET",
        f"/api/v2/mix/order/orders-plan-pending?productType=USDT-FUTURES&planType=profit_loss&symbol={symbol}",
    )
    data = d.get("data") or {}
    if isinstance(data, dict):
        return list(
            data.get("entrustedList")
            or data.get("entrustedPlanList")
            or data.get("orderList")
            or []
        )
    if isinstance(data, list):
        return data
    return []


def attach_tpsl(
    keys,
    symbol: str,
    hold: str,
    size: str,
    sl_px: float,
    tp_px: float | None = None,
    attach_tp: bool = False,
    attach_sl: bool = True,
) -> list:
    """Attach exchange safety SL (0.5%) and/or mechanical bank TP (0.4%)."""
    hs = _hold_side_oneway(hold)
    out = []

    def _ok(r: dict) -> bool:
        return str((r or {}).get("code") or "") in ("00000", "0")

    # 1) Position-level plans. Skip a side that already exists on the exchange.
    got_sl = not attach_sl
    got_tp = not attach_tp
    plans = []
    if attach_sl:
        plans.append(("pos_loss", sl_px))
    if attach_tp and tp_px:
        plans.append(("pos_profit", tp_px))
    if not plans:
        return out
    for plan_type, trigger in plans:
        body = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "planType": plan_type,
            "triggerPrice": str(trigger),
            "triggerType": "mark_price",
            "holdSide": hs,
        }
        # executePrice optional; omit rather than "0" (place-pos-tpsl rejects 0).
        r = _bitget(keys, "POST", "/api/v2/mix/order/place-tpsl-order", body)
        rec = {
            "endpoint": "place-tpsl-order",
            "planType": plan_type,
            "trigger": trigger,
            "holdSide": hs,
            "code": r.get("code"),
            "msg": r.get("msg"),
            "ok": _ok(r),
        }
        out.append(rec)
        if not rec["ok"] and str(r.get("code")) in ("43011", "400172"):
            body2 = dict(body)
            body2["holdSide"] = "long" if hs == "buy" else "short"
            r2 = _bitget(keys, "POST", "/api/v2/mix/order/place-tpsl-order", body2)
            rec2 = {
                "endpoint": "place-tpsl-order",
                "planType": plan_type,
                "trigger": trigger,
                "holdSide": body2["holdSide"],
                "code": r2.get("code"),
                "msg": r2.get("msg"),
                "ok": _ok(r2),
            }
            out.append(rec2)
            rec = rec2
        if rec.get("ok"):
            if "loss" in plan_type:
                got_sl = True
            if "profit" in plan_type:
                got_tp = True

    if (not attach_sl or got_sl) and (not attach_tp or got_tp):
        return out

    # 2) Sized plan fallback (loss_plan / profit_plan need size).
    close_side = "sell" if hs == "buy" else "buy"
    for plan_type, trigger, need in (
        ("loss_plan", sl_px, bool(attach_sl and not got_sl)),
        ("profit_plan", tp_px, bool(attach_tp and tp_px and not got_tp)),
    ):
        if not need:
            continue
        body = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "planType": plan_type,
            "triggerPrice": str(trigger),
            "triggerType": "mark_price",
            "holdSide": hs,
            "size": str(size),
            "side": close_side,
        }
        r = _bitget(keys, "POST", "/api/v2/mix/order/place-tpsl-order", body)
        rec = {
            "endpoint": "place-tpsl-order",
            "planType": plan_type,
            "trigger": trigger,
            "holdSide": hs,
            "code": r.get("code"),
            "msg": r.get("msg"),
            "ok": _ok(r),
        }
        out.append(rec)
        if not rec["ok"] and str(r.get("code")) in ("43011", "400172"):
            body2 = dict(body)
            body2["holdSide"] = "long" if hs == "buy" else "short"
            r2 = _bitget(keys, "POST", "/api/v2/mix/order/place-tpsl-order", body2)
            out.append(
                {
                    "endpoint": "place-tpsl-order",
                    "planType": plan_type,
                    "trigger": trigger,
                    "holdSide": body2["holdSide"],
                    "code": r2.get("code"),
                    "msg": r2.get("msg"),
                    "ok": _ok(r2),
                }
            )
    return out


def _pending_orders(keys, symbol: str) -> list:
    d = _bitget(
        keys,
        "GET",
        f"/api/v2/mix/order/orders-pending?productType=USDT-FUTURES&symbol={symbol}",
    )
    data = d.get("data") or {}
    if isinstance(data, dict):
        return list(
            data.get("entrustedList")
            or data.get("orderList")
            or data.get("entrustedPlanList")
            or []
        )
    if isinstance(data, list):
        return data
    return []


def place_limit_tp(keys, symbol: str, hold: str, size: str, tp_px: float) -> dict:
    """Resting reduce-only LIMIT take-profit (~0.4%). Not a trigger dump."""
    hs = _hold_side_oneway(hold)
    close_side = "sell" if hs == "buy" else "buy"
    body = {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "marginMode": "crossed",
        "size": str(size),
        "side": close_side,
        "orderType": "limit",
        "price": str(tp_px),
        "reduceOnly": "YES",
        "clientOid": ("e2tp" + str(int(time.time() * 1000)))[-20:],
    }
    r = _bitget(keys, "POST", "/api/v2/mix/order/place-order", body)
    return {
        "endpoint": "place-order",
        "kind": "limit_tp",
        "side": close_side,
        "price": tp_px,
        "reduceOnly": "YES",
        "code": r.get("code"),
        "msg": r.get("msg"),
        "ok": str((r or {}).get("code") or "") in ("00000", "0"),
        "orderId": (r.get("data") or {}).get("orderId") if isinstance(r.get("data"), dict) else None,
    }


def _has_limit_tp(orders: list, tp_px: float) -> bool:
    for o in orders:
        kind = str(o.get("orderType") or "").lower()
        if kind and kind != "limit":
            continue
        try:
            px = float(o.get("price") or o.get("priceAvg") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or tp_px <= 0:
            continue
        if abs(px - tp_px) / tp_px < 0.002:
            return True
    return False


def ensure_open_tpsl() -> dict:
    """For every live E2 fill missing exchange TP/SL, attach it. No LLM."""
    keys = _keys()
    pos = _bitget(
        keys,
        "GET",
        "/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT",
    )
    live = {}
    for p in pos.get("data") or []:
        try:
            sz = float(p.get("total") or p.get("available") or 0)
        except (TypeError, ValueError):
            sz = 0.0
        if abs(sz) < 1e-12:
            continue
        live[str(p.get("symbol") or "")] = p
    fills = []
    try:
        with open(FILLS_PATH) as fh:
            for line in fh:
                if not line.startswith("{"):
                    continue
                rec = json.loads(line)
                if rec.get("ok") and rec.get("symbol"):
                    fills.append(rec)
    except Exception:
        fills = []
    latest = {}
    for rec in fills:
        latest[str(rec.get("symbol"))] = rec
    attached = []
    skipped = []
    for symbol, rec in latest.items():
        if symbol not in live:
            continue
        pending = _pending_tpsl(keys, symbol)
        kinds = {
            str(x.get("planType") or x.get("orderType") or "").lower() for x in pending
        }
        has_sl = any("loss" in k for k in kinds)
        has_trigger_tp = any(("profit" in k or "surplus" in k) for k in kinds)
        hold = str(rec.get("hold") or live[symbol].get("holdSide") or "long")
        try:
            entry = float(live[symbol].get("openPriceAvg") or rec.get("price") or 0)
        except (TypeError, ValueError):
            entry = float(rec.get("price") or 0)
        try:
            sl = float(rec.get("sl") or 0.005)
        except (TypeError, ValueError):
            sl = 0.005
        if sl > 1:
            sl /= 100.0
        sl = min(max(sl, 0.003), 0.05)
        tp = 0.004
        pp = _price_place(keys, symbol)
        longish = hold.lower() in ("long", "buy")
        sl_px = round(entry * (1 - sl) if longish else entry * (1 + sl), pp)
        tp_px = round(entry * (1 + tp) if longish else entry * (1 - tp), pp)
        size = str(live[symbol].get("total") or rec.get("size") or "")
        cancelled = []
        if has_trigger_tp:
            pair_name = str(rec.get("pair") or (symbol[:-4] + "-USDT" if symbol.endswith("USDT") else symbol))
            cancelled = cancel_profit_plans(pair_name).get("cancelled") or []
        orders = _pending_orders(keys, symbol)
        has_limit = _has_limit_tp(orders, tp_px)
        pair_name = str(rec.get("pair") or (symbol[:-4] + "-USDT" if symbol.endswith("USDT") else symbol))
        try:
            mark = float(live[symbol].get("markPrice") or live[symbol].get("marketPrice") or 0)
        except (TypeError, ValueError):
            mark = 0.0
        u = 0.0
        if entry > 0 and mark > 0:
            u = (mark - entry) / entry if longish else (entry - mark) / entry
        lock = 0.004 if u >= 0.008 else (0.002 if u >= 0.0025 else None)
        if lock is not None:
            ratchet = modify_tpsl(pair_name, lock_pct=lock)
            if ratchet.get("ok") and not ratchet.get("skipped"):
                attached.append(
                    {
                        "symbol": symbol,
                        "entry": entry,
                        "mark": mark,
                        "unrealised": round(u, 5),
                        "rung": lock,
                        "ratchet": ratchet,
                        "ok": True,
                    }
                )
                pending = _pending_tpsl(keys, symbol)
                kinds = {
                    str(x.get("planType") or x.get("orderType") or "").lower() for x in pending
                }
                has_sl = any("loss" in k for k in kinds)
        if has_sl and has_limit:
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "already_has_sl_limit_tp",
                    "n": len(pending) + len(orders),
                    "unrealised": round(u, 5),
                    "rung": lock,
                }
            )
            continue
        tpsl = []
        if not has_sl:
            tpsl = attach_tpsl(
                keys, symbol, hold, size, sl_px, tp_px=None, attach_tp=False, attach_sl=True,
            )
        limit_rec = None
        if not has_limit:
            limit_rec = place_limit_tp(keys, symbol, hold, size, tp_px)
        attached.append(
            {
                "symbol": symbol,
                "entry": entry,
                "sl_px": sl_px,
                "tp_px": tp_px,
                "attach_sl": not has_sl,
                "attach_tp": not has_limit,
                "cancelled_trigger_tp": cancelled,
                "tpsl": tpsl,
                "limit_tp": limit_rec,
                "ok": (has_sl or any(x.get("ok") for x in tpsl)) and (has_limit or bool(limit_rec and limit_rec.get("ok"))),
            }
        )
    return {
        "ok": True,
        "live_e2": sorted(set(latest) & set(live)),
        "attached": attached,
        "skipped": skipped,
    }


def main() -> None:
    raw = sys.stdin.read().strip() or os.environ.get("E2_PLACE_JSON", "{}")
    cfg = json.loads(raw)
    pair = str(cfg.get("trading_pair") or "").upper()
    if not pair.endswith("-USDT"):
        print(json.dumps({"ok": False, "error": "bad_pair", "pair": pair}))
        return
    if pair in E4:
        print(json.dumps({"ok": False, "error": "e4_pair_blocked", "pair": pair}))
        return
    if pair not in E2_BASKET:
        print(json.dumps({"ok": False, "error": "e2_basket_blocked", "pair": pair, "basket": sorted(E2_BASKET)}))
        return
    try:
        side_i = int(cfg.get("side") or 1)
    except (TypeError, ValueError):
        side_i = 1
    side = "buy" if side_i == 1 else "sell"
    hold = "long" if side == "buy" else "short"
    lev = 20
    try:
        margin = float(cfg.get("margin_usd") or MARGIN_CAP)
    except (TypeError, ValueError):
        margin = MARGIN_CAP
    margin = max(1.0, min(margin, MARGIN_CAP))
    try:
        sl = float(cfg.get("stop_loss") or 0.005)
    except (TypeError, ValueError):
        sl = 0.005
    if sl > 1:
        sl = sl / 100.0
    sl = min(max(sl, 0.003), 0.05)
    # Mechanical bank ~$0.80 at 20x / $10. LLM cannot widen this.
    tp = 0.004
    dry = bool(cfg.get("dry_run"))
    controller_id = str(cfg.get("controller_id") or "e2")
    keys = _keys()
    symbol = _sym(pair)

    pos = _bitget(
        keys,
        "GET",
        "/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT",
    )
    open_pos = []
    for p in pos.get("data") or []:
        try:
            tot = float(p.get("total") or p.get("available") or 0)
        except (TypeError, ValueError):
            tot = 0.0
        if tot == 0:
            continue
        open_pos.append(
            {
                "symbol": p.get("symbol"),
                "holdSide": p.get("holdSide"),
                "total": tot,
            }
        )
        if p.get("symbol") == symbol:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "pair_occupied",
                        "pair": pair,
                        "holdSide": p.get("holdSide"),
                        "total": tot,
                    }
                )
            )
            return

    live_syms = {p["symbol"] for p in open_pos}
    e2_live = []
    try:
        with open(FILLS_PATH) as fh:
            for line in fh:
                if not line.startswith("{"):
                    continue
                rec = json.loads(line)
                if not rec.get("ok"):
                    continue
                rec_pair = str(rec.get("pair") or "")
                if rec_pair not in E2_BASKET:
                    continue
                rec_sym = _sym(rec_pair)
                if rec_sym in live_syms and rec_pair not in e2_live:
                    e2_live.append(rec_pair)
    except Exception:
        e2_live = []
    if len(e2_live) >= MAX_E2_SLOTS:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "e2_slots_full",
                    "used": len(e2_live),
                    "max": MAX_E2_SLOTS,
                    "pairs": e2_live,
                    "pair": pair,
                }
            )
        )
        return

    contracts = _bitget(
        keys,
        "GET",
        f"/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol={symbol}",
    )
    crow = next((c for c in (contracts.get("data") or []) if c.get("symbol") == symbol), {}) or {}
    try:
        volume_place = int(crow.get("volumePlace") or 4)
    except (TypeError, ValueError):
        volume_place = 4
    try:
        price_place = int(crow.get("pricePlace") or 4)
    except (TypeError, ValueError):
        price_place = 4
    try:
        min_trade = float(crow.get("minTradeNum") or 0)
    except (TypeError, ValueError):
        min_trade = 0.0

    tick = _bitget(
        keys,
        "GET",
        f"/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol={symbol}",
    )
    trow = (tick.get("data") or [{}])[0] if isinstance(tick.get("data"), list) else (tick.get("data") or {})
    try:
        last = float(trow.get("lastPr") or trow.get("markPrice") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0:
        print(json.dumps({"ok": False, "error": "no_price", "pair": pair, "raw": tick.get("code")}))
        return
    size = _round_size((margin * lev) / last, volume_place, min_trade)
    if size <= 0:
        print(json.dumps({"ok": False, "error": "size_zero", "pair": pair}))
        return
    notional = size * last
    client_oid = f"e2{int(time.time()*1000)}"[-20:]
    plan = {
        "ok": True,
        "dry_run": dry,
        "pair": pair,
        "symbol": symbol,
        "side": side,
        "hold": hold,
        "leverage": lev,
        "margin": margin,
        "size": size,
        "price": last,
        "notional": round(notional, 4),
        "sl": sl,
        "tp": tp,
        "clientOid": client_oid,
        "controller_id": controller_id,
    }
    if dry:
        print(json.dumps(plan))
        return

    lev_resp = _bitget(
        keys,
        "POST",
        "/api/v2/mix/account/set-leverage",
        {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "leverage": str(lev),
        },
    )
    plan["leverage_set"] = lev_resp.get("code")

    order_body = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "marginMode": "crossed",
            "size": str(size),
            "side": side,
            "orderType": "market",
            "clientOid": client_oid,
        }
    order = _bitget(
        keys,
        "POST",
        "/api/v2/mix/order/place-order",
        order_body,
    )
    if str(order.get("code")) not in ("00000", "0"):
        plan["ok"] = False
        plan["error"] = order.get("msg") or "place_failed"
        plan["bitget_code"] = order.get("code")
        print(json.dumps(plan))
        return
    data = order.get("data") or {}
    order_id = data.get("orderId") or client_oid
    plan["orderId"] = order_id
    plan["executor_id"] = order_id

    sl_px = round(last * (1 - sl) if hold == "long" else last * (1 + sl), price_place)
    tp_px = round(last * (1 + tp) if hold == "long" else last * (1 - tp), price_place)
    tpsl = attach_tpsl(keys, symbol, hold, str(size), sl_px, tp_px=None, attach_tp=False, attach_sl=True)
    limit_tp = place_limit_tp(keys, symbol, hold, str(size), tp_px)
    plan["tpsl"] = tpsl
    plan["limit_tp"] = limit_tp
    plan["tpsl_ok"] = any(x.get("ok") for x in tpsl) and bool(limit_tp.get("ok"))
    plan["sl_price"] = sl_px
    plan["tp_price"] = tp_px
    plan["safety_sl_only"] = False
    plan["bank_tp"] = tp
    plan["bank_kind"] = "limit"
    try:
        os.makedirs(os.path.dirname(FILLS_PATH), exist_ok=True)
        with open(FILLS_PATH, "a") as f:
            f.write(json.dumps({**plan, "ts": time.time()}) + "\n")
    except Exception:
        pass
    print(json.dumps(plan))


def cancel_profit_plans(pair: str) -> dict:
    """Cancel exchange take-profit on an E2 leg. Leaves safety SL in place."""
    keys = _keys()
    symbol = _sym(pair)
    pending = _pending_tpsl(keys, symbol)
    cancelled = []
    for x in pending:
        kind = str(x.get("planType") or x.get("orderType") or "").lower()
        if "profit" not in kind and "surplus" not in kind:
            continue
        oid = x.get("orderId")
        body = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "planType": x.get("planType") or "profit_loss",
        }
        if oid:
            body["orderId"] = str(oid)
        r = _bitget(keys, "POST", "/api/v2/mix/order/cancel-plan-order", body)
        cancelled.append({
            "orderId": oid,
            "planType": x.get("planType"),
            "code": r.get("code"),
            "msg": r.get("msg"),
            "ok": str(r.get("code") or "") in ("00000", "0"),
        })
    return {"ok": True, "pair": pair, "symbol": symbol, "cancelled": cancelled}


def close_pair(pair: str) -> dict:
    """Reduce-only market close of an E2 (or any) one-way leg. No tradeSide."""
    keys = _keys()
    symbol = _sym(pair)
    pos = _bitget(
        keys,
        "GET",
        "/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT",
    )
    row = None
    for p in pos.get("data") or []:
        if str(p.get("symbol") or "") != symbol:
            continue
        try:
            sz = float(p.get("total") or p.get("available") or 0)
        except (TypeError, ValueError):
            sz = 0.0
        if abs(sz) < 1e-12:
            continue
        row = p
        break
    if not row:
        return {"ok": False, "error": "not_open", "pair": pair}
    hold = str(row.get("holdSide") or "long").lower()
    side = "sell" if hold in ("long", "buy") else "buy"
    size = str(row.get("total") or row.get("available") or "")
    order = _bitget(
        keys,
        "POST",
        "/api/v2/mix/order/place-order",
        {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "marginMode": "crossed",
            "size": size,
            "side": side,
            "orderType": "market",
            "reduceOnly": "YES",
        },
    )
    ok = str(order.get("code") or "") in ("00000", "0")
    return {
        "ok": ok,
        "pair": pair,
        "symbol": symbol,
        "side": side,
        "size": size,
        "code": order.get("code"),
        "msg": order.get("msg"),
        "orderId": (order.get("data") or {}).get("orderId") if isinstance(order.get("data"), dict) else None,
    }


def _loss_plans(pending: list) -> list:
    out = []
    for x in pending:
        kind = str(x.get("planType") or x.get("orderType") or "").lower()
        if "loss" in kind:
            out.append(x)
    return out


def _tighter_sl(new_px: float, old_px: float | None, longish: bool, pp: int) -> bool:
    """True if new SL locks more profit. Long = higher trigger; short = lower. Never loosen."""
    if old_px is None or old_px <= 0:
        return True
    eps = 10 ** (-max(int(pp), 1))
    if longish:
        return new_px > old_px + eps
    return new_px < old_px - eps


def modify_tpsl(pair: str, sl_px: float | None = None, lock_pct: float | None = None) -> dict:
    """Move exchange SL. Modify first, cancel+place fallback. Never loosen. Leave LIMIT TP."""
    pair = str(pair or "").upper()
    if not pair.endswith("-USDT"):
        return {"ok": False, "error": "bad_pair", "pair": pair}
    keys = _keys()
    symbol = _sym(pair)
    pos = _bitget(
        keys,
        "GET",
        "/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT",
    )
    row = None
    for p in pos.get("data") or []:
        if str(p.get("symbol") or "") != symbol:
            continue
        try:
            sz = float(p.get("total") or p.get("available") or 0)
        except (TypeError, ValueError):
            sz = 0.0
        if abs(sz) < 1e-12:
            continue
        row = p
        break
    if not row:
        return {"ok": False, "error": "not_open", "pair": pair}
    hold = str(row.get("holdSide") or "long")
    longish = hold.lower() in ("long", "buy")
    size = str(row.get("total") or "")
    try:
        entry = float(row.get("openPriceAvg") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    pp = _price_place(keys, symbol)
    if lock_pct is not None:
        try:
            lp = float(lock_pct)
        except (TypeError, ValueError):
            lp = 0.002
        if lp > 1:
            lp /= 100.0
        lp = min(max(lp, 0.0), 0.02)
        if entry <= 0:
            return {"ok": False, "error": "no_entry", "pair": pair}
        sl_px = round(entry * (1 + lp) if longish else entry * (1 - lp), pp)
    if sl_px is None:
        return {"ok": False, "error": "need_sl_px_or_lock_pct", "pair": pair}
    try:
        sl_px = round(float(sl_px), pp)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_sl_px", "pair": pair, "sl_px": sl_px}
    try:
        mark = float(row.get("markPrice") or row.get("marketPrice") or 0)
    except (TypeError, ValueError):
        mark = 0.0
    tick = 10 ** (-max(int(pp), 1))
    # Bitget rejects (or immediately fills) SL on the wrong side of mark.
    # Short SL must stay ABOVE mark; long SL must stay BELOW mark.
    if mark > 0:
        if longish and sl_px >= mark - tick:
            return {
                "ok": True,
                "skipped": "would_trigger",
                "pair": pair,
                "wanted": sl_px,
                "mark": mark,
                "limit_tp_left": True,
            }
        if (not longish) and sl_px <= mark + tick:
            return {
                "ok": True,
                "skipped": "would_trigger",
                "pair": pair,
                "wanted": sl_px,
                "mark": mark,
                "limit_tp_left": True,
            }
    pending = _pending_tpsl(keys, symbol)
    losses = _loss_plans(pending)
    cur = None
    oid = None
    for x in losses:
        try:
            cur = float(x.get("triggerPrice") or 0)
        except (TypeError, ValueError):
            cur = None
        oid = x.get("orderId")
        break
    if not _tighter_sl(sl_px, cur, longish, pp):
        return {
            "ok": True,
            "skipped": "not_tighter",
            "pair": pair,
            "current": cur,
            "wanted": sl_px,
            "limit_tp_left": True,
        }
    modify_fail = None
    if oid:
        body = {
            "orderId": str(oid),
            "marginCoin": "USDT",
            "productType": "USDT-FUTURES",
            "symbol": symbol,
            "triggerPrice": str(sl_px),
            "triggerType": "mark_price",
            "executePrice": "0",
            "size": "",
        }
        r = _bitget(keys, "POST", "/api/v2/mix/order/modify-tpsl-order", body)
        rec = {
            "endpoint": "modify-tpsl-order",
            "orderId": oid,
            "trigger": sl_px,
            "code": r.get("code"),
            "msg": r.get("msg"),
            "ok": str(r.get("code") or "") in ("00000", "0"),
        }
        if rec["ok"]:
            return {
                "ok": True,
                "pair": pair,
                "sl_px": sl_px,
                "method": "modify",
                "result": rec,
                "limit_tp_left": True,
            }
        modify_fail = rec
    cancelled = []
    for x in losses:
        cbody = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "planType": x.get("planType") or "profit_loss",
        }
        if x.get("orderId"):
            cbody["orderId"] = str(x.get("orderId"))
        cr = _bitget(keys, "POST", "/api/v2/mix/order/cancel-plan-order", cbody)
        cancelled.append(
            {
                "orderId": x.get("orderId"),
                "code": cr.get("code"),
                "ok": str(cr.get("code") or "") in ("00000", "0"),
            }
        )
    tpsl = attach_tpsl(keys, symbol, hold, size, float(sl_px), attach_tp=False, attach_sl=True)
    ok = any(x.get("ok") for x in tpsl)
    err = None
    if not ok:
        bits = []
        if modify_fail:
            bits.append(f"modify:{modify_fail.get('code')}:{modify_fail.get('msg')}")
        for x in tpsl:
            if not x.get("ok"):
                bits.append(f"place:{x.get('code')}:{x.get('msg')}")
        err = " | ".join(bits) or "cancel_place_failed"
    return {
        "ok": ok,
        "error": err,
        "pair": pair,
        "sl_px": sl_px,
        "method": "cancel_place",
        "modify_fail": modify_fail,
        "cancelled": cancelled,
        "tpsl": tpsl,
        "limit_tp_left": True,
    }


def amend_sl(pair: str, sl_px: float) -> dict:
    """Back-compat wrapper: modify first, cancel+place fallback."""
    return modify_tpsl(pair, sl_px=float(sl_px))


def list_e2_closes(since_ms: int | None = None) -> dict:
    """Closing fills for the E2 basket, grouped by orderId. Read-only."""
    keys = _keys()
    end_ms = int(time.time() * 1000)
    if not since_ms or since_ms <= 0:
        since_ms = end_ms - 24 * 3600 * 1000
    # Bitget rejects windows > 90 days
    if end_ms - int(since_ms) > 89 * 86400 * 1000:
        since_ms = end_ms - 89 * 86400 * 1000
    e2_sym = {_sym(p) for p in E2_BASKET}
    fills: list = []
    last_id = None
    page = 0
    while page < 40:
        page += 1
        path = (
            f"/api/v2/mix/order/fills?productType=USDT-FUTURES"
            f"&startTime={int(since_ms)}&endTime={end_ms}&limit=100"
        )
        if last_id is not None:
            path += f"&idLessThan={last_id}"
        r = _bitget(keys, "GET", path)
        if str(r.get("code")) not in ("00000", "0"):
            return {"ok": False, "error": r.get("code"), "msg": r.get("msg"), "trades": []}
        data = r.get("data") or {}
        rows = data.get("fillList") if isinstance(data, dict) else data
        rows = rows or []
        if not rows:
            break
        fills.extend(rows)
        last_id = rows[-1].get("tradeId") or rows[-1].get("id")
        if len(rows) < 100:
            break
    grouped: dict = {}
    for f in fills:
        sym = str(f.get("symbol") or "")
        if sym not in e2_sym:
            continue
        try:
            pnl = float(f.get("profit") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if abs(pnl) < 1e-12:
            continue
        oid = str(f.get("orderId") or f.get("tradeId") or "")
        if not oid:
            continue
        try:
            px = float(f.get("price") or 0)
            sz = float(f.get("baseVolume") or f.get("size") or 0)
            fee = abs(float(f.get("fee") or 0))
            ts = int(f.get("cTime") or f.get("ts") or 0)
        except (TypeError, ValueError):
            px, sz, fee, ts = 0.0, 0.0, 0.0, 0
        g = grouped.setdefault(
            oid,
            {
                "orderId": oid,
                "symbol": sym,
                "side": str(f.get("side") or "").lower(),
                "pnl": 0.0,
                "fee": 0.0,
                "notional": 0.0,
                "size": 0.0,
                "price": px,
                "ts": 0,
            },
        )
        g["pnl"] += pnl
        g["fee"] += fee
        g["notional"] += abs(px * sz)
        g["size"] += abs(sz)
        g["price"] = px or g["price"]
        g["ts"] = max(int(g["ts"] or 0), ts)
        if f.get("side"):
            g["side"] = str(f.get("side") or "").lower()
    trades = []
    for g in grouped.values():
        sym = g["symbol"]
        pair = (sym[:-4] + "-USDT") if (sym.endswith("USDT") and "-" not in sym) else sym
        side_raw = g["side"]
        side = "LONG" if side_raw == "sell" else "SHORT"
        pnl = float(g["pnl"])
        ts = int(g["ts"] or 0)
        trades.append(
            {
                "id": f"e2-bg-{g['orderId']}",
                "symbol": pair,
                "pair": pair,
                "side": side,
                "engine": "E2",
                "pnl": round(pnl, 6),
                "fees": round(float(g["fee"]), 6),
                "filled_quote": round(float(g["notional"]), 4),
                "amount": float(g["size"]),
                "exit": float(g["price"] or 0),
                "leverage": 20,
                "close_type": "BITGET_CLOSE",
                "timestamp": ts / 1000.0 if ts > 1e12 else float(ts),
                "orderId": g["orderId"],
            }
        )
    trades.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
    return {"ok": True, "trades": trades, "n": len(trades), "pages": page}


if __name__ == "__main__":
    cmd = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "")
    if cmd in ("ensure_tpsl", "ensure-tpsl", "tpsl"):
        print(json.dumps(ensure_open_tpsl()))
    elif cmd in ("cancel_tp", "cancel-tp"):
        raw = sys.stdin.read().strip() or "{}"
        cfg = json.loads(raw)
        pair = str(cfg.get("trading_pair") or cfg.get("pair") or "").upper()
        print(json.dumps(cancel_profit_plans(pair)))
    elif cmd == "close":
        raw = sys.stdin.read().strip() or "{}"
        cfg = json.loads(raw)
        pair = str(cfg.get("trading_pair") or cfg.get("pair") or "").upper()
        print(json.dumps(close_pair(pair)))
    elif cmd in ("amend_sl", "amend-sl", "modify_tpsl", "modify-tpsl"):
        raw = sys.stdin.read().strip() or "{}"
        cfg = json.loads(raw)
        pair = str(cfg.get("trading_pair") or cfg.get("pair") or "").upper()
        lock = cfg.get("lock_pct")
        sl_px = cfg.get("sl_px")
        print(json.dumps(modify_tpsl(pair, sl_px=sl_px, lock_pct=lock)))
    elif cmd in ("closes", "e2_closes", "closed"):
        raw = sys.stdin.read().strip() or "{}"
        cfg = json.loads(raw) if raw else {}
        since = cfg.get("since_ms") or cfg.get("startTime")
        print(json.dumps(list_e2_closes(int(since) if since else None)))
    else:
        main()
