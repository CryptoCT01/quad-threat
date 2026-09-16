
    @field_validator("trailing_stop", mode="before")
    @classmethod
    def _ts(cls, v):
        return parse_trailing(v)

    @field_validator("position_mode", mode="before")
    @classmethod
    def _pm(cls, v):
        return parse_enum_value(PositionMode, v, "position_mode")

    @field_validator("stop_loss", "take_profit", "position_size_quote", "total_amount_quote", mode="before")
    @classmethod
    def _dec(cls, v):
        if v is None or v == "":
            return None
        return Decimal(str(v))

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            time_limit=self.time_limit,
            trailing_stop=self.trailing_stop,
            open_order_type=OrderType.MARKET,
            take_profit_order_type=OrderType.MARKET,
            stop_loss_order_type=OrderType.MARKET,
            time_limit_order_type=OrderType.MARKET,
        )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        pairs = self.trading_pairs or []
        if not pairs:
            uni = load_universe()
            pairs = [x["symbol"] for x in (uni.get("crypto") or []) + (uni.get("commodities") or []) + (uni.get("stocks") or [])]
        for p in pairs:
            markets = markets.add_or_update(self.connector_name, p)
        return markets


class V37ScalpMultiController(ControllerBase):
    def __init__(self, config: V37ScalpMultiConfig, *args, **kwargs):
        # hydrate pairs/leverage from universe if needed
        uni = load_universe()
        if not config.trading_pairs:
            config.trading_pairs = [
                x["symbol"] for x in (uni.get("crypto") or []) + (uni.get("commodities") or []) + (uni.get("stocks") or [])
            ]
        if not config.leverage_map:
            config.leverage_map = {
                x["symbol"]: int(x.get("leverage") or 5)
                for x in (uni.get("crypto") or []) + (uni.get("commodities") or []) + (uni.get("stocks") or [])
            }
        if uni.get("max_open_positions"):
            config.max_open_positions = int(uni["max_open_positions"])
        if uni.get("position_size_quote"):
            config.position_size_quote = Decimal(str(uni["position_size_quote"]))
        if uni.get("cooldown_time") is not None:
            config.cooldown_time = int(uni["cooldown_time"])
        if uni.get("stop_loss") is not None:
            config.stop_loss = Decimal(str(uni["stop_loss"]))
        if uni.get("take_profit") is not None:
            config.take_profit = Decimal(str(uni["take_profit"]))
        if uni.get("time_limit") is not None:
            config.time_limit = int(uni["time_limit"])
        if uni.get("trailing_stop"):
            ts = parse_trailing(uni["trailing_stop"])
            if ts is not None:
                config.trailing_stop = ts
        if uni.get("score_threshold") is not None:
            config.score_threshold = float(uni["score_threshold"])
        # Keep total_amount_quote aligned with slots × margin
        try:
            config.total_amount_quote = Decimal(str(config.position_size_quote)) * Decimal(
                str(config.max_open_positions)
            )
        except Exception:
            pass

        super().__init__(config, *args, **kwargs)
        self.config = config
        self._enabled: Set[str] = {"SUPER_A", "ROC_RSI", "BB_VOL"}
        self._enabled_ts = 0.0
        self._last_entry_ts: Dict[str, float] = {}
        # Set leverage on exchange for all pairs BEFORE any positions open
        self._set_all_leverage()
        # rate sources for all pairs
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_name, trading_pair=p)
            for p in config.trading_pairs
        ])
        self.processed_data = {
            "candidates": [],
            "open_slots": config.max_open_positions,
            "enabled": sorted(self._enabled),
            "universe": config.trading_pairs,
            "leverage_map": config.leverage_map,
        }

    def _set_all_leverage(self):
        """Set leverage on the exchange for all pairs via Hummingbot API + connector."""
        import urllib.request
        import json as _json
        ok = 0
        fail = 0
        for pair, lev in self.config.leverage_map.items():
            if self._ensure_leverage(pair, int(lev)):
                ok += 1
            else:
                fail += 1
        try:
            self.logger().info(f"Leverage set ok={ok} fail={fail} (target map size={len(self.config.leverage_map)})")
        except Exception:
            pass

    def _ensure_leverage(self, pair: str, lev: int) -> bool:
        """Force exchange leverage for one pair. Returns True on success.

        Critical: PositionExecutorConfig.leverage is metadata only. If Bitget
        stays at a lower leverage than we size for, margin locked = notional/actual_lev
        (e.g. SOL sized for 10x/$10 margin opens $100 notional → $20 margin at 5x).
        """
        lev = int(lev)
        # 1) Hummingbot API first — returns verified success/failure
        try:
            import urllib.request
            import json as _json
            data = _json.dumps({"trading_pair": pair, "leverage": lev}).encode()
            req = urllib.request.Request(
                f"http://localhost:8000/trading/master_account/{self.config.connector_name}/leverage",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Basic YWRtaW46YWRtaW4="},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode()
                if "success" in body.lower() or resp.status == 200:
                    # Also nudge connector if available (async; best-effort)
                    try:
                        mdp = getattr(self, "market_data_provider", None)
                        connectors = getattr(mdp, "connectors", None) or {}
                        conn = connectors.get(self.config.connector_name)
                        if conn is not None and hasattr(conn, "set_leverage"):
                            conn.set_leverage(pair, lev)
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        # 2) Connector-only fallback
        try:
            mdp = getattr(self, "market_data_provider", None)
            connectors = getattr(mdp, "connectors", None) or {}
            conn = connectors.get(self.config.connector_name)
            if conn is not None and hasattr(conn, "set_leverage"):
                conn.set_leverage(pair, lev)
                return True
        except Exception:
            pass
        return False

    def _position_amount(self, pair: str, price) -> Decimal:
        """Size position so MARGIN ≈ position_size_quote (default $10).

        amount = margin * leverage / price
        notional = margin * leverage
        """
        margin = Decimal(str(self.config.position_size_quote))
        lev = Decimal(str(self._lev(pair)))
        px = Decimal(str(price))
        if px <= 0:
            raise ValueError("price must be > 0")
        # Hard cap: never size above margin * lev (exactly the budget)
        amount = (margin * lev) / px
        return amount

    def get_candles_config(self) -> List[CandlesConfig]:
        out: List[CandlesConfig] = []
        for p in self.config.trading_pairs:
            out.append(CandlesConfig(
                connector=self.config.connector_name, trading_pair=p, interval="15m", max_records=100
            ))
            out.append(CandlesConfig(
                connector=self.config.connector_name, trading_pair=p, interval="1h", max_records=60
            ))
        return out

    def _refresh_enabled(self):
        now = time.time()
        if now - self._enabled_ts < 2:
            return
        self._enabled = load_enabled_strategies()
        self._enabled_ts = now

    def _lev(self, pair: str) -> int:
        return int(self.config.leverage_map.get(pair) or self.config.leverage_default or 5)

    def _active_pairs(self) -> Set[str]:
        """Pairs that currently occupy a slot.

        Counts live scalp executors only. YOU leftovers and keep_position
        holds do not occupy Engine 1 slots.
        """
        active = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active,
        )
        pairs: Set[str] = set()
        for e in active:
            tp = getattr(e, "trading_pair", None)
            if not tp and getattr(e, "config", None) is not None:
                tp = getattr(e.config, "trading_pair", None)
            if tp:
                pairs.add(str(tp))

        # YOU leftovers (HOOD etc.) and keep_position holds are NOT E1 slots.
        # Only a live scalp executor occupies a slot.
        pairs -= {"BTC-USDT", "ETH-USDT", "XRP-USDT", "DOGE-USDT", "SOL-USDT"}
        return pairs

    def _slots_used(self) -> int:
        return len(self._active_pairs())

    def _closed_df(self, df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Drop the forming candle. All indicators must use the last closed bar."""
        if df is None or len(df) < 2:
            return None
        return df.iloc[:-1]

    def _signals_for_df(self, df: pd.DataFrame, h1: Optional[pd.DataFrame], enabled: Set[str]) -> Tuple[int, Dict[str, int], float]:
        """Return combined signal, votes, score (0..1).

        Rules (live audit Aug 27):
        - Closed 15m/1h candles only (forming-bar flicker was the <15m −$36 bucket).
        - ROC_RSI is momentum + RSI timing, not mean-reversion (old rsi<40 AND roc>0 never fired).
        - Need ≥2 engines in the same direction. Solo SUPER_A at 0.58 is not READY.
        - Longs: H1 EMA12>EMA26. Shorts: H1 EMA12<EMA26 AND close < H1 EMA21.
        """
        d = self._closed_df(df)
        if d is None or len(d) < 32:
            return 0, {}, 0.0
        d = d.copy()
        d["roc"] = d["close"].pct_change(self.config.roc_period) * 100
        d["roc8"] = d["close"].pct_change(self.config.super_a_m15_roc_period) * 100
        delta = d["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / self.config.rsi_period, min_periods=self.config.rsi_period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / self.config.rsi_period, min_periods=self.config.rsi_period, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        d["rsi"] = 100 - 100 / (1 + rs)
        d["bb_mid"] = d["close"].rolling(self.config.bb_period).mean()
        d["bb_std"] = d["close"].rolling(self.config.bb_period).std()
        d["bb_lower"] = d["bb_mid"] - d["bb_std"] * self.config.bb_std
        d["bb_upper"] = d["bb_mid"] + d["bb_std"] * self.config.bb_std
        d["vol_avg"] = d["volume"].rolling(self.config.bb_period).mean()
        d["vol_ratio"] = d["volume"] / d["vol_avg"]
        d["ema21"] = d["close"].ewm(span=21, adjust=False).mean()
        latest = d.iloc[-1]
        votes: Dict[str, int] = {}
        roc_min = float(self.config.roc_min or 0.2)

        if "ROC_RSI" in enabled:
            # Momentum direction + RSI timing (70/30). Inner band rejects
            # oversold dumps / overbought chases that SUPER_A still allows.
            roc = float(latest["roc"] or 0)
            rsi = float(latest["rsi"] or 50)
            if roc > roc_min and 40.0 <= rsi < 70.0:
                votes["ROC_RSI"] = 1
            elif roc < -roc_min and 30.0 < rsi <= 60.0:
                votes["ROC_RSI"] = -1
            else:
                votes["ROC_RSI"] = 0

        if "BB_VOL" in enabled:
            close = float(latest["close"])
            vr = float(latest["vol_ratio"] or 0)
            if vr >= self.config.vol_ratio_min and close < float(latest["bb_lower"]):
                votes["BB_VOL"] = 1
            elif vr >= self.config.vol_ratio_min and close > float(latest["bb_upper"]):
                votes["BB_VOL"] = -1
            else:
                votes["BB_VOL"] = 0

        h = self._closed_df(h1)
        if "SUPER_A" in enabled:
            sig = 0
            if h is not None and len(h) >= self.config.super_a_h1_roc_period + 2:
                hh = h.copy()
                hh["roc_h1"] = hh["close"].pct_change(self.config.super_a_h1_roc_period) * 100
                roc_m = float(latest["roc8"] or 0)
                roc_h = float(hh.iloc[-1]["roc_h1"] or 0)
                rsi = float(latest["rsi"] or 50)
                ema = float(latest["ema21"])
                close = float(latest["close"])
                agree = (roc_m > 0 and roc_h > 0) or (roc_m < 0 and roc_h < 0)
                comb = abs(roc_m) + abs(roc_h)
                if agree and comb >= self.config.super_a_min_combined_roc:
                    if roc_m > 0 and rsi < 70 and close > ema:
                        sig = 1
                    elif roc_m < 0 and rsi > 30 and close < ema:
                        sig = -1
            votes["SUPER_A"] = sig

        vals = [v for v in votes.values() if v != 0]
        # Two engines must agree. One vote (even a strong SUPER_A) is not a trade.
        if len(vals) < 2:
            signal = 0
        elif all(v > 0 for v in vals):
            signal = 1
        elif all(v < 0 for v in vals):
            signal = -1
        else:
            signal = 0

        # Longs with H1 trend. Shorts need a clearer bear (12/26 + close < EMA21).
        if signal != 0 and h is not None and len(h) >= 30:
            try:
                h1_close = h["close"]
                h1_fast = float(h1_close.ewm(span=12, adjust=False).mean().iloc[-1])
                h1_slow = float(h1_close.ewm(span=26, adjust=False).mean().iloc[-1])
                h1_ema21 = float(h1_close.ewm(span=21, adjust=False).mean().iloc[-1])
                h1_last = float(h1_close.iloc[-1])
                h1_bullish = h1_fast > h1_slow
                h1_bearish_strong = (h1_fast < h1_slow) and (h1_last < h1_ema21)
                if signal > 0 and not h1_bullish:
                    signal = 0
                elif signal < 0 and not h1_bearish_strong:
                    signal = 0
            except Exception:
                pass

        if signal == 0:
            score = 0.0
        else:
            agree_n = sum(1 for v in votes.values() if v == signal)
            total = max(len(votes), 1)
            score = agree_n / total
            score = min(1.0, score + min(abs(float(latest.get("roc") or 0)) / 2.0, 0.25))
        return signal, votes, float(score)

    async def update_processed_data(self):
        self._refresh_enabled()
        enabled = set(self._enabled)
        candidates = []
        if not enabled:
            self.processed_data = {
                "candidates": [],
                "open_slots": self.config.max_open_positions - self._slots_used(),
                "enabled": [],
                "universe": self.config.trading_pairs,
                "leverage_map": self.config.leverage_map,
                "active_pairs": sorted(self._active_pairs()),
            }
            return

        active = self._active_pairs()
        for pair in self.config.trading_pairs:
            try:
                m15 = self.market_data_provider.get_candles_df(
                    self.config.connector_name, pair, "15m", 100
                )
                h1 = self.market_data_provider.get_candles_df(
                    self.config.connector_name, pair, "1h", 60
                )
                signal, votes, score = self._signals_for_df(m15, h1, enabled)
                if signal == 0:
                    continue
                thr = float(getattr(self.config, "score_threshold", 0.55) or 0.55)
                # Tag which engines voted with the final direction (for journal)
                agreeing = sorted([k for k, v in votes.items() if v == signal and v != 0])
                candidates.append({
                    "symbol": pair,
                    "signal": signal,
                    "direction": "LONG" if signal > 0 else "SHORT",
                    "score": round(score, 3),
                    "votes": votes,
                    "strategies": agreeing,
                    "strategy_tag": "+".join(agreeing) if agreeing else "NONE",
                    "leverage": self._lev(pair),
                    "has_position": pair in active,
                    "ready": score >= thr and signal != 0,
                })
            except Exception:
                continue

        candidates.sort(key=lambda x: (not x["has_position"], x["score"]), reverse=True)
        free = max(0, self.config.max_open_positions - len(active))
        self.processed_data = {
            "candidates": candidates,
            "open_slots": free,
            "enabled": sorted(enabled),
            "universe": self.config.trading_pairs,
            "leverage_map": self.config.leverage_map,
            "active_pairs": sorted(active),
            "max_open_positions": self.config.max_open_positions,
        }

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        actions.extend(self.create_actions_proposal())
        return actions

    def _effective_cooldown(self, pair: str) -> float:
        """Adaptive cooldown (P5): double the base cooldown after a STOP_LOSS on
        this pair, so we don't revenge re-enter the same chop. Normal cooldown
        after wins/flats. Keeps all symbols tradeable (no list kills)."""
        base = float(getattr(self.config, "cooldown_time", 1500) or 1500)
        if self._last_close_was_sl(pair):
            return base * 2.0
        return base

    def _last_close_was_sl(self, pair: str) -> bool:
        """True if this pair's most recent closed executor exited via STOP_LOSS."""
        try:
            closed = self.filter_executors(
                executors=self.executors_info,
                filter_func=lambda e: (
                    getattr(e, "trading_pair", None) == pair
                    and not getattr(e, "is_active", True)
                ),
            )
            if not closed:
                return False
            closed.sort(key=lambda e: float(getattr(e, "close_timestamp", 0) or 0), reverse=True)
            cc = getattr(closed[0], "close_type", None)
            if cc is None:
                cc = getattr(getattr(closed[0], "config", None), "close_type", None)
            return "STOP_LOSS" in str(getattr(cc, "name", cc))
        except Exception:
            return False

    def create_actions_proposal(self) -> List[ExecutorAction]:
        out: List[ExecutorAction] = []
        data = self.processed_data or {}
        candidates = data.get("candidates") or []
        active = set(data.get("active_pairs") or self._active_pairs())
        free = int(data.get("open_slots") or 0)
        if free <= 0:
            return out
        now = self.market_data_provider.time()
        opened = 0
        for c in candidates:
            if opened >= free:
                break
            pair = c["symbol"]
            if pair in active or not c.get("ready"):
                continue
            # adaptive cooldown per pair
            if now - self._last_entry_ts.get(pair, 0) < self._effective_cooldown(pair):
                continue
            try:
                price = self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.MidPrice
                )
                if price is None or price <= 0:
                    continue
                lev = self._lev(pair)
                # Ensure exchange leverage matches config BEFORE sizing/opening.
                # If exchange is at 5x but we size for 10x, margin doubles ($20 not $10).
                if not self._ensure_leverage(pair, lev):
                    try:
                        self.logger().warning(f"Could not set leverage {lev}x on {pair} — skip entry")
                    except Exception:
                        pass
                    continue
                # position_size_quote = MARGIN per trade ($10). notional = margin × lev
                amount = self._position_amount(pair, price)
                # Safety: reject if computed notional would need > $10.50 margin at this lev
                notional = amount * Decimal(str(price))
                margin_est = notional / Decimal(str(lev)) if lev else notional
                max_margin = Decimal(str(self.config.position_size_quote)) * Decimal("1.05")
                if margin_est > max_margin:
                    try:
                        self.logger().warning(
                            f"Skip {pair}: margin_est={margin_est:.2f} > max {max_margin} "
                            f"(amt={amount} px={price} lev={lev})"
                        )
                    except Exception:
                        pass
                    continue
                side = TradeType.BUY if c["signal"] > 0 else TradeType.SELL
                # Persist strategy attribution on the executor via level_id
                # Format: STRAT1+STRAT2|score|DIR  e.g. SUPER_A+ROC_RSI|0.58|LONG
                strat_tag = str(c.get("strategy_tag") or "SCALP")
                score_s = f"{float(c.get('score') or 0):.2f}"
                dir_s = "L" if c["signal"] > 0 else "S"
                level_id = f"{strat_tag}|{score_s}|{dir_s}"[:64]
                try:
                    self.logger().info(
                        f"ENTRY {pair} {dir_s} lev={lev}x score={score_s} "
                        f"strats={strat_tag} votes={c.get('votes')}"
                    )
                except Exception:
                    pass
                out.append(CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=PositionExecutorConfig(
                        timestamp=now,
                        connector_name=self.config.connector_name,
                        trading_pair=pair,
                        side=side,
                        entry_price=price,
                        amount=amount,
                        triple_barrier_config=self.config.triple_barrier_config,
                        leverage=lev,
                        level_id=level_id,
                    ),
                ))
                self._last_entry_ts[pair] = now
                active.add(pair)
                opened += 1
            except Exception:
                continue
        return out

    def to_format_status(self) -> List[str]:
        d = self.processed_data or {}
        thr = float(getattr(self.config, "score_threshold", 0.55) or 0.55)
        lines = [
            "═══ v3.7 SCALP MULTI ═══",
            f"Universe: {len(d.get('universe') or [])} pairs · slots {len(d.get('active_pairs') or [])}/{d.get('max_open_positions', 3)}",
            f"Strategies ON: {', '.join(d.get('enabled') or []) or 'NONE'} · thr≥{thr:.2f} · 2 engines · closed 15m",
            f"Open: {', '.join(d.get('active_pairs') or []) or '—'}",
            "Top candidates:",
        ]
        for c in (d.get("candidates") or [])[:8]:
            ready = " READY" if c.get("ready") else ""
            lines.append(
                f"  {c['symbol']:12} {c['direction']:5} score={c['score']:.2f} "
                f"lev={c['leverage']}x {c.get('strategy_tag') or c.get('votes')}"
                f"{' [OPEN]' if c.get('has_position') else ''}{ready}"
            )
        if not d.get("candidates"):
            lines.append("  (no ready signals)")
        return lines
