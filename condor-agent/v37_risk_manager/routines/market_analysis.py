"""Engine 2 basket-only impulse briefing. No MACD, no S/R, no 24-pair dump."""

CATEGORY = "Engine 2"

import logging
from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

E2_BASKET = {
    "XAU-USDT": 20,
    "CL-USDT": 20,
    "DOGE-USDT": 20,
    "NEAR-USDT": 20,
    "LTC-USDT": 20,
}


class Config(BaseModel):
    connector_name: str = Field(default="bitget_perpetual")
    pairs: str = Field(
        default="ALL",
        description="ALL = 20x basket only. Other names are ignored.",
    )


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


async def _fetch_candles(client, connector: str, pair: str, interval: str, days: int):
    try:
        result = await client.market_data.get_candles_last_days(
            connector_name=connector,
            trading_pair=pair,
            days=days,
            interval=interval,
        )
    except Exception as e:
        logger.warning(f"candles_last_days failed for {pair} {interval}: {e}")
        return None

    records = (
        result
        if isinstance(result, list)
        else (result.get("data", result.get("candles", [])) if isinstance(result, dict) else [])
    )
    if not records or len(records) < 20:
        return None

    df = pd.DataFrame(records)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(
            df["timestamp"],
            unit="s" if df["timestamp"].iloc[0] > 1e12 / 1000 else "ms",
            utc=True,
        )
    df = df.sort_values("datetime").reset_index(drop=True)
    # Drop the forming bar. Signals use last fully closed candle only.
    if len(df) >= 2:
        df = df.iloc[:-1].reset_index(drop=True)
    return df


async def _analyze_pair(client, connector: str, pair: str) -> dict | None:
    df_1h = await _fetch_candles(client, connector, pair, "1h", 7)
    if df_1h is None or len(df_1h) < 24:
        return None
    df_15 = await _fetch_candles(client, connector, pair, "15m", 3)

    close = df_1h["close"]
    high = df_1h["high"]
    low = df_1h["low"]
    last = df_1h.iloc[-1]
    prev = df_1h.iloc[-2]
    price = float(last["close"])
    o = float(last["open"])
    h = float(last["high"])
    l = float(last["low"])
    rng = max(h - l, 1e-12)
    body = abs(price - o)
    ema12_val = float(ema(close, 12).iloc[-1])
    ema26_val = float(ema(close, 26).iloc[-1])
    atr14 = float(atr(high, low, close, 14).iloc[-1])
    atr_pct = (atr14 / price * 100) if price > 0 else 0.0
    hh20 = float(high.tail(20).max())
    ll20 = float(low.tail(20).min())
    avg_rng = float((high - low).tail(20).mean()) or rng

    bull_1h = ema12_val > ema26_val
    bear_1h = ema12_val < ema26_val
    doji = body / rng < 0.25
    expanding = rng > 1.2 * avg_rng or atr_pct >= 0.5
    impulse_long = (price >= hh20 * 0.999) or (price > o and body / rng > 0.55 and price > ema12_val)
    impulse_short = (price <= ll20 * 1.001) or (price < o and body / rng > 0.55 and price < ema12_val)
    if df_15 is not None and len(df_15) >= 8:
        c15 = float(df_15["close"].iloc[-1])
        hh15 = float(df_15["high"].tail(16).max())
        ll15 = float(df_15["low"].tail(16).min())
        impulse_long = impulse_long or c15 >= hh15 * 0.999
        impulse_short = impulse_short or c15 <= ll15 * 1.001

    long_bits = [impulse_long, bull_1h and not doji, expanding]
    short_bits = [impulse_short, bear_1h and not doji, expanding]
    n_long = sum(1 for x in long_bits if x)
    n_short = sum(1 for x in short_bits if x)
    if n_long >= 2 and n_long >= n_short:
        setup = "OPEN_LONG"
        n = n_long
    elif n_short >= 2:
        setup = "OPEN_SHORT"
        n = n_short
    else:
        setup = "HOLD"
        n = max(n_long, n_short)

    chg = 0.0
    if len(df_1h) >= 24:
        chg = (price - float(df_1h.iloc[-24]["close"])) / float(df_1h.iloc[-24]["close"]) * 100

    return {
        "pair": pair,
        "leverage": 20,
        "price": price,
        "change_24h": round(chg, 2),
        "setup": setup,
        "factors": n,
        "impulse": "L" if impulse_long else ("S" if impulse_short else "—"),
        "dir_1h": "BULL" if bull_1h and not doji else ("BEAR" if bear_1h and not doji else "CHOP"),
        "range": "EXPAND" if expanding else "TIGHT",
        "atr_pct": round(atr_pct, 2),
        "hh20": round(hh20, 6),
        "ll20": round(ll20, 6),
        "last_body": round(body / rng, 2),
        "prev_close": round(float(prev["close"]), 6),
    }


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(text="No Hummingbot server available")

    connector = config.connector_name
    if config.pairs.upper() == "ALL":
        want = list(E2_BASKET)
    else:
        want = []
        for p in (x.strip().upper() for x in config.pairs.split(",") if x.strip()):
            pair = p if p.endswith("-USDT") else f"{p}-USDT"
            if pair in E2_BASKET:
                want.append(pair)

    results = []
    errors = []
    for pair in want:
        try:
            analysis = await _analyze_pair(client, connector, pair)
            if analysis:
                results.append(analysis)
            else:
                errors.append(f"{pair}: insufficient data")
        except Exception as e:
            errors.append(f"{pair}: {e}")

    if not results:
        return RoutineResult(text=f"No basket pairs analyzed. Errors: {'; '.join(errors[:5])}")

    results.sort(key=lambda x: (0 if x["setup"].startswith("OPEN") else 1, -x["factors"]))
    lines = [
        f"E2 BASKET IMPULSE — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "XAU/CL/DOGE/NEAR/LTC only · 20x · 2+ of impulse / 1h dir / expanding range",
        "No MACD. No S/R. Do not fade. Exchange banks 0.4% TP / 0.5% SL on new opens.",
        "",
    ]
    for r in results:
        lines.append(
            f"**{r['pair']}** {r['setup']} {r['factors']}/3  ${r['price']:.4g} ({r['change_24h']:+.2f}%) 20x\n"
            f"  impulse={r['impulse']}  1h={r['dir_1h']}  range={r['range']}  ATR={r['atr_pct']}%\n"
            f"  20h high {r['hh20']:.4g} / low {r['ll20']:.4g}  last body {r['last_body']}"
        )
        lines.append("")
    if errors:
