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
E2_BASKET = {"XAU-USDT", "CL-USDT", "SUI-USDT", "DOGE-USDT", "LINK-USDT"}
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
