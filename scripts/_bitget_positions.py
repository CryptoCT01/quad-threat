"""Print open Bitget USDT-M positions as one JSON line. No secrets."""
import hmac, hashlib, base64, time, json, urllib.request, os
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.security import Security

sm = ETHKeyFileSecretManger(os.environ.get("HBOT_PASSWORD", "CHANGE_ME"))
if not Security.login(sm):
    print("[]")
    raise SystemExit(0)
keys = Security.api_keys("bitget_perpetual") or {}
api_key = keys.get("bitget_perpetual_api_key")
secret = keys.get("bitget_perpetual_secret_key")
passphrase = keys.get("bitget_perpetual_passphrase")
if not (api_key and secret and passphrase):
    print("[]")
    raise SystemExit(0)

ts = str(int(time.time() * 1000))
path = "/api/v2/mix/position/all-position?productType=USDT-FUTURES"
msg = ts + "GET" + path
sign = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
req = urllib.request.Request(
    "https://api.bitget.com" + path,
    headers={
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "locale": "en-US",
    },
)
d = json.loads(urllib.request.urlopen(req, timeout=12).read())
out = []
for p in (d.get("data") or []):
    try:
        amt = float(p.get("total") or p.get("available") or 0)
    except (TypeError, ValueError):
        amt = 0.0
    if abs(amt) < 1e-12:
        continue
    out.append({
        "symbol": p.get("symbol"),
        "holdSide": p.get("holdSide"),
        "total": amt,
        "available": float(p.get("available") or 0),
        "openPriceAvg": float(p.get("openPriceAvg") or 0),
        "markPrice": float(p.get("markPrice") or 0),
        "unrealizedPL": float(p.get("unrealizedPL") or 0),
        "margin": float(p.get("marginSize") or p.get("margin") or 0),
        "leverage": p.get("leverage"),
    })
print(json.dumps(out))
