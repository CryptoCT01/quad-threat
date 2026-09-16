                if px <= 0:
                    continue
                side = str(
                    getattr(pos, "position_side", None)
                    or getattr(pos, "side", None)
                    or ""
                ).upper()
                if raw < 0 or "SHORT" in side or side.endswith("SELL") or side == "SELL":
                    signed = -abs(raw)
                else:
                    signed = abs(raw)
                return signed * px
        except Exception:
            pass
        return Decimal("0")

    def _inventory_deadband_quote(self) -> Decimal:
        """Ignore dust below ~5% of inventory cap (or $5)."""
        cap = Decimal(str(self.config.total_amount_quote or 100))
        return max(cap * Decimal("0.05"), Decimal("5"))

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        """Pull wrong-side quotes immediately when inventory is skewed."""
        inv = self._net_inventory_quote()
        band = self._inventory_deadband_quote()
        block_prefix = None
        if inv <= -band:
            # Short: cancel sells (would add to short)
            block_prefix = "sell"
        elif inv >= band:
            # Long: cancel buys (would add to long)
            block_prefix = "buy"
        if not block_prefix:
            return []

        def _wrong_side(x) -> bool:
            if not x.is_active:
                return False
            info = getattr(x, "custom_info", None) or {}
            lid = info.get("level_id") if isinstance(info, dict) else None
            if not lid:
                # fallback: executor config side
                try:
                    side = str(getattr(getattr(x, "config", None), "side", "") or "").upper()
                    if block_prefix == "sell":
                        return "SELL" in side
                    if block_prefix == "buy":
                        return "BUY" in side
                    return False
                except Exception:
                    return False
            return str(lid).startswith(block_prefix)

        bad = self.filter_executors(
            executors=self.executors_info,
            filter_func=_wrong_side,
        )
        out = [
            StopExecutorAction(controller_id=self.config.id, executor_id=e.id)
            for e in bad
        ]
        # Do NOT early-stop filled cover executors. That paid ~$0.04 fees every
        # refresh and never let the LIMIT TP print (ETH 79/79 EARLY_STOP after
        # the 20:15 tune). Wrong-side quotes still get pulled above.
        return out

    def get_levels_to_execute(self) -> List[str]:
        """Keep a level occupied until the executor is fully terminated.

        Inventory one-side (bleed control):
          short → buy only (cover)
          long  → sell only (distribute)
          flat  → both sides

        Hard cap: still block the side that would push |inv| past 90% of
        total_amount_quote.
        """
        self._ensure_leverage()
        now = self.market_data_provider.time()
        cooldown = self.config.cooldown_time

        def _holds_level(x) -> bool:
            if x.is_active:
                return True
            if getattr(x, "status", None) == RunnableStatus.SHUTTING_DOWN:
                return True
            if x.close_type == CloseType.STOP_LOSS and x.close_timestamp is not None:
                return (now - x.close_timestamp) < cooldown
            return False

        working_levels = self.filter_executors(
            executors=self.executors_info,
            filter_func=_holds_level,
        )
        working_levels_ids = []
        for executor in working_levels:
            info = getattr(executor, "custom_info", None) or {}
            lid = info.get("level_id") if isinstance(info, dict) else None
            if lid:
                working_levels_ids.append(lid)
        levels = self.get_not_active_levels_ids(working_levels_ids)
        cap = Decimal(str(self.config.total_amount_quote or 100))
        inv = self._net_inventory_quote()
        band = self._inventory_deadband_quote()

        # One-side until flat/skewed back inside deadband
        if inv <= -band:
            levels = [x for x in levels if str(x).startswith("buy")]
        elif inv >= band:
            levels = [x for x in levels if str(x).startswith("sell")]

        # Hard cap still applies when flat or after partial cover
        if inv >= cap * Decimal("0.90"):
            levels = [x for x in levels if not str(x).startswith("buy")]
        if inv <= -cap * Decimal("0.90"):
            levels = [x for x in levels if not str(x).startswith("sell")]
        return levels

    # ---------- Inventory-priced quotes (mini A–S) ----------
