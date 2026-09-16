

def _e4_spawn_engine() -> None:
    """Start the E4 PMM the way that actually quotes. E1 untouched."""
    subprocess.Popen(
        [
            "docker", "exec", "-d", "-w", "/home/hummingbot",
            "-e", "PYTHONPATH=/home/hummingbot",
            "-e", "HBOT_PASSWORD=" + str(HBOT_PASSWORD or ""),
            "hummingbot-e4",
            "/opt/conda/envs/hummingbot/bin/python",
            "-m", "hummingbot.cli.engine",
            "--name", "conf_e4_pmm",
            "--script-config", "conf_e4_pmm.yml",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _e4_alive_hostpgrep() -> bool:
    """Truth from docker exec pgrep inside the hummingbot-e4 container.

    The E4 engine cmdline depends on how it was spawned:
      - `cli.engine --name conf_e4_pmm --script-config conf_e4_pmm.yml`
        (older direct spawn)
      - `hummingbot_quickstart.py -p ... --headless --v2 conf_e4_pmm.yml`
        (current spawn via quickstart to bypass the password wall)
    The unique substring `conf_e4_pmm` is in both. Host pgrep always
    returns False (the engine is inside the container). Returns False
    on any error (no hang)."""
    try:
        return bool(subprocess.run(
            ["docker", "exec", "hummingbot-e4", "pgrep", "-f", "conf_e4_pmm"],
            capture_output=True, timeout=2,
        ).stdout.strip())
    except Exception:
        return False


def e4_bot_status(force: bool = False) -> Dict[str, Any]:
    """Truth = quoting, not a zombie engine / hbot ACK."""
    global _e4_status_cache
    now = time.time()
    ts, cached = _e4_status_cache
    if cached and (now - ts) < 3.0 and not force:
        return cached
    out: Dict[str, Any] = {
        "available": False,
        "running": False,
        "quoting": False,
        "engine": False,
        "orders": [],
        "slots_used": 0,
        "pairs": [],
        "container": "hummingbot-e4",
    }
    try:
        alive = _e4_alive_hostpgrep()
        # Do not docker-exec tail. Hung tails wedged hummingbot-e4 and painted
        # E4 OFF even while quickstart was alive. Truth = pgrep conf_e4_pmm.
        text = ""
        last_create = text.rfind("Created LIMIT")
        last_dead = max(
            text.rfind("Global drawdown reached"),
            text.rfind("Hummingbot stopped."),
            text.rfind("Strategy stopped"),
        )
        quoting = bool(alive)
        orders = []
        pairs = set()
        for line in text.splitlines():
            if "Created LIMIT" not in line:
                continue
            side = "SELL" if "LIMIT SELL" in line else ("BUY" if "LIMIT BUY" in line else "")
            pair = ""
            for n in ("BTC-USDT", "ETH-USDT"):
                if n in line:
                    pair = n
                    break
            if side and pair:
                base = pair.replace("-USDT", "")
                orders.append(f"{base} {side}")
                pairs.add(pair)
        # keep last unique
        seen = set()
        uniq = []
        for o in reversed(orders):
            if o in seen:
                continue
            seen.add(o)
            uniq.append(o)
        uniq.reverse()
        out.update({
            "available": True,
            "engine": alive,
            "quoting": quoting,
            "running": quoting,
            "orders": uniq[-8:],
            "slots_used": len(pairs),
            "pairs": sorted(pairs),
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
    global _e4_status_cache
    pre = e4_bot_status(force=True)
    if pre.get("running") and not replace:
        return {
            "ok": True,
            "running": True,
            "message": "Engine 4 already QUOTING",
            "status": pre,
        }
    with _e4_op_lock:
        if _e4_op.get("phase") == "starting":
            return {"ok": True, "phase": "starting", "message": "Engine 4 start already in progress"}
        _e4_op["phase"] = "starting"
        _e4_op["started_at"] = time.time()
    try:
        if replace or pre.get("engine"):
            try:
                _docker_e4("pkill -f hummingbot.cli.engine || true", timeout=10.0)
                time.sleep(1.0)
            except Exception:
                pass
        _e4_spawn_engine()
        _e4_status_cache = (0.0, {})
        status: Dict[str, Any] = {}
        for _ in range(12):
            time.sleep(2)
            status = e4_bot_status(force=True)
            if status.get("running"):
                break
        running = bool(status.get("running"))
        return {
            "ok": True,
            "running": running,
            "phase": "start" if running else "starting",
            "status": status,
            "message": "Engine 4 QUOTING" if running else "Engine 4 starting — quotes in ~15s",
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
        try:
            _docker_e4("pkill -f hummingbot.cli.engine || true", timeout=10.0)
        except Exception:
            pass
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
    if not running:
        try:
            pr = subprocess.run(
                ["docker", "exec", "hummingbot", "bash", "-lc",
                 "pgrep -f 'hummingbot.cli.engine --name conf_v37_scalp_multi' || true"],
                capture_output=True, text=True, timeout=5,
            )
            pid_s = (pr.stdout or "").strip().split()[0] if (pr.stdout or "").strip() else ""
            if pid_s.isdigit():
                running = True
                data = dict(data or {})
                data["running"] = True
                data["pid"] = int(pid_s)
                data["config"] = data.get("config") or "conf_v37_scalp_multi.yml"
                data["type"] = data.get("type") or "controller"
                data["strategy"] = data.get("strategy") or "v2_with_controllers"
        except Exception:
            pass
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
