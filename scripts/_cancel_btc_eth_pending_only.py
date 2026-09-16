#!/usr/bin/env python3
"""Cancel Bitget pending LIMIT quotes on BTCUSDT+ETHUSDT only. Never positions, never other symbols, never plan TPSL."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, time, urllib.request, urllib.error
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.security import Security


def keys():
    sm = ETHKeyFileSecretManger(os.environ.get("HBOT_PASSWORD", "CHANGE_ME"))
    if not Security.login(sm):
        raise SystemExit("login_failed")
    k = Security.api_keys("bitget_perpetual") or {}
    return {
        "key": k.get("bitget_perpetual_api_key"),
        "secret": k.get("bitget_perpetual_secret_key"),
        "pass": k.get("bitget_perpetual_passphrase"),
    }


def q(k, method, path, body=None):
    ts = str(int(time.time() * 1000))
    payload = json.dumps(body) if body is not None else ""
    msg = ts + method + path + payload
    sign = base64.b64encode(hmac.new(k["secret"].encode(), msg.encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request(
        "https://api.bitget.com" + path,
        data=payload.encode() if body is not None else None,
        method=method,
        headers={
            "ACCESS-KEY": k["key"],
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": k["pass"],
            "Content-Type": "application/json",
            "locale": "en-US",
        },
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": str(e.code)}


def pending_list(k):
    pending = q(k, "GET", "/api/v2/mix/order/orders-pending?productType=USDT-FUTURES")
    plist = pending.get("data") or []
    if isinstance(plist, dict):
        plist = plist.get("entrustedList") or plist.get("orderList") or []
    return plist if isinstance(plist, list) else []


def main():
    k = keys()
    before = pending_list(k)
    print(f"[pending_before] n={len(before)}")
    for o in before:
        print(
            f"  {o.get('symbol')} {o.get('side')} {o.get('orderType')} "
            f"px={o.get('price')} sz={o.get('size')} id={o.get('orderId')}"
        )
    for sym in ("BTCUSDT", "ETHUSDT"):
        r = q(
            k,
            "POST",
            "/api/v2/mix/order/cancel-all-orders",
            {"productType": "USDT-FUTURES", "marginCoin": "USDT", "symbol": sym},
        )
        print(f"[cancel_all {sym}]", r.get("code"), r.get("msg"), str(r.get("data"))[:160])
    time.sleep(1)
    after = pending_list(k)
    print(f"[pending_after] n={len(after)}")
    for o in after:
        print(
            f"  LEFT {o.get('symbol')} {o.get('side')} {o.get('orderType')} "
            f"px={o.get('price')} sz={o.get('size')}"
        )
    print("[NOTE] positions NOT touched; plans NOT touched; only BTC/ETH pending")


if __name__ == "__main__":
    main()
