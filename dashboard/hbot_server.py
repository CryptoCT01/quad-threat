"""
hbot_server.py — Local proxy + dashboard server for Hummingbot × Bitget
Serves dashboard on :8770.
Port map — never steal these:
  trading-floor-bitget → 8765
  tradfi-bitget        → 8766
  humming-bot dash     → 8770
Provides:
  - Bitget public market data (candles, tickers)
  - Multi-strategy signal scan (SUPER_A, ROC_RSI, BB_VOL)
  - Strategy toggle state (persisted)
  - Proxy to Hummingbot API (:8000) with basic auth
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple

# Port map (do not collide):
#   trading-floor-bitget → 8765
#   tradfi-bitget        → 8766
#   humming-bot dash     → 8770
PORT = int(os.environ.get("HBOT_DASH_PORT", "8770"))
HBOT_API = "http://localhost:8000"
HBOT_AUTH = ("admin", "admin")
BOT_NAME = "hummingbot"
# Default controller config for dashboard Start/Stop (conf/controllers/)
BOT_CONFIG = os.environ.get("HBOT_DASH_CONFIG", "conf_v37_scalp_multi.yml")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Shared with live Hummingbot controller (bind-mounted conf/)
HBOT_CONF_DIR = os.path.expanduser("~/Desktop/humming-bot/hummingbot/conf")
STATE_PATH = os.path.join(HBOT_CONF_DIR, "active_strategy.json")
# local fallback copy in dashboard folder
STATE_PATH_LOCAL = os.path.join(BASE_DIR, "active_strategy.json")
UNIVERSE_PATH = os.path.join(HBOT_CONF_DIR, "universe.yml")

MAX_OPEN_POSITIONS = 3


def load_universe_file() -> Dict[str, Any]:
    """Load conf/universe.yml (crypto + stocks + leverage)."""
    default_crypto = [
        {"symbol": "BTC-USDT", "leverage": 15, "tier": 1},
        {"symbol": "ETH-USDT", "leverage": 15, "tier": 1},
        {"symbol": "SOL-USDT", "leverage": 10, "tier": 2},
        {"symbol": "XRP-USDT", "leverage": 10, "tier": 2},
        {"symbol": "SUI-USDT", "leverage": 5, "tier": 3},
        {"symbol": "DOGE-USDT", "leverage": 5, "tier": 3},
        {"symbol": "LINK-USDT", "leverage": 5, "tier": 2},
        {"symbol": "DOT-USDT", "leverage": 5, "tier": 2},
        {"symbol": "ARB-USDT", "leverage": 5, "tier": 3},
        {"symbol": "FET-USDT", "leverage": 5, "tier": 3},
    ]
    default_commodities = [
        {"symbol": "XAU-USDT", "leverage": 5, "tier": 2},
        {"symbol": "CL-USDT", "leverage": 5, "tier": 2},
    ]
    default_stocks = [
        {"symbol": "TSLA-USDT", "leverage": 5, "tier": 2},
        {"symbol": "NVDA-USDT", "leverage": 5, "tier": 2},
        {"symbol": "AAPL-USDT", "leverage": 5, "tier": 2},
        {"symbol": "AMZN-USDT", "leverage": 5, "tier": 2},
        {"symbol": "META-USDT", "leverage": 5, "tier": 2},
        {"symbol": "MSFT-USDT", "leverage": 5, "tier": 2},
        {"symbol": "GOOGL-USDT", "leverage": 5, "tier": 2},
        {"symbol": "COIN-USDT", "leverage": 5, "tier": 3},
        {"symbol": "MSTR-USDT", "leverage": 5, "tier": 3},
        {"symbol": "HOOD-USDT", "leverage": 5, "tier": 3},
        {"symbol": "PLTR-USDT", "leverage": 5, "tier": 3},
        {"symbol": "AMD-USDT", "leverage": 5, "tier": 3},
    ]
    data: Dict[str, Any] = {
        "max_open_positions": MAX_OPEN_POSITIONS,
        "position_size_quote": 10,
        "crypto": default_crypto,
        "commodities": default_commodities,
        "stocks": default_stocks,
    }
    try:
        if os.path.isfile(UNIVERSE_PATH):
            try:
                import yaml  # type: ignore
                raw = yaml.safe_load(open(UNIVERSE_PATH).read()) or {}
            except Exception:
                raw = {}
            if raw:
                data.update(raw)
    except Exception as e:
        print(f"[warn] universe load: {e}")

    crypto = data.get("crypto") or default_crypto
    commodities = data.get("commodities") or default_commodities
    stocks = data.get("stocks") or default_stocks
    all_rows = list(crypto) + list(commodities) + list(stocks)
    leverage_map = {r["symbol"]: int(r.get("leverage") or 5) for r in all_rows if r.get("symbol")}
    tier_map = {r["symbol"]: int(r.get("tier") or 3) for r in all_rows if r.get("symbol")}
    symbols = [r["symbol"] for r in all_rows if r.get("symbol")]
    return {
        **data,
        "crypto": crypto,
        "stocks": stocks,
        "symbols": symbols,
        "leverage_map": leverage_map,
        "tier_map": tier_map,
        "max_open_positions": int(data.get("max_open_positions") or MAX_OPEN_POSITIONS),
    }


_UNIVERSE = load_universe_file()
CRYPTO_SYMBOLS = [r["symbol"] for r in _UNIVERSE.get("crypto") or []]
COMMODITY_SYMBOLS = [r["symbol"] for r in _UNIVERSE.get("commodities") or []]
STOCK_SYMBOLS = [r["symbol"] for r in _UNIVERSE.get("stocks") or []]
SYMBOLS = list(_UNIVERSE.get("symbols") or (CRYPTO_SYMBOLS + COMMODITY_SYMBOLS + STOCK_SYMBOLS))
TIER_MAP = dict(_UNIVERSE.get("tier_map") or {})
LEV_MAP = dict(_UNIVERSE.get("leverage_map") or {})
MAX_OPEN_POSITIONS = int(_UNIVERSE.get("max_open_positions") or MAX_OPEN_POSITIONS or 3)
HBOT_PASSWORD = os.environ.get("HBOT_PASSWORD", "CHANGE_ME")
HBOT_BIN = os.environ.get("HBOT_BIN") or shutil.which("hbot") or os.path.expanduser("~/.local/bin/hbot")

# Single-flight bot ops — concurrent Start with replace=True was killing the bot mid-init
_bot_op_lock = threading.Lock()
_bot_op: Dict[str, Any] = {
    "phase": "idle",       # idle | starting | stopping
    "started_at": 0.0,
    "result": None,
    "error": None,
}
# Short TTL cache for hbot status — avoids thundering-herd docker execs that freeze the UI
_status_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
STATUS_TTL = 2.5

_strategies: Dict[str, Dict[str, Any]] = {
    "SUPER_A": {
        "active": True, "name": "SUPER A", "emoji": "🔮",
        "pf": 1.11, "wr": 69.4, "pnl": "+$78", "trades": 350,
        "confirm_cycles": 8, "min_components": 5,
    },
    "ROC_RSI": {
        "active": True, "name": "ROC + RSI", "emoji": "⚡",
        "pf": 1.20, "wr": 73.5, "pnl": "+$19", "trades": 892,
        "confirm_cycles": 2, "min_components": 2,
    },
    "BB_VOL": {
        "active": True, "name": "BB + VOL", "emoji": "🔊",
        "pf": 1.22, "wr": 74.9, "pnl": "+$10", "trades": 450,
        "confirm_cycles": 2, "min_components": 3,
    },
}

_journal: List[Dict[str, Any]] = []
_journal_last_fetch = 0.0
_perf_cache = {
    "win_rate": "—", "profit_factor": "—", "total_pnl": 0,
    "trades": 0, "sharpe": "—", "max_dd": "0%",
    "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0,
    "avg_win": 0.0, "avg_loss": 0.0, "fees": 0.0, "volume": 0.0,
    "by_close_type": {}, "by_engine": {}, "by_symbol": {}, "source": "none",
}
_perf_last_fetch = 0.0

# Bot SQLite DBs (written by the hummingbot container)
HBOT_DATA_DIR = os.path.expanduser("~/Desktop/humming-bot/hummingbot/data")
HBOT_E4_DATA_DIR = os.path.expanduser("~/Desktop/humming-bot/hummingbot/e4/data")
_EXECUTOR_DB_CANDIDATES = [
    os.path.join(HBOT_DATA_DIR, "conf_v37_scalp_multi.sqlite"),
    os.path.join(HBOT_DATA_DIR, "conf_v37_scalp.sqlite"),
]
# Per-engine DBs. E1 truth is Executors; E4 PMM truth is TradeFill
# (quote refresh leaves filled_amount_quote=0 on executors).
_ENGINE_DBS = [
    {
        "path": os.path.join(HBOT_DATA_DIR, "conf_v37_scalp_multi.sqlite"),
        "engine": "E1",
        "strategy": "v37_scalp_multi",
        "prefer": "executors",
    },
    {
        "path": os.path.join(HBOT_DATA_DIR, "conf_v37_scalp.sqlite"),
        "engine": "E1",
        "strategy": "v37_scalp_multi",
        "prefer": "executors",
    },
    {
        "path": os.path.join(HBOT_E4_DATA_DIR, "conf_e4_pmm.sqlite"),
        "engine": "E4",
        "strategy": "e4_pmm",
        "prefer": "fills",
    },
]
PERF_BASELINE_PATH = os.path.join(HBOT_CONF_DIR, "perf_baseline.json")
# Hummingbot CloseType enum values
_CLOSE_TYPE_MAP = {
    1: "TIME_LIMIT",
    2: "STOP_LOSS",
    3: "TAKE_PROFIT",
    4: "EXPIRED",
    5: "EARLY_STOP",
    6: "TRAILING_STOP",
    7: "INSUFFICIENT_BALANCE",
    8: "FAILED",
    9: "COMPLETED",
    10: "POSITION_HOLD",
}


def _perf_reset_ts() -> float:
    """Unix seconds; trades closed at/before this are hidden from journal/perf."""
    try:
        if os.path.isfile(PERF_BASELINE_PATH):
            with open(PERF_BASELINE_PATH, "r") as f:
                data = json.load(f)
            return float(data.get("reset_at") or 0)
    except Exception:
        pass
    return 0.0

# Caches
_candle_cache: Dict[str, Tuple[float, List]] = {}  # key -> (ts, candles)
_ticker_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
_scan_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
_persist: Dict[str, Dict[str, Any]] = {}  # "STRAT:SYM" -> {direction, count, first_seen}
_lock = threading.Lock()

CANDLE_TTL = 25.0
TICKER_TTL = 8.0
SCAN_TTL = 20.0


def _load_strategy_state() -> None:
    global _strategies
    try:
        path = STATE_PATH if os.path.isfile(STATE_PATH) else STATE_PATH_LOCAL
        if not os.path.isfile(path):
            _save_strategy_state()
            return
        with open(path, "r") as f:
            data = json.load(f)
        enabled = data.get("enabled") or data.get("enabled_strategies") or []
        if isinstance(enabled, list) and enabled:
            for k in _strategies:
                _strategies[k]["active"] = k in enabled
        active_map = data.get("strategies") or {}
        for k, v in active_map.items():
            if k in _strategies and isinstance(v, dict) and "active" in v:
                _strategies[k]["active"] = bool(v["active"])
        # flat flags
        for k in _strategies:
            if k in data and isinstance(data[k], bool):
                _strategies[k]["active"] = data[k]
    except Exception as e:
        print(f"[warn] load strategy state: {e}")


def _save_strategy_state() -> None:
    try:
        enabled = [k for k, v in _strategies.items() if v.get("active")]
        payload = {
            "enabled": enabled,
            "enabled_strategies": enabled,
            "strategies": {k: {"active": bool(v.get("active"))} for k, v in _strategies.items()},
            "updated_at": time.time(),
            # live controller reads these flags every tick
            "SUPER_A": bool(_strategies.get("SUPER_A", {}).get("active")),
            "ROC_RSI": bool(_strategies.get("ROC_RSI", {}).get("active")),
            "BB_VOL": bool(_strategies.get("BB_VOL", {}).get("active")),
        }
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        for path in (STATE_PATH, STATE_PATH_LOCAL):
            try:
                with open(path, "w") as f:
                    json.dump(payload, f, indent=2)
            except Exception as e:
                print(f"[warn] save strategy state {path}: {e}")
    except Exception as e:
        print(f"[warn] save strategy state: {e}")


def normalize_symbol(symbol: str) -> str:
    """Normalize any BTCUSDT / BTC-USDT / btc_usdt → BTC-USDT."""
    s = (symbol or "BTC-USDT").upper().replace("_", "-").replace("/", "-").strip()
    if "-" in s:
        parts = [p for p in s.split("-") if p]
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[-1]}"
        s = parts[0] if parts else "BTCUSDT"
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}-USDT"
    if s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}-USDT"
    return f"{s}-USDT" if not s.endswith("-USDT") else s


def bitget_symbol(symbol: str) -> str:
    return normalize_symbol(symbol).replace("-", "")


def hbot_request(method: str, path: str, body: Any = None, timeout: float = 10.0) -> Dict[str, Any]:
    url = HBOT_API + path
    data = None
    headers = {
        "Authorization": "Basic " + base64.b64encode(
            f"{HBOT_AUTH[0]}:{HBOT_AUTH[1]}".encode()
        ).decode(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        try:
            parsed = json.loads(err_body)
        except Exception:
            parsed = {"detail": err_body}
        return {"error": f"HTTP {e.code}", "status": e.code, "detail": parsed, "path": path}
    except Exception as e:
        return {"error": str(e), "path": path}


def hbot_get(path: str) -> Dict[str, Any]:
    return hbot_request("GET", path)


def hbot_post(path: str, body: Any = None) -> Dict[str, Any]:
    return hbot_request("POST", path, body if body is not None else {})


def run_hbot_cli(args: List[str], timeout: float = 90.0, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run host `hbot` CLI against the Docker install. Returns parsed JSON when possible."""
    if not os.path.isfile(HBOT_BIN) and not shutil.which(HBOT_BIN):
        return {
            "ok": False,
            "error": f"hbot CLI not found at {HBOT_BIN}",
            "hint": "Run: export PATH=\"$HOME/.local/bin:$PATH\"",
        }
    cmd = [HBOT_BIN, *args]
    env = os.environ.copy()
    env["HBOT_PASSWORD"] = HBOT_PASSWORD
    env["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + env.get("PATH", "")
    env["HBOT_PREFER"] = "docker"
    env["HBOT_CONTAINER"] = "hummingbot"
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"hbot timed out after {timeout}s", "cmd": cmd}
    except Exception as e:
        return {"ok": False, "error": str(e), "cmd": cmd}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parsed: Any = None
    # Prefer JSON lines / full JSON stdout
    for blob in (out, err):
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
            break
        except Exception:
            # try last JSON object in output
            start = blob.find("{")
            end = blob.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(blob[start : end + 1])
                    break
                except Exception:
                    pass

    ok = proc.returncode == 0
    # Some hbot commands print markdown success without json
    if parsed is None:
        lower = (out + "\n" + err).lower()
        if ok and ("error" not in lower or "no error" in lower):
            parsed = {"raw": out or err, "returncode": proc.returncode}
        else:
            parsed = {"raw": out or err, "returncode": proc.returncode}

    return {
        "ok": ok,
        "returncode": proc.returncode,
        "cmd": cmd,
        "stdout": out[-4000:],
        "stderr": err[-2000:],
        "result": parsed,
    }



_e4_status_cache = (0.0, {})

def e4_bot_status() -> Dict[str, Any]:
    """Read-only status of hummingbot-e4 (PMM). Never start/stop it."""
    global _e4_status_cache
    now = time.time()
    ts, cached = _e4_status_cache
    if cached and (now - ts) < 3.0:
        return cached
    out: Dict[str, Any] = {
        "available": False,
        "running": False,
        "orders": [],
        "slots_used": 0,
        "container": "hummingbot-e4",
    }
    env = os.environ.copy()
    env["HBOT_PASSWORD"] = HBOT_PASSWORD
    env["HBOT_CONTAINER"] = "hummingbot-e4"
    env["HBOT_PREFER"] = "docker"
    env["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            [HBOT_BIN, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        blob = (proc.stdout or "").strip()
        data: Dict[str, Any] = {}
        if blob.startswith("{"):
            data = json.loads(blob)
        else:
            start, end = blob.find("{"), blob.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(blob[start : end + 1])
        running = bool(data.get("running"))
        fs = str(data.get("format_status") or "")
        orders = []
        pairs = set()
        for line in fs.splitlines():
            if "BTC-USDT" in line or "ETH-USDT" in line:
                parts = line.split()
                # Exchange Market Side Price Amount Age
                if "BTC-USDT" in parts or "ETH-USDT" in parts:
                    try:
                        i = parts.index("BTC-USDT") if "BTC-USDT" in parts else parts.index("ETH-USDT")
                        pair = parts[i]
                        side = parts[i + 1] if i + 1 < len(parts) else ""
                        px = parts[i + 2] if i + 2 < len(parts) else ""
                        amt = parts[i + 3] if i + 3 < len(parts) else ""
                        if side.lower() in ("buy", "sell"):
                            base = pair.replace("-USDT", "")
                            orders.append(f"{base} {side.upper()} {amt}@{px}")
                            pairs.add(pair)
                    except Exception:
                        pass
        out.update({
            "available": True,
            "running": running,
            "orders": orders[:6],
            "slots_used": len(pairs),
        })
    except Exception as e:
        out["error"] = str(e)[:180]
    _e4_status_cache = (now, out)
    return out



E4_ENV = {"HBOT_CONTAINER": "hummingbot-e4", "HBOT_PREFER": "docker"}
E4_CONFIG = "conf_e4_pmm.yml"
_e4_op_lock = threading.Lock()
_e4_op = {"phase": "idle", "started_at": 0.0}


def e4_bot_start(replace: bool = False) -> Dict[str, Any]:
    """Start hummingbot-e4 PMM only. Never touches Engine 1."""
    pre = e4_bot_status()
    if pre.get("running") and not replace:
        return {
            "ok": True,
            "running": True,
            "message": "Engine 4 already RUNNING",
            "status": pre,
        }
    with _e4_op_lock:
        if _e4_op.get("phase") == "starting":
            return {"ok": True, "phase": "starting", "message": "Engine 4 start already in progress"}
        _e4_op["phase"] = "starting"
        _e4_op["started_at"] = time.time()
    try:
        imp = run_hbot_cli(["import", E4_CONFIG], timeout=45.0, extra_env=E4_ENV)
        if not imp.get("ok"):
            imp = run_hbot_cli(["import", E4_CONFIG.replace(".yml", "")], timeout=45.0, extra_env=E4_ENV)
        if not imp.get("ok"):
            return {
                "ok": False,
                "phase": "import",
                "error": "Failed to import Engine 4 script config",
                "import": {"ok": imp.get("ok"), "stdout": (imp.get("stdout") or "")[-400:]},
                "hint": "conf lives at hummingbot/e4/conf/scripts/conf_e4_pmm.yml",
            }
        start_args = ["start", "--json", "--timeout", "180"]
        if replace:
            start_args.append("--replace")
        st = run_hbot_cli(start_args, timeout=200.0, extra_env=E4_ENV)
        global _e4_status_cache
        _e4_status_cache = (0.0, {})
        status: Dict[str, Any] = {}
        for _ in range(12):
            time.sleep(2)
            status = e4_bot_status()
            if status.get("running"):
                break
        running = bool(status.get("running"))
        stdout_l = str(st.get("stdout") or "").lower()
        soft_ok = ("running" in stdout_l) or ("started" in stdout_l) or ("pid" in stdout_l)
        ok = running or bool(st.get("ok")) or soft_ok
        return {
            "ok": ok,
            "running": running,
            "phase": "start" if running else ("starting" if ok else "start"),
            "import": {"ok": imp.get("ok")},
            "start": {"ok": st.get("ok"), "stdout": (st.get("stdout") or "")[-400:]},
            "status": status,
            "message": "Engine 4 RUNNING" if running else ("Engine 4 starting" if ok else "Engine 4 did not start"),
        }
    finally:
        with _e4_op_lock:
            _e4_op["phase"] = "idle"
        _e4_status_cache = (0.0, {})


def e4_bot_stop() -> Dict[str, Any]:
    """Stop hummingbot-e4 only. Quotes cancel; inventory kept. Never stop Engine 1."""
    with _e4_op_lock:
        _e4_op["phase"] = "stopping"
    try:
        st = run_hbot_cli(["stop", "--json"], timeout=90.0, extra_env=E4_ENV)
        time.sleep(2.0)
        global _e4_status_cache
        _e4_status_cache = (0.0, {})
        status = e4_bot_status()
        running = bool(status.get("running"))
        return {
            "ok": not running,
            "running": running,
            "keep_positions": True,
            "stop": {"ok": st.get("ok"), "stdout": (st.get("stdout") or "")[-400:]},
            "status": status,
            "message": (
                "Engine 4 STOPPED — quotes cancelled, inventory kept. Engine 1 untouched."
                if not running else "Engine 4 stop requested but still reports running"
            ),
        }
    finally:
        with _e4_op_lock:
            _e4_op["phase"] = "idle"
        _e4_status_cache = (0.0, {})


def cli_bot_status(force: bool = False) -> Dict[str, Any]:
    """Authoritative bot status via `hbot status --json` (cached ~2.5s)."""
    global _status_cache
    now = time.time()
    ts, cached = _status_cache
    if not force and cached and (now - ts) < STATUS_TTL:
        # Overlay live start/stop phase so UI can show STARTING without clobbering
        out = dict(cached)
        with _bot_op_lock:
            phase = _bot_op.get("phase") or "idle"
            out["op_phase"] = phase
            if phase == "starting" and not out.get("running"):
                out["status"] = "STARTING"
                out["note"] = out.get("note") or "starting"
            elif phase == "stopping" and out.get("running"):
                out["status"] = "STOPPING"
                out["note"] = out.get("note") or "stopping"
        return out

    r = run_hbot_cli(["status", "--json"], timeout=30.0)
    data = r.get("result") if isinstance(r.get("result"), dict) else {}
    running = bool(data.get("running")) if data else False
    # Also merge API orchestration view (fast; don't let it block status forever)
    api = hbot_request("GET", "/bot-orchestration/status", timeout=5.0)
    api_bot = {}
    if isinstance(api, dict):
        payload = api.get("data") or {}
        api_bot = payload.get(BOT_NAME) or payload.get("hummingbot") or {}
    status = "RUNNING" if running else "STOPPED"
    note = data.get("note") or ""
    with _bot_op_lock:
        phase = _bot_op.get("phase") or "idle"
    # Prefer CLI truth — API orchestration often stuck on "stopped" for CLI bots
    if running:
        if not note or str(note).lower() in ("stopped", "stop", "false"):
            note = "running"
        status = "RUNNING"
        if phase == "stopping":
            status = "STOPPING"
            note = "stopping"
    else:
        if not note:
            note = api_bot.get("status") or ""
        if phase == "starting":
            status = "STARTING"
            note = note or "starting"
        elif phase == "stopping":
            status = "STOPPING"
            note = note or "stopping"
    result = {
        "ok": r.get("ok", False) or data is not None,
        "running": running,
        "status": status,
        "note": note,
        "config": data.get("config"),
        "type": data.get("type"),
        "next": data.get("next"),
        "op_phase": phase,
        "cli": data,
        "api": api_bot,
        "raw": r if not data else None,
    }
    _status_cache = (now, result)
    return result


def _invalidate_status_cache() -> None:
    global _status_cache
    _status_cache = (0.0, {})


def set_all_leverage() -> Dict[str, Any]:
    """Set leverage for all pairs in the universe before starting."""
    results = {}
    for pair, lev in LEV_MAP.items():
        try:
            r = hbot_post(f"/trading/master_account/bitget_perpetual/leverage", {
                "trading_pair": pair,
                "leverage": int(lev),
            })
            results[pair] = {"leverage": lev, "ok": r.get("status") == "success"}
        except Exception as e:
            results[pair] = {"leverage": lev, "ok": False, "error": str(e)}
    ok_count = sum(1 for v in results.values() if v.get("ok"))
    print(f"[leverage] Set {ok_count}/{len(results)} pairs")
    return results


def cli_bot_start(config: str = BOT_CONFIG, replace: bool = False) -> Dict[str, Any]:
    """
    Real start path:
      1) Set leverage for all pairs
      2) hbot import <config> --controller
      3) hbot start --json [--replace]
    API start-bot alone only ACKs MQTT and leaves the bot stopped when no strategy is loaded.
    """
    config = (config or BOT_CONFIG).replace(".yml", "") + ".yml"
    # Ensure controller module is importable (correct package path)
    ensure_controller_installed()

    # Already running? Don't kill+restart unless replace explicitly requested.
    pre = cli_bot_status(force=True)
    if pre.get("running") and not replace:
        return {
            "ok": True,
            "phase": "start",
            "message": "Bot already RUNNING",
            "status": pre,
            "config": config,
        }

    # Set leverage for all pairs before starting (non-fatal if some fail)
    try:
        set_all_leverage()
    except Exception as e:
        print(f"[leverage] set_all_leverage error (continuing): {e}")

    imp = run_hbot_cli(["import", config, "--controller"], timeout=45.0)
    if not imp.get("ok"):
        # try without extension variants
        alt = config.replace(".yml", "")
        imp2 = run_hbot_cli(["import", alt, "--controller"], timeout=45.0)
        if imp2.get("ok"):
            imp = imp2
        else:
            _invalidate_status_cache()
            return {
                "ok": False,
                "phase": "import",
                "error": "Failed to import strategy config",
                "import": imp,
                "hint": (
                    "Controller must live at controllers/generic/v37_scalp_multi.py "
                    "and conf at conf/controllers/conf_v37_scalp_multi.yml"
                ),
            }

    start_args = ["start", "--json", "--timeout", "180"]
    if replace:
        start_args.append("--replace")
    # start uses currently imported config when file omitted
    st = run_hbot_cli(start_args, timeout=200.0)
    _invalidate_status_cache()
    # verify — poll up to ~60s; 24-pair Bitget init is slow under rate limits
    status: Dict[str, Any] = {}
    for _ in range(20):
        time.sleep(3)
        status = cli_bot_status(force=True)
        if status.get("running"):
            break
    running = bool(status.get("running"))
    # CLI may time out while bot is still coming up inside the container.
    # If start stdout looks successful OR process is running, treat as ok.
    cli_ok = bool(st.get("ok"))
    stdout_l = str(st.get("stdout") or "").lower()
    soft_ok = ("running" in stdout_l) or ("started" in stdout_l) or ("pid" in stdout_l)
    ok = running or (cli_ok and soft_ok)
    if running:
        msg = "Bot RUNNING"
    elif cli_ok or soft_ok:
        # Still initializing order books — frontend should keep polling
        ok = True
        msg = "Bot start issued — still initializing markets (poll status)"
    else:
        msg = (
            f"Start did not stay running: "
            f"{status.get('note') or st.get('stderr') or st.get('stdout') or st.get('error') or 'unknown'}"
        )
    return {
        "ok": ok,
        "phase": "start" if running else ("starting" if ok else "start"),
        "import": {"ok": imp.get("ok"), "stdout": imp.get("stdout"), "result": imp.get("result")},
        "start": st,
        "status": status,
        "config": config,
        "running": running,
        "message": msg,
    }


def cli_bot_stop() -> Dict[str, Any]:
    """
    Stop the bot WITHOUT force-closing exchange positions.

    Positions stay open on Bitget. The strategy script
    (scripts/v2_with_controllers.py) is patched so on_stop sends
    StopExecutorAction(keep_position=True) before the orchestrator
    tears down executors. API stop also uses skip_order_cancellation.
    """
    st = run_hbot_cli(["stop", "--json"], timeout=90.0)
    time.sleep(2.0)
    _invalidate_status_cache()
    status = cli_bot_status(force=True)
    # Skip order cancellation so positions stay open on the exchange
    api = hbot_post("/bot-orchestration/stop-bot", {
        "bot_name": BOT_NAME,
        "skip_order_cancellation": True,
        "async_backend": True,
    })
    # Invalidate journal/perf cache so next poll picks up POSITION_HOLD rows
    global _journal_last_fetch, _perf_last_fetch
    _journal_last_fetch = 0.0
    _perf_last_fetch = 0.0
    return {
        "ok": not bool(status.get("running")),
        "stop": st,
        "api_stop": api,
        "status": status,
        "keep_positions": True,
        "message": (
            "Bot STOPPED — exchange positions KEPT OPEN"
            if not status.get("running")
            else "Stop requested but bot still reports running"
        ),
    }


def ensure_controller_installed() -> Dict[str, Any]:
    """Copy v37_scalp controller into controllers/directional_trading if missing."""
    candidates_src = [
        os.path.expanduser("~/Desktop/humming-bot/controllers/v37_scalp.py"),
        os.path.expanduser("~/Desktop/humming-bot/hummingbot/controllers/v37_scalp.py"),
        os.path.expanduser("~/Desktop/humming-bot/hummingbot/controllers/directional_trading/v37_scalp.py"),
    ]
    dest = os.path.expanduser(
        "~/Desktop/humming-bot/hummingbot/controllers/directional_trading/v37_scalp.py"
    )
    src = next((p for p in candidates_src if os.path.isfile(p)), None)
    result = {"dest": dest, "src": src, "copied": False}
    if not src:
        result["error"] = "v37_scalp.py source not found"
        return result
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Always refresh from canonical project controllers/ copy if present
        canonical = os.path.expanduser("~/Desktop/humming-bot/controllers/v37_scalp.py")
        use = canonical if os.path.isfile(canonical) else src
        with open(use, "rb") as f:
            data = f.read()
        # write if missing or different
        write = True
        if os.path.isfile(dest):
            with open(dest, "rb") as f:
                if f.read() == data:
                    write = False
        if write:
            with open(dest, "wb") as f:
                f.write(data)
            result["copied"] = True
        result["src"] = use
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    return result


def bitget_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"https://api.bitget.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "hbot-dashboard/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "code": "ERR"}


def get_candles(symbol: str, granularity: str = "15m", limit: int = 100) -> List[List[str]]:
    sym = bitget_symbol(symbol)
    key = f"{sym}:{granularity}:{limit}"
    now = time.time()
    with _lock:
        ts, cached = _candle_cache.get(key, (0.0, []))
        if cached and now - ts < CANDLE_TTL:
            return cached
    data = bitget_get("/api/v2/mix/market/candles", {
        "symbol": sym,
        "productType": "USDT-FUTURES",
        "granularity": granularity,
        "limit": str(limit),
    })
    candles = data.get("data") or []
    if candles:
        with _lock:
            _candle_cache[key] = (now, candles)
    return candles


def get_tickers() -> Dict[str, Dict[str, Any]]:
    global _ticker_cache
    now = time.time()
    ts, cached = _ticker_cache
    if cached and now - ts < TICKER_TTL:
        return cached

    data = bitget_get("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"})
    out: Dict[str, Dict[str, Any]] = {}
    rows = data.get("data") or []
    wanted = {bitget_symbol(s): s for s in SYMBOLS}
    for row in rows:
        sym_bg = row.get("symbol") or ""
        if sym_bg not in wanted:
            continue
        pair = wanted[sym_bg]
        try:
            last = float(row.get("lastPr") or 0)
            chg = float(row.get("change24h") or 0)
            # Bitget change24h can be fraction or percent depending on product
            if abs(chg) <= 1 and chg != 0:
                chg *= 100
            high = float(row.get("high24h") or 0)
            low = float(row.get("low24h") or 0)
            vol = float(row.get("baseVolume") or row.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            last, chg, high, low, vol = 0.0, 0.0, 0.0, 0.0, 0.0
        out[pair] = {
            "symbol": pair,
            "bitget": sym_bg,
            "last_price": last,
            "change_24h": chg,
            "high_24h": high,
            "low_24h": low,
            "volume": vol,
            "tier": TIER_MAP.get(pair, 3),
            "leverage": LEV_MAP.get(pair, 10),
        }
    if out:
        _ticker_cache = (now, out)
    return out or cached


def _series(candles: List) -> Tuple[List[float], List[float], List[float], List[float]]:
    # Bitget returns newest-first
    ordered = list(reversed(candles))
    closes = [float(c[4]) for c in ordered]
    highs = [float(c[2]) for c in ordered]
    lows = [float(c[3]) for c in ordered]
    volumes = [float(c[5]) for c in ordered]
    return closes, highs, lows, volumes


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _roc(closes: List[float], period: int) -> float:
    if len(closes) <= period:
        return 0.0
    base = closes[-(period + 1)]
    if base == 0:
        return 0.0
    return ((closes[-1] - base) / base) * 100.0


def _ema(closes: List[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    # simple SMA seed then EMA
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _bb(closes: List[float], period: int = 20, mult: float = 2.0) -> Tuple[float, float, float, str]:
    if len(closes) < period:
        c = closes[-1]
        return c, c, c, "Mid"
    window = closes[-period:]
    ma = sum(window) / period
    var = sum((x - ma) ** 2 for x in window) / period
    std = var ** 0.5
    upper = ma + mult * std
    lower = ma - mult * std
    price = closes[-1]
    if price < lower:
        pos = "Lower"
    elif price > upper:
        pos = "Upper"
    else:
        pos = "Mid"
    return ma, upper, lower, pos


def _fmt_price(p: float) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.4f}"
    return f"${p:.6f}"


def _component(name: str, label: str, ok: bool, value: str, weight: float = 1.0) -> Dict[str, Any]:
    return {"name": name, "label": label, "ok": bool(ok), "value": value, "weight": weight}


def compute_strategy_signal(strategy: str, symbol: str,
                            m15: Optional[List] = None,
                            h1: Optional[List] = None) -> Dict[str, Any]:
    pair = normalize_symbol(symbol)
    strat = (strategy or "ROC_RSI").upper()
    m15 = m15 if m15 is not None else get_candles(pair, "15m", 100)
    h1 = h1 if h1 is not None else get_candles(pair, "1H", 60)
    # Match controller: drop the FORMING candle.
    # Bitget mix candles are newest-first, so index 0 is the live bar.
    # [:-1] would drop the OLDEST bar and keep the forming one — wrong.
    if m15 and len(m15) >= 2:
        m15 = m15[1:]
    if h1 and len(h1) >= 2:
        h1 = h1[1:]

    if len(m15) < 32:
        return {
            "strategy": strat, "symbol": pair, "direction": "NEUTRAL",
            "score": 0.0, "composite_score": 0.0, "strength": "NONE",
            "passing": 0, "total": 0, "ready": False, "components": [],
            "price": 0.0,
        }

    closes, highs, lows, volumes = _series(m15)
    h1_closes = _series(h1)[0] if h1 and len(h1) >= 10 else closes
    price = closes[-1]
    rsi = _rsi(closes, 14)
    roc5 = _roc(closes, 5)
    roc8 = _roc(closes, 8)
    h1_roc5 = _roc(h1_closes, 5)
    ema21 = _ema(closes, 21)
    bb_ma, bb_up, bb_lo, bb_pos = _bb(closes, 20, 2.0)
    vol_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else (volumes[-1] or 1)
    vol_ratio = (volumes[-1] / vol_avg) if vol_avg > 0 else 1.0

    components: List[Dict[str, Any]] = []
    direction = "NEUTRAL"

    if strat == "SUPER_A":
        # Momentum alignment M15 ROC8 + H1 ROC5 + RSI/EMA/agreement
        combined = abs(roc8) + abs(h1_roc5)
        roc_m_ok = abs(roc8) >= 0.15
        roc_h_ok = abs(h1_roc5) >= 0.20
        agree = (roc8 > 0 and h1_roc5 > 0) or (roc8 < 0 and h1_roc5 < 0)
        rsi_ok = (roc8 > 0 and rsi < 70) or (roc8 < 0 and rsi > 30)
        ema_ok = (roc8 > 0 and price > ema21) or (roc8 < 0 and price < ema21)
        comb_ok = combined >= 0.40
        components = [
            _component("roc_m15", "ROC M15(8)", roc_m_ok, f"{roc8:.2f}%"),
            _component("roc_h1", "ROC H1(5)", roc_h_ok, f"{h1_roc5:.2f}%"),
            _component("agree", "M15↔H1 Agree", agree, "YES" if agree else "NO"),
            _component("rsi", "RSI(14)", rsi_ok, f"{rsi:.1f}"),
            _component("ema", "EMA(21)", ema_ok, _fmt_price(ema21)),
            _component("comb", "Combined ROC", comb_ok, f"{combined:.2f}"),
        ]
        if agree and roc8 > 0 and rsi < 70:
            direction = "LONG"
        elif agree and roc8 < 0 and rsi > 30:
            direction = "SHORT"
        min_pass = int(_strategies["SUPER_A"].get("min_components", 5))

    elif strat == "BB_VOL":
        vol_ok = vol_ratio >= 1.3
        long_setup = bb_pos == "Lower" and vol_ok
        short_setup = bb_pos == "Upper" and vol_ok
        bb_ok = bb_pos in ("Lower", "Upper")
        rsi_ok = (bb_pos == "Lower" and rsi < 45) or (bb_pos == "Upper" and rsi > 55) or bb_pos == "Mid"
        bounce_ok = long_setup or short_setup
        components = [
            _component("bb", "BB Position", bb_ok, bb_pos),
            _component("vol", "Vol Ratio", vol_ok, f"{vol_ratio:.2f}x"),
            _component("rsi", "RSI(14)", rsi_ok, f"{rsi:.1f}"),
            _component("setup", "BB+Vol Setup", bounce_ok, "YES" if bounce_ok else "NO"),
            _component("roc", "ROC(5)", abs(roc5) > 0.05, f"{roc5:.2f}%"),
        ]
        if long_setup:
            direction = "LONG"
        elif short_setup:
            direction = "SHORT"
        min_pass = int(_strategies["BB_VOL"].get("min_components", 3))

    else:  # ROC_RSI — momentum + RSI timing (not mean-reversion)
        roc_ok = abs(roc5) >= 0.20
        rsi_long = roc5 > 0.20 and 40.0 <= rsi < 70.0
        rsi_short = roc5 < -0.20 and 30.0 < rsi <= 60.0
        rsi_ok = rsi_long or rsi_short
        ema_ok = (roc5 > 0 and price > ema21) or (roc5 < 0 and price < ema21)
        vol_ok = vol_ratio >= 1.0
        components = [
            _component("roc", "ROC(5)", roc_ok, f"{roc5:.2f}%"),
            _component("rsi", "RSI timing", rsi_ok, f"{rsi:.1f}"),
            _component("ema", "EMA(21)", ema_ok, _fmt_price(ema21)),
            _component("bb", "BB Position", bb_pos != "Mid" or abs(roc5) >= 0.3, bb_pos),
            _component("vol", "Vol Ratio", vol_ok, f"{vol_ratio:.2f}x"),
        ]
        if rsi_long:
            direction = "LONG"
        elif rsi_short:
            direction = "SHORT"
        min_pass = int(_strategies.get("ROC_RSI", {}).get("min_components", 2))

    passing = sum(1 for c in components if c["ok"])
    total = len(components) or 1
    ready = direction in ("LONG", "SHORT") and passing >= min_pass
    score = passing / total
    # signed composite for scanner coloring
    signed = score if direction == "LONG" else (-score if direction == "SHORT" else 0.0)
    if ready:
        strength = "STRONG" if score >= 0.8 else "MODERATE"
    else:
        strength = "WEAK" if score >= 0.4 else "NONE"

    return {
        "strategy": strat,
        "symbol": pair,
        "direction": direction,
        "score": round(score, 3),
        "composite_score": round(signed, 3),
        "strength": strength,
        "passing": passing,
        "total": total,
        "ready": ready,
        "components": components,
        "price": price,
        "roc": round(roc5, 4),
        "rsi": round(rsi, 2),
        "bb_pos": bb_pos,
        "vol_ratio": round(vol_ratio, 3),
        "ema21": ema21,
    }


def update_persistence(sig: Dict[str, Any]) -> Dict[str, Any]:
    """Track consecutive ready cycles per strategy:symbol."""
    sk = sig.get("strategy") or ""
    sym = normalize_symbol(sig.get("symbol") or "")
    key = f"{sk}:{sym.replace('-', '')}"
    direction = sig.get("direction") or "NEUTRAL"
    ready = bool(sig.get("ready"))
    now = time.time()
    with _lock:
        prev = _persist.get(key)
        if ready and direction in ("LONG", "SHORT"):
            if prev and prev.get("direction") == direction:
                prev["count"] = int(prev.get("count") or 0) + 1
                prev["last_seen"] = now
            else:
                _persist[key] = {
                    "direction": direction,
                    "count": 1,
                    "first_seen": now,
                    "last_seen": now,
                    "strategy": sk,
                    "symbol": sym,
                }
        else:
            # decay / reset on not ready
            if prev:
                prev["count"] = 0
                prev["direction"] = direction
                prev["last_seen"] = now
            else:
                _persist[key] = {
                    "direction": direction, "count": 0,
                    "first_seen": now, "last_seen": now,
                    "strategy": sk, "symbol": sym,
                }
        conf = dict(_persist.get(key) or {})
    sig = dict(sig)
    sig["confirm_count"] = int(conf.get("count") or 0)
    conf_need = int((_strategies.get(sk) or {}).get("confirm_cycles") or 2)
    sig["confirm_need"] = conf_need
    return sig


def _h1_trend_ok(h1_closes: List[float], signal: int) -> bool:
    """Copy of live controller H1 filter: longs need EMA12>EMA26;
    shorts need EMA12<EMA26 AND close < EMA21."""
    if not h1_closes or len(h1_closes) < 30 or signal == 0:
        return False
    try:
        h1_fast = _ema(h1_closes, 12)
        h1_slow = _ema(h1_closes, 26)
        h1_ema21 = _ema(h1_closes, 21)
        h1_last = float(h1_closes[-1])
        h1_bullish = h1_fast > h1_slow
        h1_bearish_strong = (h1_fast < h1_slow) and (h1_last < h1_ema21)
        if signal > 0:
            return h1_bullish
        return h1_bearish_strong
    except Exception:
        return False


def _combined_engine_gate(pair_sigs: Dict[str, Dict[str, Any]],
                          h1_closes: List[float]) -> Tuple[bool, float, str, Dict[str, int]]:
    """Fallback READY gate matching live v37_scalp_multi:
    ≥2 engines in the same direction, then H1 trend filter.
    Bot-truth overlay still wins the UI when the bot is running."""
    votes: Dict[str, int] = {}
    for sk, sig in (pair_sigs or {}).items():
        d = str((sig or {}).get("direction") or "NEUTRAL").upper()
        if d == "LONG":
            votes[sk] = 1
        elif d == "SHORT":
            votes[sk] = -1
        else:
            votes[sk] = 0
    vals = [v for v in votes.values() if v != 0]
    if len(vals) < 2:
        return False, 0.0, "NEUTRAL", votes
    if all(v > 0 for v in vals):
        signal = 1
    elif all(v < 0 for v in vals):
        signal = -1
    else:
        return False, 0.0, "NEUTRAL", votes
    if not _h1_trend_ok(h1_closes, signal):
        return False, 0.0, "NEUTRAL", votes
    direction = "LONG" if signal > 0 else "SHORT"
    agree_n = sum(1 for v in votes.values() if v == signal)
    total = max(len(votes), 1)
    roc = 0.0
    for sig in (pair_sigs or {}).values():
        try:
            roc = float((sig or {}).get("roc") or 0)
            break
        except (TypeError, ValueError):
            pass
    score = min(1.0, (agree_n / total) + min(abs(roc) / 2.0, 0.25))
    return True, float(score), direction, votes


def overlay_bot_truth(scan: Dict[str, Any]) -> Dict[str, Any]:
    """Drive scan payload from /api/bot/signals when the bot is live.
    REPLACE candidates even when the bot list is empty — never keep scan READY."""
    try:
        parsed = parse_bot_signals()
    except Exception:
        parsed = {"available": False}
    scan = dict(scan or {})
    if not parsed.get("available"):
        scan["bot_truth"] = False
        return scan
    scan["bot_truth"] = True
    scan["bot_signals_meta"] = {
        "enabled": parsed.get("enabled") or [],
        "score_threshold": parsed.get("score_threshold"),
        "open_pairs": parsed.get("open_pairs") or [],
    }
    bot_cands = list(parsed.get("candidates") or [])
    # Always replace — empty bot list means NONE, not leftover scanner READY
    scan["candidates"] = bot_cands
    if parsed.get("slots"):
        slots = dict(parsed["slots"])
        used = int(slots.get("used") or 0)
        mx = int(slots.get("max") or MAX_OPEN_POSITIONS or 3)
        slots["used"] = used
        slots["max"] = mx
        slots["free"] = max(0, mx - used)
        slots["over"] = used > mx
        scan["slots"] = slots
    scan["bot_open_pairs"] = list(parsed.get("open_pairs") or [])
    bot_map = {c.get("symbol"): c for c in bot_cands if c.get("symbol")}
    for m in scan.get("market") or []:
        b = bot_map.get(m.get("symbol"))
        if b:
            if b.get("direction"):
                m["direction"] = b["direction"]
            if b.get("score") is not None:
                m["score"] = b["score"]
            m["ready"] = bool(b.get("ready"))
            strats = b.get("strategies") or []
            if strats:
                m["strategy"] = strats[0]
                m["strategies"] = strats
            m["has_position"] = bool(b.get("has_position"))
        else:
            m["ready"] = False
    return scan



def build_scan(force: bool = False) -> Dict[str, Any]:
    global _scan_cache
    now = time.time()
    ts, cached = _scan_cache
    if not force and cached and now - ts < SCAN_TTL:
        return cached

    tickers = get_tickers()
    enabled = [k for k, v in _strategies.items() if v.get("active")]
    if not enabled:
        enabled = list(_strategies.keys())

    by_strategy: Dict[str, Dict[str, Any]] = {k: {} for k in enabled}
    best_by_symbol: Dict[str, Any] = {}
    flat: List[Dict[str, Any]] = []
    market: List[Dict[str, Any]] = []

    for pair in SYMBOLS:
        m15 = get_candles(pair, "15m", 100)
        h1 = get_candles(pair, "1H", 50)
        t = tickers.get(pair) or {
            "symbol": pair, "last_price": 0, "change_24h": 0,
            "tier": TIER_MAP.get(pair, 3), "leverage": LEV_MAP.get(pair, 10),
        }
        best = None
        for sk in enabled:
            sig = compute_strategy_signal(sk, pair, m15=m15, h1=h1)
            sig = update_persistence(sig)
            by_strategy[sk][pair] = sig
            flat.append(sig)
            if best is None:
                best = sig
            else:
                # prefer ready, then higher passing, then abs score
                def rank(s):
                    return (
                        1 if s.get("ready") else 0,
                        int(s.get("confirm_count") or 0),
                        int(s.get("passing") or 0),
                        abs(float(s.get("composite_score") or 0)),
                    )
                if rank(sig) > rank(best):
                    best = sig
        if best:
            best_by_symbol[pair] = best
        market.append({
            **t,
            "score": best.get("composite_score") if best else 0,
            "direction": best.get("direction") if best else "NEUTRAL",
            "strategy": best.get("strategy") if best else None,
            "ready": bool(best.get("ready")) if best else False,
            "passing": best.get("passing") if best else 0,
            "total": best.get("total") if best else 0,
            "leverage": LEV_MAP.get(pair, t.get("leverage") or 5),
            "tier": TIER_MAP.get(pair, t.get("tier") or 3),
            "asset_class": "commodity" if pair in COMMODITY_SYMBOLS else ("stock" if pair in STOCK_SYMBOLS else "crypto"),
        })

    # Fallback READY: copy live 2-engine gate + H1 filter so the dashboard
    # does not glow READY on a solo engine. Bot overlay still replaces this.
    for m in market:
        pair = m.get("symbol")
        votes_map = {sk: by_strategy.get(sk, {}).get(pair) for sk in enabled}
        votes_map = {k: v for k, v in votes_map.items() if v}
        # H1 closes: cached candles, newest-first -> drop forming -> reverse
        try:
            raw_h1 = get_candles(pair, "1H", 50) if pair else []
            if raw_h1 and len(raw_h1) >= 2:
                raw_h1 = raw_h1[1:]
            h1_closes = _series(raw_h1)[0] if raw_h1 and len(raw_h1) >= 10 else []
        except Exception:
            h1_closes = []
        gated, gscore, gdir, votes = _combined_engine_gate(votes_map, h1_closes)
        m["engine_votes"] = votes
        m["ready"] = bool(gated)
        if gated:
            m["direction"] = gdir
            m["score"] = round(gscore if gdir == "LONG" else -gscore, 3)
        else:
            # keep per-strat score for coloring but never READY
            m["ready"] = False

    # Sort market by abs score desc
    market.sort(key=lambda m: (1 if m.get("ready") else 0, abs(float(m.get("score") or 0))), reverse=True)

    # Best opportunities for open slots (ready first)
    candidates = [m for m in market if m.get("ready")]
    candidates = sorted(
        candidates,
        key=lambda m: abs(float(m.get("score") or 0)),
        reverse=True,
    )[:MAX_OPEN_POSITIONS]

    # live open count — use EXCHANGE positions (not executors API which can be empty).
    # Same source as the Open Positions panel: hbot API /trading/positions.
    open_count = 0
    try:
        pos_resp = hbot_request("POST", "/trading/positions", {
            "account_names": ["master_account"],
            "connector_names": ["bitget_perpetual"],
        }, timeout=6.0)
        pos_list = (pos_resp.get("data") or []) if isinstance(pos_resp, dict) else []
        open_count = len(pos_list)
    except Exception:
        # fallback to executors if positions API fails
        try:
            ex = hbot_request("GET", "/executors/summary", timeout=4.0)
            if isinstance(ex, dict) and "error" not in ex:
                open_count = int(ex.get("total_active") or 0)
        except Exception:
            open_count = 0
    # Honest slots: never clamp used to max (4/3 OVER must show as 4/3)
    free_slots = max(0, MAX_OPEN_POSITIONS - open_count)

    out = {
        "ts": now,
        "enabled_strategies": enabled,
        "strategies": _strategies,
        "market": market,
        "strategy_signals_by_strategy": by_strategy,
        "strategy_signals": best_by_symbol,
        "signal_persistence": dict(_persist),
        "flat": flat,
        "candidates": candidates,
        "slots": {
            "used": open_count,
            "max": MAX_OPEN_POSITIONS,
            "free": free_slots,
            "over": open_count > MAX_OPEN_POSITIONS,
        },
        "universe": {
            "symbols": SYMBOLS,
            "crypto": CRYPTO_SYMBOLS,
            "commodities": COMMODITY_SYMBOLS,
            "stocks": STOCK_SYMBOLS,
            "leverage_map": LEV_MAP,
            "tier_map": TIER_MAP,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "position_size_quote": _UNIVERSE.get("position_size_quote", 10),
            "config": BOT_CONFIG,
        },
    }
    out = overlay_bot_truth(out)
    _scan_cache = (now, out)
    return out


def parse_bot_signals() -> Dict[str, Any]:
    """Parse the BOT's REAL live signals from `hbot status` format_status.

    This is the authoritative source — it reflects what v37_scalp_multi is
    actually computing each tick (candidates, scores, direction, READY slots,
    open pairs, enabled strategies). The dashboard should PREFER this over
    its own `compute_strategy_signal()` (which diverges from the controller).

    Returns structured dict, or an `available: False` marker when the bot is
    stopped so callers know to fall back to the local scanner.
    """
    try:
        status = cli_bot_status()
    except Exception:
        return {"available": False, "reason": "status_error"}
    if not status.get("running"):
        return {"available": False, "reason": "bot_stopped"}

    fs = ""
    try:
        fs = (status.get("cli") or {}).get("format_status") or ""
    except Exception:
        fs = ""
    if not fs and isinstance(status.get("raw"), dict):
        fs = str((status.get("raw") or {}).get("result") or {}).get("format_status") if status.get("raw") else ""
    if not fs:
        return {"available": False, "reason": "no_format_status"}

    out: Dict[str, Any] = {"available": True, "raw": fs}

    # --- slots: "Universe: 24 pairs · slots 2/2" or "slots 2/2" ---
    import re as _re
    m = _re.search(r"slots\s+(\d+)/(\d+)", fs)
    if m:
        used_n, max_n = int(m.group(1)), int(m.group(2))
        out["slots"] = {"used": used_n, "max": max_n, "free": max(0, max_n - used_n), "over": used_n > max_n}

    # --- enabled strategies: "Strategies ON: BB_VOL, ROC_RSI, SUPER_A · thr≥0.55 ---"
    m = _re.search(r"Strategies ON:\s*([^\n·]*)", fs)
    if m:
        out["enabled"] = [s.strip() for s in m.group(1).split(",") if s.strip()]

    # --- open pairs: "Open: CL-USDT, LINK-USDT" ---
    m = _re.search(r"Open:\s*([^\n]*)", fs)
    if m:
        opened = [s.strip() for s in m.group(1).split(",") if s.strip() and s.strip() != "—"]
        out["open_pairs"] = opened

    # --- entry threshold: "Strategies ON: ... · thr≥0.62 · ..." ---
    threshold = None
    tm = _re.search(r"thr≥([\d.]+)", fs)
    if tm:
        try:
            threshold = float(tm.group(1))
        except (TypeError, ValueError):
            threshold = None
    out["score_threshold"] = threshold

    # --- top candidates block ---
    # line pattern:  SYMBOL     DIR  score=0.58 lev=5x STRATEGY READY
    candidates = []
    seen_block = False
    for line in fs.splitlines():
        s = line.strip()
        if "Top candidates:" in s:
            seen_block = True
            continue
        if not seen_block:
            continue
        # stop at blank line or "Recent Executors" / performance block
        if not s or s.startswith("Recent Executors") or "Performance" in s or s.startswith("+---"):
            break
        cm = _re.match(
            r"([A-Z0-9\-]+)\s+(LONG|SHORT)\s+score=([\d.]+)\s+lev=(\d+)x\s*(.*)$", s
        )
        if cm:
            sym, direction, score, lev, tail = cm.groups()
            tail = tail.strip()
            strategies = []
            if tail:
                strategies = [t.strip() for t in tail.replace("READY", "").replace("[OPEN]", "").split("+") if t.strip()]
            ready = "READY" in tail
            is_open = "[OPEN]" in tail or sym in out.get("open_pairs", [])
            candidates.append({
                "symbol": sym,
                "direction": direction,
                "score": float(score),
                "leverage": int(lev),
                "strategies": strategies or ["—"],
                "ready": ready,
                "has_position": is_open,
            })
    out["candidates"] = candidates
    out["count"] = len(candidates)
    running_ex = []
    for line in fs.splitlines():
        if "position_executor" not in line or "RunnableStatus.RUNNING" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        # type, side, status, pnl_pct, pnl_quote, filled_quote, is_trading, close_type, age
        rec = {"side": "", "filled_amount_quote": 0.0, "net_pnl_quote": 0.0}
        for p in parts:
            if p.startswith("TradeType."):
                rec["side"] = p.replace("TradeType.", "")
            try:
                if p.replace(".", "", 1).replace("-", "", 1).isdigit() and "filled" not in rec:
                    pass
            except Exception:
                pass
        nums = []
        for p in parts:
            try:
                nums.append(float(p))
            except Exception:
                continue
        # pnl_pct, pnl_quote, filled_quote, age typically last numeric cluster
        if len(nums) >= 3:
            rec["net_pnl_quote"] = nums[1] if len(nums) >= 2 else 0.0
            rec["filled_amount_quote"] = nums[2] if len(nums) >= 3 else 0.0
            if len(nums) >= 4:
                rec["age_s"] = nums[-1]
        if rec["filled_amount_quote"] > 0:
            running_ex.append(rec)
    out["executors_running"] = running_ex
    return out


def get_bot_signals() -> Dict[str, Any]:
    """HTTP handler for /api/bot/signals — the honest, bot-authoritative signal view."""
    parsed = parse_bot_signals()
    if parsed.get("available"):
        return parsed
    # fallback: enrich with local scanner so UI still has data when bot is off
    try:
        scan = build_scan()
        parsed["fallback_scan"] = {
            "candidates": scan.get("candidates", []),
            "slots": scan.get("slots", {}),
        }
    except Exception:
        pass
    return parsed


def get_condor_status() -> Dict[str, Any]:
    """Check if Condor AI agent is running and return real status."""
    result: Dict[str, Any] = {
        "status": "Not Connected",
        "agent": "—",
        "strategy": "—",
        "llm": "—",
        "server": "—",
        "web_port": "—",
        "info": "Condor not detected",
    }
    # 1. Check if Condor tmux session is alive
    condor_running = False
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", "condor"],
            capture_output=True, timeout=3,
        )
        condor_running = proc.returncode == 0
    except Exception:
        pass

    # Also check by process name
    if not condor_running:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", "condor.*main\\.py"],
                capture_output=True, timeout=3,
            )
            condor_running = proc.returncode == 0
        except Exception:
            pass

    if not condor_running:
        return result

    # 2. Hit Condor web health endpoint
    try:
        req = urllib.request.Request(
            "http://localhost:8088/",
            headers={"User-Agent": "hbot-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
        result["web_port"] = "8088"
    except Exception:
        pass

    # 3. Read config.yml for server info
    condor_config_path = os.path.expanduser(
        "~/Desktop/humming-bot/condor/config.yml"
    )
    try:
        if os.path.isfile(condor_config_path):
            import yaml  # type: ignore
            cfg = yaml.safe_load(open(condor_config_path).read()) or {}
            servers = cfg.get("servers") or {}
            default = cfg.get("default_server") or ""
            srv = servers.get(default) or {}
            if srv:
                host = srv.get("host", "localhost")
                port = srv.get("port", 8000)
                result["server"] = f"{host}:{port}"
    except Exception:
        pass

    # 4. Read .env and config.yml for LLM info
    condor_env_path = os.path.expanduser(
        "~/Desktop/humming-bot/condor/.env"
    )
    condor_config_path_2 = os.path.expanduser(
        "~/Desktop/humming-bot/condor/config.yml"
    )
    try:
        # Check config.yml for custom provider
        if os.path.isfile(condor_config_path_2):
            import yaml  # type: ignore
            cfg2 = yaml.safe_load(open(condor_config_path_2).read()) or {}
            user_prefs = cfg2.get("user_preferences", {})
            for uid, prefs in user_prefs.items():
                agent_prefs = prefs.get("agent", {})
                custom = agent_prefs.get("custom_providers", [])
                if custom:
                    provider = custom[0]
                    models = provider.get("models", [])
                    result["llm"] = f"{provider.get('name', 'Custom')}/{models[0]}" if models else provider.get("name", "Custom")
                    break
        # Fallback to .env
        if result["llm"] in ("—", "Not configured") and os.path.isfile(condor_env_path):
            env_text = open(condor_env_path).read()
            base_url = ""
            for line in env_text.splitlines():
                if line.startswith("OPENAI_BASE_URL="):
                    base_url = line.split("=", 1)[1].strip().lower()
                    break
            if "xiaomi" in base_url:
                result["llm"] = "mimo v2.5 (Xiaomi)"
            elif "openrouter" in base_url:
                result["llm"] = "DeepSeek V4 Flash 0731 (OpenRouter)"
            elif os.path.isfile(condor_env_path) and "OPENROUTER_API_KEY=" in env_text:
                result["llm"] = "DeepSeek V4 Flash 0731 (OpenRouter)"
    except Exception:
        pass

    # Autonomous agent: report the v37 risk manager status
    try:
        rm = _read_risk_manager_status()
        result["autonomous_agent"] = rm
    except Exception:
        pass

    result["status"] = "Connected"
    result["agent"] = "Condor v0.1"
    result["strategy"] = "Telegram + Web + 🤖 risk agent"
    result["info"] = "Condor AI agent is running"
    return result


def _read_risk_manager_status() -> Dict[str, Any]:
    """Read the v37 risk-manager autonomous agent's latest state from disk."""
    d: Dict[str, Any] = {"agent": "v37_risk_manager", "status": "idle", "last_action": None,
                          "model": "DeepSeek V4 Flash 0731", "ticks": 0}
    base = os.path.expanduser("~/Desktop/humming-bot/condor/agents/v37_risk_manager/strategies/disciplined_perps_strategy")
    try:
        # Latest decision can be in legacy dry-runs or current live session snapshots.
        import glob
        import json, re
        runs = glob.glob(os.path.join(base, "dry_runs", "*.md"))
        runs += glob.glob(os.path.join(base, "sessions", "session_*", "snapshots", "*.md"))
        runs = sorted(runs, key=os.path.getmtime, reverse=True)
        if runs:
            txt = open(runs[0]).read()
            # Extract the decision JSON — choose a block containing both action and thesis.
            blocks = re.findall(r"```json\s*(\{.*?\})\s*```", txt, re.S)
            dec = None
            for b in blocks:
                try:
                    j = json.loads(b)
                    if isinstance(j, dict) and "action" in j and "thesis" in j:
                        dec = j
                        break
                except Exception:
                    continue
            if dec:
                d["last_action"] = {
                    "action": dec.get("action"),
                    "symbol": dec.get("symbol"),
                    "side": dec.get("side"),
                    "confidence": dec.get("confidence"),
                    "thesis": (dec.get("thesis") or "")[:160],
                    "risk_pct": dec.get("risk_pct"),
                }
                d["status"] = "ticked"
        # agent/strategy meta
        ag = os.path.expanduser("~/Desktop/humming-bot/condor/agents/v37_risk_manager/AGENT.md")
        if os.path.isfile(ag):
            d["name"] = "V37 Risk Manager"
    except Exception:
        pass
    return d



def _side_label(side_val: Any) -> str:
    """Normalize TradeType / int / str side to LONG/SHORT."""
    if side_val is None:
        return "?"
    if isinstance(side_val, int):
        return "LONG" if side_val == 1 else ("SHORT" if side_val == 2 else str(side_val))
    s = str(side_val).upper()
    if "BUY" in s or s in ("1", "LONG"):
        return "LONG"
    if "SELL" in s or s in ("2", "SHORT"):
        return "SHORT"
    return s


def _fmt_ts(ts: Any) -> str:
    """Unix seconds → 'YYYY-MM-DD HH:MM:SS' UTC string."""
    try:
        t = float(ts)
        if t > 1e12:  # ms
            t /= 1000.0
        if t <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    except Exception:
        return ""


def _parse_level_id(level_id: Any) -> Dict[str, Any]:
    """Parse level_id format: STRAT1+STRAT2|0.58|L  → strategy tag, score, dir."""
    out = {"strategy_tag": "—", "strategies": [], "entry_score": None, "strat": "SCALP"}
    if not level_id:
        return out
    s = str(level_id)
    parts = s.split("|")
    tag = parts[0].strip() if parts else s
    if tag and tag not in ("None", "null", ""):
        out["strategy_tag"] = tag
        out["strategies"] = [x for x in tag.split("+") if x and x != "NONE"]
        # Primary strat for compact column: first agreeing engine or full tag
        out["strat"] = out["strategies"][0] if len(out["strategies"]) == 1 else tag
    if len(parts) >= 2:
        try:
            out["entry_score"] = float(parts[1])
        except Exception:
            pass
    return out


def _px_fixed(v: Any) -> float:
    """Hummingbot TradeFill stores price/amount as fixed-point ×1e6."""
    try:
        return float(v) / 1_000_000.0
    except Exception:
        return 0.0


def _fee_from_fill_row(r: Any) -> float:
    try:
        j = json.loads(r["trade_fee"] or "{}") if isinstance(r["trade_fee"], str) else (r["trade_fee"] or {})
        flat = j.get("flat_fees") or []
        total = sum(float(f.get("amount") or 0) for f in flat)
        if total:
            return total
    except Exception:
        pass
    try:
        v = r["trade_fee_in_quote"]
        if v is None:
            return 0.0
        # stored ×1e6 sometimes; if already small float leave it
        fv = float(v)
        return fv / 1_000_000.0 if fv > 1.0 else fv
    except Exception:
        return 0.0


def load_trades_from_fills(db_path: str, limit: int = 200, engine: str = "E1", strategy: str = "v37_scalp_multi") -> List[Dict[str, Any]]:
    """
    Reconstruct round-trip trades from TradeFill when Executors table is empty.
    Matches LONG(BUY open)+SELL close and SHORT(SELL open)+BUY close chronologically.
    """
    import sqlite3

    trades: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT symbol, trade_type, price, amount, leverage, trade_fee, "
            "trade_fee_in_quote, timestamp, order_id FROM TradeFill ORDER BY timestamp ASC"
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"[journal] TradeFill read failed {db_path}: {e}")
        return []

    # symbol -> open lots [{side, entry, amount, fees, ts, lev}]
    open_lots: Dict[str, List[Dict[str, Any]]] = {}

    for r in rows:
        sym = r["symbol"]
        tt = (r["trade_type"] or "").upper()
        price = _px_fixed(r["price"])
        amount = _px_fixed(r["amount"])
        if amount <= 0 or price <= 0:
            continue
        fees = _fee_from_fill_row(r)
        ts = float(r["timestamp"] or 0)
        # timestamps may be ms
        ts_s = ts / 1000.0 if ts > 1e12 else ts
        lev = int(r["leverage"] or 1)
        lots = open_lots.setdefault(sym, [])

        closing = False
        if lots:
            lot = lots[0]
            closing = (lot["side"] == "LONG" and tt == "SELL") or (lot["side"] == "SHORT" and tt == "BUY")

        if closing and lots:
            lot = lots[0]
            close_amt = min(amount, lot["amount"])
            if lot["side"] == "LONG":
                raw_pnl = (price - lot["entry"]) * close_amt
            else:
                raw_pnl = (lot["entry"] - price) * close_amt
            open_fee_part = lot["fees"] * (close_amt / lot["amount"]) if lot["amount"] else 0.0
            close_fee_part = fees * (close_amt / amount) if amount else fees
            net = raw_pnl - open_fee_part - close_fee_part
            notional = lot["entry"] * close_amt
            margin = notional / lot["lev"] if lot["lev"] else notional
            pnl_pct = (net / margin * 100.0) if margin else 0.0
            eid = f"fill-{sym}-{int(lot['ts'])}-{int(ts_s)}"
            result = "win" if net > 0 else ("loss" if net < 0 else "flat")
            strat_tag = "PMM" if engine == "E4" else "—"
            trades.append({
                "id": eid,
                "symbol": sym,
                "pair": sym,
                "side": lot["side"],
                "engine": engine,
                "strategy": strategy,
                "strategy_tag": strat_tag,
                "strategies": ["PMM"] if engine == "E4" else [],
                "strat": strat_tag,
                "entry_score": None,
                "entry": round(lot["entry"], 8),
                "exit": round(price, 8),
                "amount": round(close_amt, 8),
                "leverage": lot["lev"],
                "pnl": round(net, 6),
                "pnl_usd": round(net, 6),
                "pnl_pct": round(pnl_pct, 4),
                "fees": round(open_fee_part + close_fee_part, 6),
                "filled_quote": round(notional, 4),
                "close_type": "FILL_RECON",
                "exit_reason": "FILL_RECON",
                "result": result,
                "status": "CLOSED",
                "opened_at": _fmt_ts(lot["ts"]),
                "closed_at": _fmt_ts(ts_s),
                "time": _fmt_ts(ts_s),
                "timestamp": ts_s,
                "source_db": os.path.basename(db_path),
                "source": "tradefill",
            })
            lot["amount"] -= close_amt
            lot["fees"] -= open_fee_part
            amount -= close_amt
            fees -= close_fee_part
            if lot["amount"] <= 1e-12:
                lots.pop(0)

        if amount > 1e-12:
            side = "LONG" if tt == "BUY" else "SHORT"
            if lots and lots[-1]["side"] == side:
                lot = lots[-1]
                new_amt = lot["amount"] + amount
                lot["entry"] = (lot["entry"] * lot["amount"] + price * amount) / new_amt
                lot["amount"] = new_amt
                lot["fees"] += fees
            else:
                lots.append({
                    "side": side, "entry": price, "amount": amount,
                    "fees": fees, "ts": ts_s, "lev": lev,
                })

    trades.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
    return trades[:limit]


def _engine_tag(controller_id: str, default: str = "E1") -> str:
    cid = (controller_id or "").lower()
    if cid.startswith("e4_") or "pmm" in cid:
        return "E4"
    if "v37" in cid or "scalp" in cid:
        return "E1"
    return default or "E1"


def load_engine2_closed_trades() -> List[Dict[str, Any]]:
    """Engine 2 (Condor) closed fills if any session snapshot recorded PnL."""
    import glob
    import re
    base = os.path.expanduser(
        "~/Desktop/humming-bot/condor/agents/v37_risk_manager/strategies/"
        "disciplined_perps_strategy/sessions"
    )
    trades: List[Dict[str, Any]] = []
    files = sorted(glob.glob(os.path.join(base, "session_*", "snapshots", "*.md")),
                   key=os.path.getmtime)
    for filename in files:
        try:
            text_md = open(filename).read()
        except Exception:
            continue
        for block in re.findall(r"```json\s*(\{.*?\})\s*```", text_md, re.S):
            try:
                obj = json.loads(block)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            action = str(obj.get("action") or "").upper()
            pnl = obj.get("pnl") or obj.get("realized_pnl") or obj.get("pnl_usd")
            if pnl is None:
                continue
            if action not in ("CLOSE", "EXIT", "STOP", "TAKE_PROFIT", "CLOSED"):
                continue
            try:
                pnl_f = float(pnl)
            except Exception:
                continue
            ts = os.path.getmtime(filename)
            sym = obj.get("symbol") or obj.get("pair") or "?"
            side = str(obj.get("side") or "—").upper()
            if side in ("BUY", "LONG"):
                side = "LONG"
            elif side in ("SELL", "SHORT"):
                side = "SHORT"
            eid = f"e2-{os.path.basename(filename)}-{sym}"
            result = "win" if pnl_f > 0 else ("loss" if pnl_f < 0 else "flat")
            trades.append({
                "id": eid,
                "symbol": sym,
                "pair": sym,
                "side": side,
                "engine": "E2",
                "strategy": "condor",
                "strategy_tag": "CONDOR",
                "strategies": ["CONDOR"],
                "strat": "CONDOR",
                "entry_score": obj.get("confidence"),
                "entry": float(obj.get("entry") or obj.get("entry_price") or 0),
                "exit": float(obj.get("exit") or obj.get("exit_price") or 0),
                "amount": float(obj.get("amount") or 0),
                "leverage": int(obj.get("leverage") or 1),
                "pnl": round(pnl_f, 6),
                "pnl_usd": round(pnl_f, 6),
                "pnl_pct": float(obj.get("pnl_pct") or 0),
                "fees": float(obj.get("fees") or 0),
                "filled_quote": float(obj.get("notional") or 0),
                "close_type": action,
                "exit_reason": action,
                "result": result,
                "status": "CLOSED",
                "opened_at": "",
                "closed_at": _fmt_ts(ts),
                "time": _fmt_ts(ts),
                "timestamp": ts,
                "source_db": "condor",
                "source": "engine2",
            })
    return trades


def load_executor_trades(limit: int = 200) -> List[Dict[str, Any]]:
    """
    Closed trades from every engine:
      E1 — PositionExecutors (TradeFill only if executors empty)
      E4 — TradeFill round-trips (PMM quote refresh leaves executors unfilled)
      E2 — Condor closed snapshots, if any
    """
    import sqlite3

    trades: List[Dict[str, Any]] = []
    seen_ids: set = set()
    e1_executor_closed = False

    for spec in _ENGINE_DBS:
        db_path = spec["path"]
        engine = spec["engine"]
        prefer = spec.get("prefer") or "executors"
        strategy = spec.get("strategy") or "v37_scalp_multi"
        if not os.path.isfile(db_path):
            continue

        if prefer == "executors":
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT id, timestamp, type, close_type, close_timestamp, status, "
                    "config, net_pnl_pct, net_pnl_quote, cum_fees_quote, filled_amount_quote, "
                    "is_active, is_trading, custom_info, controller_id "
                    "FROM Executors ORDER BY COALESCE(close_timestamp, timestamp) DESC"
                ).fetchall()
                con.close()
            except Exception as e:
                print(f"[journal] sqlite read failed {db_path}: {e}")
                rows = []

            for r in rows:
                eid = r["id"]
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                try:
                    cfg = json.loads(r["config"] or "{}")
                except Exception:
                    cfg = {}
                try:
                    info = json.loads(r["custom_info"] or "{}") if r["custom_info"] else {}
                except Exception:
                    info = {}

                filled = float(r["filled_amount_quote"] or 0)
                is_active = bool(r["is_active"])
                close_type_n = r["close_type"]
                close_type = _CLOSE_TYPE_MAP.get(close_type_n, str(close_type_n) if close_type_n is not None else "")
                pnl = float(r["net_pnl_quote"] or 0)
                pnl_pct = float(r["net_pnl_pct"] or 0) * 100.0
                pair = cfg.get("trading_pair") or info.get("trading_pair") or "?"
                side = _side_label(cfg.get("side") if cfg.get("side") is not None else info.get("side"))
                entry = float(cfg.get("entry_price") or info.get("current_position_average_price") or 0)
                exit_px = float(info.get("close_price") or 0)
                amount = float(cfg.get("amount") or 0)
                lev = int(cfg.get("leverage") or 1)
                fees = float(r["cum_fees_quote"] or 0)
                opened_at = _fmt_ts(r["timestamp"])
                closed_at = _fmt_ts(r["close_timestamp"]) if r["close_timestamp"] else ""
                level_meta = _parse_level_id(cfg.get("level_id") or info.get("level_id"))
                tag = _engine_tag(r["controller_id"], engine)

                if is_active:
                    status = "OPEN"
                elif close_type == "POSITION_HOLD":
                    status = "HELD"
                elif filled <= 0 and abs(pnl) < 1e-12:
                    status = "FAILED"
                else:
                    status = "CLOSED"

                if status not in ("CLOSED", "HELD"):
                    continue
                if status == "CLOSED" and filled <= 0:
                    continue

                if tag == "E1" and status == "CLOSED":
                    e1_executor_closed = True

                result = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
                trades.append({
                    "id": eid,
                    "symbol": pair,
                    "pair": pair,
                    "side": side,
                    "engine": tag,
                    "strategy": r["controller_id"] or strategy,
                    "strategy_tag": level_meta["strategy_tag"],
                    "strategies": level_meta["strategies"],
                    "strat": level_meta["strat"],
                    "entry_score": level_meta["entry_score"],
                    "entry": entry,
                    "exit": exit_px,
                    "amount": amount,
                    "leverage": lev,
                    "pnl": round(pnl, 6),
                    "pnl_usd": round(pnl, 6),
                    "pnl_pct": round(pnl_pct, 4),
                    "fees": round(fees, 6),
                    "filled_quote": round(filled, 4),
                    "close_type": close_type or "—",
                    "exit_reason": close_type or "—",
                    "result": result,
                    "status": status,
                    "opened_at": opened_at,
                    "closed_at": closed_at or opened_at,
                    "time": closed_at or opened_at,
                    "timestamp": float(r["close_timestamp"] or r["timestamp"] or 0),
                    "source_db": os.path.basename(db_path),
                    "source": "executors",
                })

        if prefer == "fills" or (engine == "E1" and not e1_executor_closed):
            fill_trades = load_trades_from_fills(
                db_path, limit=max(limit, 200), engine=engine, strategy=strategy
            )
            for t in fill_trades:
                if t["id"] not in seen_ids:
                    seen_ids.add(t["id"])
                    trades.append(t)

    for t in load_engine2_closed_trades():
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            trades.append(t)

    trades.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
    reset_ts = _perf_reset_ts()
    if reset_ts > 0:
        trades = [t for t in trades if float(t.get("timestamp") or 0) > reset_ts]
    return trades[:limit]



def get_journal(limit: int = 100) -> Dict[str, Any]:
    """Cached journal payload for /api/journal."""
    global _journal, _journal_last_fetch
    now = time.time()
    if now - _journal_last_fetch < 8 and _journal:
        return {
            "trades": _journal[:limit],
            "count": len(_journal),
            "source": (_journal[0].get("source") if _journal else "sqlite"),
        }
    trades = load_executor_trades(limit=max(limit, 200))
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    _journal = closed
    _journal_last_fetch = now
    src_set = sorted({(t.get("source") or "sqlite") for t in closed}) if closed else ["none"]
    engines = sorted({(t.get("engine") or "?") for t in closed}) if closed else []
    return {
        "trades": closed[:limit],
        "count": len(closed),
        "source": "+".join(src_set),
        "engines": engines,
    }


def compute_performance() -> Dict[str, Any]:
    """
    Live performance from bot SQLite closed executors (or TradeFill fallback).
    Includes per-strategy breakdown when level_id attribution is present.
    """
    global _perf_cache, _perf_last_fetch
    now = time.time()
    if now - _perf_last_fetch < 10 and _perf_cache.get("source") not in (None, "none"):
        return _perf_cache

    trades = [t for t in load_executor_trades(limit=500) if t.get("status") == "CLOSED"]

    if trades:
        pnls = [float(t["pnl"]) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        flats = [p for p in pnls if p == 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        total_pnl = sum(pnls)
        n = len(pnls)
        wr = (100.0 * len(wins) / n) if n else 0.0
        pf = (gross_profit / gross_loss) if gross_loss > 1e-12 else (999.0 if gross_profit > 0 else 0.0)
        fees = sum(float(t.get("fees") or 0) for t in trades)
        volume = sum(float(t.get("filled_quote") or 0) for t in trades)

        chronological = sorted(trades, key=lambda t: t.get("timestamp") or 0)
        equity = peak = 0.0
        max_dd = 0.0
        for t in chronological:
            equity += float(t["pnl"])
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        if peak >= 0.5:
            max_dd_str = f"{(100.0 * max_dd / peak):.1f}%"
        elif max_dd > 0:
            max_dd_str = f"-${max_dd:.2f}"
        else:
            max_dd_str = "0%"

        sharpe = "—"
        if len(pnls) >= 3:
            mean = sum(pnls) / len(pnls)
            var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            std = var ** 0.5
            if std > 1e-12:
                sharpe = f"{(mean / std):.2f}"

        by_ct: Dict[str, int] = {}
        for t in trades:
            ct = t.get("close_type") or "—"
            by_ct[ct] = by_ct.get(ct, 0) + 1

        # Per-strategy attribution (from level_id). Attribute full trade PnL to each
        # agreeing engine so multi-vote trades count for all contributors.
        by_strat: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            strats = t.get("strategies") or []
            tag = t.get("strategy_tag") or t.get("strat") or "—"
            keys = strats if strats else ([tag] if tag and tag != "—" else ["unattributed"])
            for k in keys:
                b = by_strat.setdefault(k, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "fees": 0.0})
                b["trades"] += 1
                pnl = float(t.get("pnl") or 0)
                b["pnl"] += pnl
                b["fees"] += float(t.get("fees") or 0)
                if pnl > 0:
                    b["wins"] += 1
                elif pnl < 0:
                    b["losses"] += 1
        for k, b in by_strat.items():
            b["pnl"] = round(b["pnl"], 4)
            b["fees"] = round(b["fees"], 4)
            b["win_rate"] = f"{(100.0 * b['wins'] / b['trades']):.1f}%" if b["trades"] else "—"

        by_symbol: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            sym = t.get("symbol") or "?"
            b = by_symbol.setdefault(sym, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
            b["trades"] += 1
            pnl = float(t.get("pnl") or 0)
            b["pnl"] += pnl
            if pnl > 0:
                b["wins"] += 1
            elif pnl < 0:
                b["losses"] += 1
        for k, b in by_symbol.items():
            b["pnl"] = round(b["pnl"], 4)

        by_engine: Dict[str, Dict[str, Any]] = {
            "E1": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "fees": 0.0},
            "E2": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "fees": 0.0},
            "E4": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "fees": 0.0},
        }
        for t in trades:
            eng = t.get("engine") or "E1"
            b = by_engine.setdefault(eng, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "fees": 0.0})
            b["trades"] += 1
            pnl = float(t.get("pnl") or 0)
            b["pnl"] += pnl
            b["fees"] += float(t.get("fees") or 0)
            if pnl > 0:
                b["wins"] += 1
            elif pnl < 0:
                b["losses"] += 1
        for k, b in by_engine.items():
            b["pnl"] = round(b["pnl"], 4)
            b["fees"] = round(b["fees"], 4)
            b["win_rate"] = f"{(100.0 * b['wins'] / b['trades']):.1f}%" if b["trades"] else "—"

        src_set = sorted({(t.get("source") or "sqlite") for t in trades})
        src = "+".join(src_set) if src_set else "sqlite"
        _perf_cache = {
            "win_rate": f"{wr:.1f}%",
            "profit_factor": f"{pf:.2f}" if pf < 100 else "∞",
            "total_pnl": round(total_pnl, 4),
            "trades": n,
            "sharpe": sharpe,
            "max_dd": max_dd_str,
            "wins": len(wins),
            "losses": len(losses),
            "flats": len(flats),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "fees": round(fees, 4),
            "volume": round(volume, 2),
            "by_close_type": by_ct,
            "by_strategy": by_strat,
            "by_symbol": by_symbol,
            "by_engine": by_engine,
            "source": src,
        }
        _perf_last_fetch = now
        return _perf_cache

    # Fallback: API performance (usually empty for CLI bots)
    data = hbot_get("/executors/performance")
    if isinstance(data, dict) and "error" not in data and (data.get("total_executors") or 0) > 0:
        wr = data.get("win_rate")
        pf = data.get("profit_factor") if "profit_factor" in data else None
        sharpe = data.get("sharpe_ratio")
        _perf_cache = {
            "win_rate": f"{float(wr) * 100:.1f}%" if isinstance(wr, (int, float)) and wr <= 1 else (
                f"{wr:.1f}%" if isinstance(wr, (int, float)) else "—"
            ),
            "profit_factor": f"{pf:.2f}" if isinstance(pf, (int, float)) else "—",
            "total_pnl": float(data.get("pnl_total_quote") or data.get("global_pnl_quote") or 0),
            "trades": int(data.get("total_executors") or 0),
            "sharpe": f"{sharpe:.2f}" if isinstance(sharpe, (int, float)) else "—",
            "max_dd": "—",
            "wins": 0,
            "losses": 0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "fees": float(data.get("fees_total_quote") or 0),
            "volume": float(data.get("volume_total_quote") or 0),
            "by_close_type": data.get("by_status") or {},
            "by_strategy": {},
            "by_symbol": {},
            "source": "api",
        }
    else:
        _perf_cache = {
            "win_rate": "—", "profit_factor": "—", "total_pnl": 0,
            "trades": 0, "sharpe": "—", "max_dd": "0%",
            "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "fees": 0.0, "volume": 0.0,
            "by_close_type": {}, "by_strategy": {}, "by_symbol": {}, "source": "none",
        }
    _perf_last_fetch = now
    return _perf_cache


def chart_payload(symbol: str, tf: str = "15m") -> Dict[str, Any]:
    pair = normalize_symbol(symbol)
    tf_map = {"15m": "15m", "1h": "1H", "1H": "1H", "4h": "4H", "4H": "4H"}
    gran = tf_map.get(tf, "15m")
    candles = get_candles(pair, gran, 200)
    out = []
    for c in reversed(candles):  # oldest first for chart
        try:
            out.append({
                "t": int(c[0]),
                "o": float(c[1]),
                "h": float(c[2]),
                "l": float(c[3]),
                "c": float(c[4]),
                "v": float(c[5]),
            })
        except Exception:
            continue
    # dedupe ascending
    seen = set()
    clean = []
    for row in sorted(out, key=lambda x: x["t"]):
        if row["t"] in seen or row["o"] <= 0:
            continue
        seen.add(row["t"])
        clean.append(row)
    tickers = get_tickers()
    t = tickers.get(pair) or {}
    return {
        "symbol": pair,
        "tf": tf,
        "candles": clean,
        "last_price": t.get("last_price"),
        "change_24h": t.get("change_24h"),
    }


# ---------------------------------------------------------------------------
# Condor API proxy helpers
# ---------------------------------------------------------------------------

_condor_jwt_cache: Dict[str, Any] = {"token": None, "expires": 0.0}


def _get_condor_jwt() -> str:
    """Generate a JWT for Condor API, cached for 23 hours."""
    now = time.time()
    if _condor_jwt_cache["token"] and now < _condor_jwt_cache["expires"]:
        return _condor_jwt_cache["token"]

    try:
        proc = subprocess.run(
            [
                "bash", "-c",
                'cd ~/Desktop/humming-bot/condor && unset PYTHONPATH && '
                'uv run python -c \'from condor.web.auth import create_jwt; '
                'print(create_jwt(0, "CryptoCTO1", "Crypto", "admin"))\'',
            ],
            capture_output=True, text=True, timeout=15,
        )
        token = (proc.stdout or "").strip().split("\n")[-1].strip()
        if token and len(token) > 20:
            _condor_jwt_cache["token"] = token
            _condor_jwt_cache["expires"] = now + 23 * 3600
            return token
        print(f"[condor] JWT gen failed: out={proc.stdout!r} err={proc.stderr!r}")
    except Exception as e:
        print(f"[condor] JWT gen error: {e}")
    return ""


def condor_api_get(path: str) -> Dict[str, Any]:
    """GET request to Condor API with JWT auth."""
    token = _get_condor_jwt()
    if not token:
        return {"error": "Failed to obtain Condor JWT"}
    url = f"http://localhost:8088/api/v1/{path}"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "hbot-dashboard/1.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "url": url}


def condor_api_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST request to Condor API with JWT auth."""
    token = _get_condor_jwt()
    if not token:
        return {"error": "Failed to obtain Condor JWT"}
    url = f"http://localhost:8088/api/v1/{path}"
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "hbot-dashboard/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "url": url}


_XIAOMI_SYSTEM_PROMPT = (
    "You are an expert crypto trading assistant integrated with a Hummingbot "
    "dashboard. You analyze market signals, recommend trades with precise "
    "entry/SL/TP levels, and advise on position management. Be concise, "
    "actionable, and always include a risk assessment. Format your responses "
    "clearly with bullet points when appropriate."
)


def call_xiaomi_llm(messages: List[Dict[str, str]], max_tokens: int = 1024) -> str:
    """Call the Xiaomi mimo-v2.5-pro LLM API and return assistant reply."""
    api_key = os.environ.get("XIAOMI_TOKEN_PLAN_API_KEY", "")
    if not api_key:
        return "Error: XIAOMI_TOKEN_PLAN_API_KEY not set"
    url = "https://token-plan-ams.xiaomimimo.com/v1/chat/completions"
    payload = json.dumps({
        "model": "mimo-v2.5-pro",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            msg = result["choices"][0]["message"]
            content = msg.get("content") or ""
            # mimo-v2.5-pro is a reasoning model — when content is empty,
            # the actual output may be in reasoning_content
            if not content:
                content = msg.get("reasoning_content") or ""
            return content
    except Exception as e:
        return f"Error calling LLM: {e}"


# ══════════════════════════════════════════════════════════════════
#  🤖 AUTONOMOUS AGENT  (DeepSeek V4 Flash 0731 via OpenRouter)
#  Fully autonomous: opens/closes/monitors ALL positions (strategy + its own).
#  Slots: strategy 3 + agent 3 = 6 total. $10 min margin. Free leverage (capped).
#  NO dry-run — all decisions place real Bitget orders.
# ══════════════════════════════════════════════════════════════════
_AGENT_MODEL = os.environ.get("AGENT_MODEL", "deepseek/deepseek-v4-flash-0731")
_AGENT_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not _AGENT_API_KEY:
    # fallback: read from condor/.env (never logged)
    try:
        _condor_env = os.path.expanduser("~/Desktop/humming-bot/condor/.env")
        if os.path.isfile(_condor_env):
            with open(_condor_env, "r") as _f:
                for _line in _f:
                    if _line.startswith("OPENROUTER_API_KEY="):
                        _AGENT_API_KEY = _line.strip().split("=", 1)[1].strip().strip("'\"")
                        break
    except Exception:
        pass
_AGENT_STATE_PATH = os.path.join(BASE_DIR, "autonomous_agent.json")

_AGENT = {
    # Dashboard-native LLM Engine 2: separate 3-slot pool alongside the systematic bot.
    "enabled": False,
    "freq_sec": 60,
    "model": _AGENT_MODEL,
    "slots": 3,                 # agent's own slots (separate from strategy's 3)
    "position_size_quote": 10,  # $10 min margin per trade
    "max_position_size_quote": 50,  # cap when "increase" — max $50 margin
    "max_leverage": 20,          # agent may choose leverage up to 20x
    "dry_run": False,            # always live
    "running": False,            # loop thread alive
    "last_tick": 0.0,
    "last_decision": "HOLD",
    "last_reason": "agent idle",
    "last_pnl": 0.0,
    "agent_pnl": 0.0,
    "agent_trades": 0,
    "consecutive_losses": 0,
    "history": [],               # capped list of {ts, decision, reason, actions}
    "circuit_breaker": False,
    "equity_floor_hit": False,
    "error": None,
}
_history_lock = threading.Lock()


def _agent_load_state() -> None:
    """Restore agent state from disk so it survives dashboard restarts."""
    try:
        if os.path.isfile(_AGENT_STATE_PATH):
            with open(_AGENT_STATE_PATH, "r") as f:
                saved = json.load(f)
            for k in ("enabled", "freq_sec", "model", "slots", "position_size_quote",
                      "max_position_size_quote", "max_leverage", "agent_pnl",
                      "agent_trades", "consecutive_losses", "dry_run"):
                if k in saved:
                    _AGENT[k] = saved[k]
    except Exception:
        pass


def _agent_save_state() -> None:
    try:
        with open(_AGENT_STATE_PATH, "w") as f:
            json.dump({k: _AGENT[k] for k in (
                "enabled", "freq_sec", "model", "slots", "position_size_quote",
                "max_position_size_quote", "max_leverage", "dry_run",
                "agent_pnl", "agent_trades", "consecutive_losses"
            )}, f, indent=2)
    except Exception:
        pass


def call_openrouter_llm(messages: List[Dict[str, str]], max_tokens: int = 1200,
                        temperature: float = 0.3) -> str:
    """Call DeepSeek V4 Flash 0731 via OpenRouter. Returns raw text or error string."""
    if not _AGENT_API_KEY:
        return "Error: OPENROUTER_API_KEY not set"
    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = json.dumps({
        "model": _AGENT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {_AGENT_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8770",
            "X-Title": "Hummingbot Autonomous Agent",
        })
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode())
            msg = result["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content:
                # DeepSeek V4 Flash 0731 is a reasoning model:
                # output can land in reasoning / reasoning_content when content is empty
                content = msg.get("reasoning_content") or ""
            if not content and msg.get("reasoning"):
                content = msg.get("reasoning")
            return content
    except Exception as e:
        return f"Error calling DeepSeek: {e}"


def _agent_get_context() -> str:
    """Build a compact market + position context string for the agent (sanitized)."""
    lines = []
    try:
        pos_resp = hbot_request("POST", "/trading/positions", {
            "account_names": ["master_account"],
            "connector_names": ["bitget_perpetual"],
        }, timeout=8.0)
        pos = (pos_resp.get("data") or []) if isinstance(pos_resp, dict) else []
        lines.append(f"OPEN POSITIONS ({len(pos)}):")
        for p in pos:
            text = (f"  {p.get('trading_pair','')} {p.get('side','')} "
                    f"entry={p.get('entry_price')} lev={p.get('leverage')} "
                    f"pnl={round(float(p.get('unrealized_pnl') or 0), 2)}")
            lines.append(text)
    except Exception:
        pass
    try:
        pf = hbot_request("POST", "/portfolio/state", {}, timeout=6.0)
        if isinstance(pf, dict):
            for acc, conns in pf.items():
                if not isinstance(conns, dict):
                    continue
                for conn, tokens in conns.items():
                    if conn != "bitget_perpetual" or not isinstance(tokens, list):
                        continue
                    for t in tokens:
                        if t.get("token") == "USDT":
                            lines.append(f"EQUITY: {round(float(t.get('units') or 0), 2)} "
                                         f"AVAILABLE: {round(float(t.get('available_units') or 0), 2)}")
    except Exception:
        pass
    # signals — top 6 by abs score
    try:
        scan = build_scan(force=True)
        market = sorted(scan.get("market") or [],
                        key=lambda m: abs(float(m.get("score") or 0)), reverse=True)[:6]
        lines.append("SIGNALS (top):")
        for m in market:
            if float(m.get("score") or 0) != 0:
                lines.append(f"  {m.get('symbol','')} {m.get('direction','')} "
                             f"score={m.get('score')} ready={m.get('ready')}")
    except Exception:
        pass
    scores = ", ".join(lines)
    # strip control chars / injectable markup
    import re as _re
    scores = _re.sub(r"[\x00-\x1f]", " ", scores)
    return scores


def _agent_open_trade(pair: str, side: str, margin_usdt: float, leverage: int) -> Dict[str, Any]:
    """Open a position via Hummingbot API. Returns order result."""
    margin = max(float(margin_usdt or _AGENT["position_size_quote"]), 5.0)
    margin = min(margin, _AGENT["max_position_size_quote"])
    lev = max(1, min(int(leverage or 5), _AGENT["max_leverage"]))
    # position_action=OPEN, $margin at chosen leverage
    amount = None  # let API use notional from margin? There's no quote-amount param; compute base
    # amount_base = (margin * lev) / price
    try:
        import urllib.parse as _up
        px = None
        cur = bitget_get("/api/v2/mix/market/tickers", {"symbol": bitget_symbol(pair), "productType": "USDT-FUTURES"})
        rows = cur.get("data") or []
        # Bitget /tickers ignores the symbol filter and returns ALL pairs (data[0] is always BTC).
        # Filter for the actual symbol so we don't size every order off BTC's price.
        sym = bitget_symbol(pair)
        for _r in rows:
            if _r.get("symbol") == sym:
                px = float(_r.get("lastPr") or 0)
                break
        if not px or px <= 0:
            # fallback to scan price
            scan = build_scan(force=True)
            for m in scan.get("market") or []:
                if m.get("symbol") == pair:
                    px = float(m.get("last_price") or m.get("price") or 0)
                    break
        if not px or px <= 0:
            return {"ok": False, "error": f"no price for {pair}"}
        amount = round((margin * lev) / px, 8)
        trade_type = "BUY" if side.upper() in ("BUY", "LONG") else "SELL"
        result = hbot_request("POST", "/trading/orders", {
            "account_name": "master_account",
            "connector_name": "bitget_perpetual",
            "trading_pair": pair,
            "trade_type": trade_type,
            "order_type": "MARKET",
            "amount": str(amount),
            "position_action": "OPEN",
            "price": None,
            "is_global_kwargs": True,
        }, timeout=20.0)
        return {"ok": "error" not in result, "detail": result, "amount": amount,
                "margin": margin, "leverage": lev, "pair": pair, "side": side}
    except Exception as e:
        return {"ok": False, "error": str(e), "pair": pair}


def _agent_close_trade(pair: str, side: str) -> Dict[str, Any]:
    """Close a position via Hummingbot API (reduce-only market order)."""
    try:
        pos_resp = hbot_request("POST", "/trading/positions", {
            "account_names": ["master_account"],
            "connector_names": ["bitget_perpetual"],
        }, timeout=8.0)
        amt = 0.0
        for p in (pos_resp.get("data") or []) if isinstance(pos_resp, dict) else []:
            if p.get("trading_pair") == pair:
                amt = float(p.get("amount") or 0)
                break
        if amt <= 0:
            return {"ok": False, "error": f"no open position {pair}"}
        close_trade_type = "SELL" if side.upper() in ("BUY", "LONG") else "BUY"
        result = hbot_request("POST", "/trading/orders", {
            "account_name": "master_account",
            "connector_name": "bitget_perpetual",
            "trading_pair": pair,
            "trade_type": close_trade_type,
            "order_type": "MARKET",
            "amount": str(round(amt, 8)),
            "position_action": "CLOSE",
            "price": None,
            "is_global_kwargs": True,
        }, timeout=20.0)
        return {"ok": "error" not in result, "detail": result, "pair": pair}
    except Exception as e:
        return {"ok": False, "error": str(e), "pair": pair}


def _agent_execute(decision: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Validate + execute the agent's structured decision. Returns per-action results."""
    results: List[Dict[str, Any]] = []
    if _AGENT["circuit_breaker"]:
        return [{"ok": False, "action": "SKIP", "reason": "circuit breaker armed — manual reset"}]
    # Count current total open (strategy + agent) to enforce combined cap 6
    open_count = 0
    try:
        pos_resp = hbot_request("POST", "/trading/positions", {
            "account_names": ["master_account"],
            "connector_names": ["bitget_perpetual"],
        }, timeout=8.0)
        open_count = len(pos_resp.get("data") or []) if isinstance(pos_resp, dict) else 0
    except Exception:
        open_count = 0
    actions = decision.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    for a in actions:
        if not isinstance(a, dict):
            continue
        act = str(a.get("action") or "").upper()
        pair = str(a.get("pair") or "")
        side = str(a.get("side") or "")
        try:
            if act == "HOLD":
                results.append({"ok": True, "action": "HOLD", "pair": pair})
                continue
            if act == "CLOSE":
                results.append({**{"action": "CLOSE"}, **_agent_close_trade(pair, side)})
                continue
            if act == "OPEN":
                # enforce slots
                if open_count >= 6:
                    results.append({"ok": False, "action": "OPEN", "pair": pair,
                                    "reason": f"combined slot cap 6 reached ({open_count} open)"})
                    continue
                margin = float(a.get("margin_usdt") or _AGENT["position_size_quote"])
                lev = int(a.get("leverage") or 5)
                res = _agent_open_trade(pair, side, margin, lev)
                if res.get("ok"):
                    open_count += 1
                results.append({**{"action": "OPEN"}, **res})
                continue
            results.append({"ok": False, "action": act, "pair": pair, "reason": "unknown action"})
        except Exception as e:
            results.append({"ok": False, "action": act, "pair": pair, "error": str(e)})
    return results


def agent_tick() -> Dict[str, Any]:
    """One autonomous agent decision cycle: context → LLM → validate → execute."""
    import datetime as _dt
    now = time.time()
    _AGENT["last_tick"] = now
    _AGENT["running"] = True
    try:
        ctx = _agent_get_context()
        sys_prompt = (
            "You are the autonomous trading agent for a Bitget USDT-M perpetual futures bot. "
            "A separate rule-based strategy (v37_scalp_multi) runs independently with 3 slots and "
            "may hold positions. You have YOUR OWN 3 slots. You may manage ALL open positions "
            "(strategy + your own).\n\n"
            "MANDATE:\n"
            "- Find high-quality setups and OPEN trades at $10 min margin (scale up to at most $50 "
            "  margin when conviction is very high).\n"
            "- You choose leverage freely (1x-20x) based on the pair's volatility.\n"
            "- CLOSE positions (strategy or your own) when momentum reverses or risk warrants.\n"
            "- Protect capital above all. If no compelling edge, output HOLD.\n\n"
            "RISK RULES (HARD, always enforce):\n"
            "- Never exceed combined 6 open positions total (strategy slots + your 3).\n"
            "- Never exceed 20x leverage.\n"
            "- $10 to $50 margin per OPEN.\n"
            "- Respect per-pair leverage sanity for volatile assets.\n"
            "- Do not chase; only act on strong, clear signals.\n\n"
            "Respond ONLY with a JSON object in this exact schema:\n"
            "{\n"
            "  \"analysis\": \"1-2 sentence reasoning\",\n"
            "  \"actions\": [\n"
            "    {\"action\": \"OPEN\", \"pair\": \"ARB-USDT\", \"side\": \"LONG\", \"margin_usdt\": 10, \"leverage\": 5},\n"
            "    {\"action\": \"CLOSE\", \"pair\": \"AMD-USDT\", \"side\": \"LONG\"}\n"
            "  ]\n"
            "}\n"
            "actions may be empty [] for HOLD. Allowed actions: OPEN, CLOSE. NEVER invent pairs."
        )
        user_msg = (
            "Current market + positions context (data as of now):\n"
            "---\n" + ctx + "\n---\n"
            "Decide what to do this tick."
        )
        raw = call_openrouter_llm([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ], max_tokens=3000)
        if raw.startswith("Error"):
            _AGENT["error"] = raw
            _AGENT["last_decision"] = "ERROR"
            _AGENT["last_reason"] = raw
            return {"ok": False, "error": raw}
        # Robust JSON extraction — reasoning models may wrap the JSON in text
        decision = None
        try:
            decision = json.loads(raw)
        except Exception:
            import re as _re
            # Prefer a brace-balanced JSON object: find the first '{' then scan
            # forward counting braces so we capture the OUTER object, not a nested one.
            start = raw.find("{")
            if start != -1:
                depth = 0
                end = -1
                in_str = False
                esc = False
                for i in range(start, len(raw)):
                    c = raw[i]
                    if in_str:
                        if esc:
                            esc = False
                        elif c == "\\":
                            esc = True
                        elif c == '"':
                            in_str = False
                        continue
                    if c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                if end != -1:
                    try:
                        decision = json.loads(raw[start:end + 1])
                    except Exception:
                        decision = None
        if isinstance(decision, dict) and "" in decision and "analysis" not in decision:
            # DeepSeek occasionally emits the analysis under an empty-string key:
            #   {"": "...analysis...", "actions": [...]}
            decision["analysis"] = decision.pop("")
        if decision is None or not isinstance(decision, dict):
            _AGENT["last_decision"] = "PARSE_ERROR"
            _AGENT["last_reason"] = ("non-JSON reply: " + raw[:120])
            _AGENT["error"] = "LLM returned non-JSON"
            return {"ok": False, "error": "JSON parse failed", "raw": raw[:200]}
        actions = decision.get("actions") or []
        results = _agent_execute(decision)
        n_opens = sum(1 for r in results if r.get("action") == "OPEN" and r.get("ok"))
        n_closes = sum(1 for r in results if r.get("action") == "CLOSE" and r.get("ok"))
        if not actions:
            _AGENT["last_decision"] = "HOLD"
            _AGENT["last_reason"] = decision.get("analysis") or "no edge"
        else:
            _AGENT["last_decision"] = f"{n_opens} OPEN / {n_closes} CLOSE"
            _AGENT["last_reason"] = decision.get("analysis") or ""
        # journal
        with _history_lock:
            _AGENT["history"].append({
                "ts": _dt.datetime.now().isoformat(),
                "decision": _AGENT["last_decision"],
                "reason": _AGENT["last_reason"],
                "results": [{"action": r.get("action"), "ok": r.get("ok"), "pair": r.get("pair")} for r in results],
            })
            if len(_AGENT["history"]) > 200:
                _AGENT["history"] = _AGENT["history"][-200:]
        _AGENT["error"] = None
        return {"ok": True, "decision": decision, "results": results,
                "analysis": decision.get("analysis")}
    except json.JSONDecodeError:
        _AGENT["last_decision"] = "PARSE_ERROR"
        _AGENT["last_reason"] = "LLM returned non-JSON"
        _AGENT["error"] = "LLM returned non-JSON"
        return {"ok": False, "error": "JSON parse failed"}
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _AGENT["last_decision"] = "ERROR"
        _AGENT["last_reason"] = str(e)
        _AGENT["error"] = str(e)
        return {"ok": False, "error": str(e)}
    finally:
        _AGENT["running"] = False


def _agent_loop() -> None:
    """Continuous background loop — ticks every freq_sec while enabled."""
    while True:
        try:
            if _AGENT["enabled"] and not _AGENT["running"] and not _AGENT["circuit_breaker"]:
                agent_tick()
        except Exception:
            pass
        time.sleep(_AGENT.get("freq_sec") or 60)


# start the loop thread at module import
threading.Thread(target=_agent_loop, daemon=True).start()


JUPYTER_TOKEN = os.environ.get("JUPYTER_TOKEN", "CHANGE_ME")
QUANTS_LAB_DIR = os.path.expanduser("~/Desktop/humming-bot/quants-lab/research_notebooks")


def jupyter_status() -> Dict[str, Any]:
    """Check if Jupyter is reachable via its REST API."""
    url = f"http://localhost:8888/api/status?token={JUPYTER_TOKEN}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {"running": True, "data": data}
    except Exception as e:
        return {"running": False, "error": str(e)}


def list_notebooks() -> Dict[str, Any]:
    """List .ipynb files under QUANTS_LAB_DIR with name, path, and category."""
    notebooks: List[Dict[str, str]] = []
    root = QUANTS_LAB_DIR
    if not os.path.isdir(root):
        return {"notebooks": [], "error": f"Directory not found: {root}"}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if not fn.endswith(".ipynb") or fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            # category = immediate parent folder name (or "root")
            parts = rel.split(os.sep)
            category = parts[0] if len(parts) > 1 else "root"
            notebooks.append({
                "name": fn,
                "path": rel,
                "category": category,
            })
    return {"notebooks": notebooks, "count": len(notebooks)}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        # keep quiet; print errors only
        msg = format % args if args else str(format)
        if any(x in msg for x in (" 5", "Error", "error")):
            print(f"[http] {msg}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.serve_file("hbot-dashboard.html", "text/html")
        elif path.startswith("/api/"):
            self.handle_api_get()
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0].startswith("/api/"):
            self.handle_api_post()
        else:
            self.send_error(404)

    def serve_file(self, filename: str, content_type: str):
        filepath = os.path.join(BASE_DIR, filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def handle_api_get(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path == "/api/strategies":
                enabled = [k for k, v in _strategies.items() if v.get("active")]
                self.send_json({
                    "strategies": _strategies,
                    "enabled_strategies": enabled,
                })
                return

            if path == "/api/health":
                self.send_json({"ok": True, "port": PORT, "bot": BOT_NAME, "config": BOT_CONFIG})
                return

            if path == "/api/bot/status":
                self.send_json(cli_bot_status())
                return

            if path == "/api/e4/status":
                self.send_json(e4_bot_status())
                return

            if path == "/api/universe":
                self.send_json(load_universe_file())
                return

            if path == "/api/scan":
                force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
                self.send_json(build_scan(force=force))
                return

            if path == "/api/bot/signals":
                self.send_json(get_bot_signals())
                return

            if path == "/api/market":
                tickers = get_tickers()
                market = [tickers.get(s) or {"symbol": s, "last_price": 0, "change_24h": 0} for s in SYMBOLS]
                self.send_json({"market": market, "symbols": SYMBOLS})
                return

            if path.startswith("/api/chart/"):
                sym = path[len("/api/chart/"):]
                tf = qs.get("tf", ["15m"])[0]
                self.send_json(chart_payload(sym, tf))
                return

            if path.startswith("/api/signal"):
                symbol = qs.get("symbol", ["BTC-USDT"])[0]
                strategy = qs.get("strategy", [None])[0]
                if strategy:
                    sig = update_persistence(compute_strategy_signal(strategy, symbol))
                else:
                    # best among enabled
                    enabled = [k for k, v in _strategies.items() if v.get("active")] or list(_strategies)
                    best = None
                    for sk in enabled:
                        s = update_persistence(compute_strategy_signal(sk, symbol))
                        if best is None or (
                            (s.get("ready"), s.get("passing", 0), abs(s.get("composite_score") or 0))
                            > (best.get("ready"), best.get("passing", 0), abs(best.get("composite_score") or 0))
                        ):
                            best = s
                    sig = best or update_persistence(compute_strategy_signal("ROC_RSI", symbol))
                self.send_json(sig)
                return

            if path.startswith("/api/strategy/signals"):
                symbol = qs.get("symbol", ["BTC-USDT"])[0]
                strategy = qs.get("strategy", ["ROC_RSI"])[0]
                sig = update_persistence(compute_strategy_signal(strategy, symbol))
                self.send_json(sig)
                return

            if path == "/api/performance":
                self.send_json(compute_performance())
                return

            if path == "/api/condor/status":
                self.send_json(get_condor_status())
                return

            # Engine 2 truth path: Condor owns the independent live LLM loop.
            # Keep this separate from the retired dashboard-native /api/agent/* loop.
            if path == "/api/engine2/status":
                agents = condor_api_get("agents")
                if isinstance(agents, dict) and agents.get("error"):
                    self.send_json({"ok": False, "error": agents.get("error")}, 502)
                    return
                engine2 = None
                for agent in agents if isinstance(agents, list) else []:
                    if isinstance(agent, dict) and agent.get("slug") == "v37_risk_manager":
                        engine2 = agent
                        break
                if not isinstance(engine2, dict):
                    self.send_json({"ok": False, "error": "Engine 2 definition not found"}, 404)
                    return
                strategy = next((s for s in engine2.get("strategies", [])
                                 if isinstance(s, dict) and s.get("slug") == "disciplined_perps_strategy"), {})
                instance = (strategy.get("instances") or [{}])[0] if isinstance(strategy, dict) else {}
                self.send_json({
                    "ok": True,
                    "enabled": strategy.get("status") == "running",
                    "running": strategy.get("status") == "running",
                    "agent": engine2.get("name", "V37 Risk Manager"),
                    "model": instance.get("agent_key") or engine2.get("agent_key") or "—",
                    "frequency_sec": instance.get("frequency_sec", 60),
                    "slots": {"used": instance.get("open_count", 0),
                              "max": (instance.get("risk_limits") or {}).get("max_open_executors", 3)},
                    "pnl": instance.get("total_pnl", 0.0),
                    "tick_count": instance.get("tick_count", 0),
                    "execution_mode": instance.get("execution_mode", "—"),
                    "last_action": _read_risk_manager_status().get("last_action"),
                })
                return

            if path == "/api/engine2/history":
                import glob
                import re
                import json as _json
                base = os.path.expanduser("~/Desktop/humming-bot/condor/agents/v37_risk_manager/strategies/disciplined_perps_strategy/sessions")
                files = sorted(glob.glob(os.path.join(base, "session_*", "snapshots", "*.md")),
                               key=os.path.getmtime, reverse=True)[:30]
                history = []
                for filename in files:
                    try:
                        text = open(filename).read()
                        decision = None
                        for block in re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S):
                            try:
                                obj = _json.loads(block)
                                if isinstance(obj, dict) and "action" in obj and "thesis" in obj:
                                    decision = obj
                                    break
                            except Exception:
                                continue
                        if decision:
                            history.append({
                                "file": os.path.basename(filename),
                                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(filename))),
                                "action": decision.get("action", "HOLD"),
                                "symbol": decision.get("symbol"),
                                "side": decision.get("side"),
                                "confidence": decision.get("confidence"),
                                "thesis": decision.get("thesis", ""),
                            })
                    except Exception:
                        continue
                self.send_json({"ok": True, "history": history, "count": len(history)})
                return

            if path == "/api/journal":
                try:
                    limit = int(qs.get("limit", ["100"])[0])
                except Exception:
                    limit = 100
                self.send_json(get_journal(limit=min(max(limit, 1), 500)))
                return

            if path.startswith("/api/proxy"):
                target = path[len("/api/proxy"):] or "/"
                # keep query string
                if parsed.query:
                    target = target + "?" + parsed.query
                self.send_json(hbot_get(target))
                return

            # --- Condor API proxy GET endpoints ---
            _condor_get_routes = {
                "/api/condor/portfolio": "servers/local/portfolio",
                "/api/condor/positions": "servers/local/positions",
                "/api/condor/bots": "servers/local/bots",
                "/api/condor/executors": "servers/local/executors",
                "/api/condor/reports": "reports",
                "/api/condor/routines": "routines",
                "/api/condor/servers": "settings/servers",
            }
            if path in _condor_get_routes:
                self.send_json(condor_api_get(_condor_get_routes[path]))
                return

            # --- Condor API proxy GET: additional routes ---
            _condor_extra_get = {
                "/api/condor/backtesting/saved": "servers/local/backtesting/saved",
                "/api/condor/backtesting/tasks": "servers/local/backtesting/tasks",
                "/api/condor/routines/instances": "routines/instances",
                "/api/condor/settings/credentials": "settings/credentials",
                "/api/condor/settings/connectors": "settings/connectors",
                "/api/condor/settings/gateway/status": "settings/gateway/status",
                "/api/condor/market/prices": "servers/local/market/prices",
                "/api/condor/market/tickers": "servers/local/market/tickers",
                "/api/condor/controller-performance": "servers/local/controller-performance/latest",
            }
            if path in _condor_extra_get:
                self.send_json(condor_api_get(_condor_extra_get[path]))
                return

            # Path-parameterized Condor GET routes
            if path.startswith("/api/condor/reports/"):
                remainder = path[len("/api/condor/reports/"):]
                if remainder.endswith("/html"):
                    report_id = remainder[: -len("/html")]
                    self.send_json(condor_api_get(f"reports/{report_id}/html"))
                else:
                    self.send_json(condor_api_get(f"reports/{remainder}"))
                return

            # --- Quants Lab / Portfolio Analytics endpoints ---

            if path == "/api/jupyter/status":
                self.send_json(jupyter_status())
                return

            if path == "/api/notebooks":
                self.send_json(list_notebooks())
                return

            if path == "/api/portfolio/summary":
                self.send_json(hbot_get("/portfolio/summary"))
                return

            # --- Autonomous Agent endpoints ---
            if path == "/api/agent/status":
                # compute age of last decision
                age = (time.time() - _AGENT["last_tick"]) if _AGENT["last_tick"] else None
                self.send_json({
                    "enabled": bool(_AGENT["enabled"]),
                    "freq_sec": _AGENT["freq_sec"],
                    "model": _AGENT["model"],
                    "slots": _AGENT["slots"],
                    "running": bool(_AGENT["running"]),
                    "last_decision": _AGENT["last_decision"],
                    "last_reason": _AGENT["last_reason"],
                    "last_age_s": age,
                    "agent_pnl": _AGENT["agent_pnl"],
                    "agent_trades": _AGENT["agent_trades"],
                    "consecutive_losses": _AGENT["consecutive_losses"],
                    "circuit_breaker": bool(_AGENT["circuit_breaker"]),
                    "dry_run": bool(_AGENT["dry_run"]),
                    "error": _AGENT["error"],
                    "api_key_set": bool(_AGENT_API_KEY),
                })
                return
            if path == "/api/agent/history":
                with _history_lock:
                    hist = list(_AGENT["history"])
                self.send_json({"history": hist, "count": len(hist)})
                return

            self.send_json({"error": "Unknown endpoint", "path": path}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def handle_api_post(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self.read_body()

            if path == "/api/strategy":
                strat = str(body.get("strategy") or "").upper()
                active = bool(body.get("active", False))
                if strat not in _strategies:
                    self.send_json({"ok": False, "error": f"Unknown strategy: {strat}"})
                    return
                # prevent all-off
                if not active:
                    others_on = any(k != strat and v.get("active") for k, v in _strategies.items())
                    if not others_on:
                        self.send_json({"ok": False, "error": "At least one strategy must stay ON"})
                        return
                _strategies[strat]["active"] = active
                _save_strategy_state()
                # bust scan cache
                global _scan_cache
                _scan_cache = (0.0, {})
                enabled = [k for k, v in _strategies.items() if v.get("active")]
                self.send_json({
                    "ok": True, "strategy": strat, "active": active,
                    "enabled_strategies": enabled,
                })
                return

            if path == "/api/e4/start":
                confirm = bool(body.get("confirm_live", False))
                if not confirm:
                    self.send_json({
                        "ok": False,
                        "error": "confirm_live required",
                        "hint": "Engine 4 start places LIVE Bitget quotes on BTC+ETH. Pass confirm_live=true after UI confirm.",
                    })
                    return
                replace = bool(body.get("replace", False))
                self.send_json(e4_bot_start(replace=replace))
                return

            if path == "/api/e4/stop":
                self.send_json(e4_bot_stop())
                return

            if path == "/api/bot/start":
                # Real start via hbot CLI (import + start). Confirm flag required from UI for safety.
                confirm = bool(body.get("confirm_live", False))
                if not confirm:
                    self.send_json({
                        "ok": False,
                        "error": "confirm_live required",
                        "hint": (
                            f"Start places LIVE Bitget orders across universe "
                            f"({len(SYMBOLS)} pairs, max {MAX_OPEN_POSITIONS} slots, "
                            f"${_UNIVERSE.get('position_size_quote', 10)} each). "
                            "Pass confirm_live=true after UI confirm."
                        ),
                        "config": body.get("config") or BOT_CONFIG,
                        "universe_size": len(SYMBOLS),
                        "max_open_positions": MAX_OPEN_POSITIONS,
                    })
                    return
                config = body.get("config") or body.get("conf") or BOT_CONFIG
                # Default replace=False so re-clicks don't kill a healthy bot mid-init.
                # Pass replace=true only when intentionally restarting to load new code.
                replace = bool(body.get("replace", False))

                # Single-flight: if a start is already running, do NOT spawn another
                # (replace would kill the first mid-init — root cause of "won't stay on").
                with _bot_op_lock:
                    phase = _bot_op.get("phase") or "idle"
                    if phase == "starting":
                        self.send_json({
                            "ok": True,
                            "phase": "starting",
                            "message": "Start already in progress — keep waiting…",
                            "config": str(config),
                            "replace": replace,
                            "started_at": _bot_op.get("started_at"),
                        })
                        return
                    if phase == "stopping":
                        self.send_json({
                            "ok": False,
                            "error": "Bot is currently stopping — wait a few seconds then Start again",
                            "phase": "stopping",
                        })
                        return

                current_status = cli_bot_status(force=True)
                if current_status.get("running") and not replace:
                    self.send_json({
                        "ok": True,
                        "message": "Bot already RUNNING",
                        "status": current_status,
                        "running": True,
                    })
                    return

                with _bot_op_lock:
                    _bot_op["phase"] = "starting"
                    _bot_op["started_at"] = time.time()
                    _bot_op["result"] = None
                    _bot_op["error"] = None

                _box = {"result": None, "done": False}

                def _bg_start():
                    try:
                        res = cli_bot_start(config=str(config), replace=replace)
                        _box["result"] = res
                        with _bot_op_lock:
                            _bot_op["result"] = res
                    except Exception as e:
                        err = {"ok": False, "error": str(e), "phase": "start"}
                        _box["result"] = err
                        with _bot_op_lock:
                            _bot_op["result"] = err
                            _bot_op["error"] = str(e)
                    finally:
                        _box["done"] = True
                        with _bot_op_lock:
                            # Keep "starting" if soft-start still initializing; else idle
                            res = _box.get("result") or {}
                            if res.get("running") or (res.get("ok") and res.get("phase") != "starting"):
                                _bot_op["phase"] = "idle"
                            elif not res.get("ok"):
                                _bot_op["phase"] = "idle"
                            else:
                                # soft starting — leave phase=starting briefly for UI; clear after 90s via status
                                _bot_op["phase"] = "starting"
                        _invalidate_status_cache()
                        # Auto-clear stuck starting phase after 3 minutes
                        def _clear_phase():
                            time.sleep(180)
                            with _bot_op_lock:
                                if _bot_op.get("phase") == "starting":
                                    st2 = cli_bot_status(force=True)
                                    if st2.get("running"):
                                        _bot_op["phase"] = "idle"
                                    else:
                                        _bot_op["phase"] = "idle"
                        threading.Thread(target=_clear_phase, daemon=True).start()

                t = threading.Thread(target=_bg_start, daemon=True)
                t.start()
                # Give it a few seconds to see if it fails fast
                t.join(timeout=8)
                if _box["done"] and _box["result"]:
                    # Finished within 8s — return actual result
                    self.send_json(_box["result"])
                else:
                    # Still running — return "starting" and let frontend poll
                    self.send_json({
                        "ok": True,
                        "phase": "starting",
                        "message": "Bot starting in background (24 pairs, ~30-90s to initialize)…",
                        "config": str(config),
                        "replace": replace,
                    })
                return

            if path == "/api/bot/stop":
                with _bot_op_lock:
                    if _bot_op.get("phase") == "starting":
                        # Allow stop during start — mark stopping
                        pass
                    _bot_op["phase"] = "stopping"
                    _bot_op["started_at"] = time.time()
                try:
                    result = cli_bot_stop()
                finally:
                    with _bot_op_lock:
                        _bot_op["phase"] = "idle"
                    _invalidate_status_cache()
                self.send_json(result)
                return

            if path == "/api/position/close":
                pair = body.get("pair", "")
                side = body.get("side", "")
                # Use Hummingbot API to close position via market order
                close_side = "SELL" if side in ("BUY", "LONG") else "BUY"
                result = hbot_post("/trading/orders", {
                    "account_name": "master_account",
                    "connector_name": "bitget_perpetual",
                    "trading_pair": pair,
                    "side": close_side,
                    "order_type": "MARKET",
                    "is_global_kwargs": True,
                })
                if result.get("error"):
                    self.send_json({"ok": False, "message": str(result.get("error")), "detail": result})
                else:
                    self.send_json({"ok": True, "message": f"Close order sent for {pair}", "detail": result})
                return

            if path == "/api/bot/status":
                self.send_json(cli_bot_status())
                return

            if path.startswith("/api/proxy"):
                target = path[len("/api/proxy"):] or "/"
                if parsed.query:
                    target = target + "?" + parsed.query
                self.send_json(hbot_post(target, body))
                return

            # --- Condor AI POST endpoints ---

            if path == "/api/condor/chat":
                message = body.get("message", "")
                if not message:
                    self.send_json({"error": "message required"}, 400)
                    return
                messages = [
                    {"role": "system", "content": _XIAOMI_SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ]
                reply = call_xiaomi_llm(messages)
                self.send_json({"reply": reply})
                return

            if path == "/api/condor/find-trade":
                # Bot-truth first. Dashboard scan is fallback only.
                best = None
                try:
                    bot = get_bot_signals()
                except Exception:
                    bot = {}
                if bot.get("available"):
                    cands = list(bot.get("candidates") or [])
                    ready = [c for c in cands if c.get("ready")]
                    if ready:
                        best = sorted(ready, key=lambda s: abs(float(s.get("score") or 0)), reverse=True)[0]
                    else:
                        self.send_json({"trade": {"raw_reply": "NONE — bot has no READY setup"}})
                        return
                if best is None:
                    scan = build_scan(force=bool(body.get("force", False)))
                    market = scan.get("market") or []
                    ready = [s for s in market if s.get("ready")]
                    if not ready:
                        self.send_json({"trade": {"raw_reply": "NONE — no READY setup"}})
                        return
                    best = ready[0]
                price = best.get("last_price") or best.get("price") or 0
                if not price:
                    try:
                        t = (get_tickers() or {}).get(best.get("symbol") or "") or {}
                        price = float(t.get("last_price") or 0)
                    except Exception:
                        price = 0
                try:
                    price = float(price or 0)
                except (TypeError, ValueError):
                    price = 0.0
                direction = best.get("direction", "LONG")
                leverage = best.get("leverage", 5)
                score = best.get("score", 0)
                strat = best.get("strategy") or ((best.get("strategies") or ["—"])[0] if isinstance(best.get("strategies"), list) else "—")
                # Calculate SL/TP based on direction (v3.7 tuned: 0.8% SL / 1.6% TP)
                if direction == "LONG":
                    side = "BUY"
                    sl = round(price * 0.992, 6) if price else 0
                    tp = round(price * 1.016, 6) if price else 0
                else:
                    side = "SELL"
                    sl = round(price * 1.008, 6) if price else 0
                    tp = round(price * 0.984, 6) if price else 0
                chg = best.get("change_24h")
                try:
                    chg_s = f"{float(chg):.2f}%" if chg is not None else "—"
                except (TypeError, ValueError):
                    chg_s = "—"
                trade = {
                    "symbol": best.get("symbol", "UNKNOWN"),
                    "side": side,
                    "strategy": strat,
                    "entry": price,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "leverage": leverage,
                    "score": score,
                    "rationale": (
                        f"BOT-truth READY: {best.get('symbol')} {direction} via {strat} "
                        f"(score {score}, 24h {chg_s})"
                    ),
                }
                self.send_json({"trade": trade})
                return

            if path == "/api/condor/analyze-position":
                symbol = body.get("symbol", "UNKNOWN")
                side = body.get("side", "LONG")
                entry = body.get("entry", 0)
                pnl = body.get("pnl", 0)
                extra = {k: v for k, v in body.items() if k not in ("symbol", "side", "entry", "pnl")}
                prompt = (
                    f"Position analysis request:\n"
                    f"- Symbol: {symbol}\n"
                    f"- Side: {side}\n"
                    f"- Entry price: {entry}\n"
                    f"- Current PnL: {pnl}\n"
                )
                if extra:
                    prompt += f"- Additional info: {json.dumps(extra)}\n"
                prompt += (
                    "\nProvide:\n"
                    "1. A brief assessment of the position\n"
                    "2. Specific action recommendations (hold, add, partial close, full close, adjust SL/TP)\n"
                    "3. Risk level (low/medium/high)\n\n"
                    "Return a JSON object: {\"suggestion\": \"...\", \"actions\": [\"action1\", \"action2\", ...]}"
                )
                messages = [
                    {"role": "system", "content": _XIAOMI_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
                reply = call_xiaomi_llm(messages, max_tokens=2048)
                result = {"suggestion": reply, "actions": []}
                try:
                    start = reply.find("{")
                    end = reply.rfind("}") + 1
                    if start >= 0 and end > start:
                        result = json.loads(reply[start:end])
                except Exception:
                    pass
                self.send_json(result)
                return

            # --- Condor API proxy POST: additional routes ---

            if path == "/api/condor/backtesting/run":
                self.send_json(condor_api_post("servers/local/backtesting/tasks", body))
                return

            if path == "/api/condor/routines/run":
                self.send_json(condor_api_post("routines/run", body))
                return

            if path == "/api/condor/trade":
                self.send_json(condor_api_post("servers/local/executors", body))
                return

            if path == "/api/condor/bots/deploy":
                self.send_json(condor_api_post("servers/local/bots/deploy", body))
                return

            if path.startswith("/api/condor/bots/") and path.endswith("/stop"):
                bot_name = path[len("/api/condor/bots/"):-len("/stop")]
                self.send_json(condor_api_post(f"servers/local/bots/{bot_name}/stop", {}))
                return

            if path.startswith("/api/condor/executors/") and path.endswith("/stop"):
                executor_id = path[len("/api/condor/executors/"):-len("/stop")]
                self.send_json(condor_api_post(f"servers/local/executors/{executor_id}/stop", {}))
                return

            # --- Engine 2 controls: Condor-owned LLM risk manager ---
            _engine2_base = "agents/v37_risk_manager/strategies/disciplined_perps_strategy"
            if path == "/api/engine2/start":
                config = {
                    "agent_key": "openrouter:YOUR_MODEL_HERE",
                    "frequency_sec": 600,
                    "execution_mode": "loop",
                    "total_amount_quote": 30.0,
                    "trading_context": (
                        "LIVE Bitget USDT-M perpetuals. Engine 2 is an independent LLM risk manager: "
                        "every 60 seconds review all Engine-1 and Engine-2 exchange positions; maintain "
                        "up to 3 dedicated Engine-2 positions; trade only via manage_executors."
                    ),
                    "risk_limits": {
                        "max_position_size_quote": 10.0,
                        "max_open_executors": 3,
                        "max_drawdown_pct": -1.0,
                        "shutdown_drawdown_pct": -1.0,
                    },
                }
                result = condor_api_post(f"{_engine2_base}/start", {"chat_id": 0, "config": config})
                self.send_json({"ok": "error" not in result, "result": result})
                return
            if path == "/api/engine2/stop":
                result = condor_api_post(f"{_engine2_base}/stop", {})
                self.send_json({"ok": "error" not in result, "result": result})
                return

            # --- Legacy dashboard-native loop (retired; preserve hard-disabled) ---
            if path == "/api/agent/toggle":
                self.send_json({"ok": False, "error": "Retired: use Condor Engine 2 controls."}, 410)
                return
            if path == "/api/agent/run-now":
                self.send_json({"ok": False, "error": "Retired: use Condor Engine 2 controls."}, 410)
                return
            if path == "/api/agent/toggle":
                _AGENT["enabled"] = bool(body.get("on", not _AGENT["enabled"]))
                if "freq_sec" in body:
                    _AGENT["freq_sec"] = max(10, int(body.get("freq_sec", 60)))
                _agent_save_state()
                self.send_json({
                    "ok": True, "enabled": _AGENT["enabled"],
                    "freq_sec": _AGENT["freq_sec"],
                    "message": ("Autonomous agent ENABLED — live trading every %ds" % _AGENT["freq_sec"])
                    if _AGENT["enabled"] else "Autonomous agent disabled",
                })
                return
            if path == "/api/agent/run-now":
                # Fire one dashboard-native Engine 2 decision cycle synchronously.
                result = agent_tick()
                self.send_json({**{"ok": True}, **result})
                return
            if path == "/api/agent/reset-breaker":
                _AGENT["circuit_breaker"] = False
                _AGENT["consecutive_losses"] = 0
                _agent_save_state()
                self.send_json({"ok": True, "message": "Circuit breaker reset"})
                return

            self.send_json({"error": "Unknown endpoint", "path": path}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    _load_strategy_state()
    _agent_load_state()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"🚀 HUMMINGBOT Dashboard Server running on http://localhost:{PORT}")
    print(f"   Dashboard: http://localhost:{PORT}")
    print(f"   Scan API:  http://localhost:{PORT}/api/scan")
    print(f"   HBOT API:  {HBOT_API} (bot_name={BOT_NAME})")
    print("   Press Ctrl+C to stop")
    # warm scan in background
    def _warm():
        try:
            build_scan(force=True)
            print("   ✓ initial scan ready")
        except Exception as e:
            print(f"   ⚠ initial scan failed: {e}")
    threading.Thread(target=_warm, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹ Server stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
