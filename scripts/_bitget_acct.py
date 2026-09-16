"""Print Bitget USDT-M equity/available as one JSON line. No secrets."""
import hmac, hashlib, base64, time, json, urllib.request, os
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.security import Security

sm = ETHKeyFileSecretManger(os.environ.get("HBOT_PASSWORD", "CHANGE_ME"))
if not Security.login(sm):
    print("{}")
    raise SystemExit(0)
keys = Security.api_keys("bitget_perpetual") or {}
api_key = keys.get("bitget_perpetual_api_key")
secret = keys.get("bitget_perpetual_secret_key")
passphrase = keys.get("bitget_perpetual_passphrase")
if not (api_key and secret and passphrase):
    print("{}")
    raise SystemExit(0)

ts = str(int(time.time() * 1000))
path = "/api/v2/mix/account/accounts?productType=USDT-FUTURES"
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
a = next((x for x in (d.get("data") or []) if x.get("marginCoin") == "USDT"), {})
def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0

# Available chip = what you can still OPEN with (unionAvailable / crossedMaxAvailable).
# Bitget `available` stays near equity even when margin is locked — that is NOT free cash.
openable = _f(a.get("unionAvailable") or a.get("crossedMaxAvailable"))
out = {
    "equity": _f(a.get("accountEquity") or a.get("usdtEquity")),
    "available": openable,
    "wallet_available": _f(a.get("available")),
    "crossed": _f(a.get("crossedMaxAvailable")),
    "locked": _f(a.get("locked")),
    "margin": _f(a.get("crossedMargin")),
    "mm": _f(a.get("unionMm")),
}
print(json.dumps(out))
