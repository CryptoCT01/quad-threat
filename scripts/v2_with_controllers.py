                controller.stop()
            except Exception:
                self.logger().exception("Error stopping controller")

        if self.keep_positions_on_stop:
            try:
                actions = []
                for ex in self.get_all_executors():
                    if getattr(ex, "is_active", False):
                        actions.append(StopExecutorAction(
                            controller_id=getattr(ex, "controller_id", None) or "main",
                            executor_id=ex.id,
                            keep_position=True,
                        ))
                if actions:
                    self.logger().info(
                        f"keep_positions_on_stop: holding {len(actions)} executor(s) open on exchange"
                    )
                    self.executor_orchestrator.execute_actions(actions)
            except Exception:
                self.logger().exception("Failed to issue keep_position stops")

        # Parent handles market re-registration, orchestrator.stop wait loop,
        # force_stop_with_position_hold for hung executors, and store_all_*.
        # Because we already put executors in SHUTTING_DOWN/POSITION_HOLD,
        # orchestrator.stop will not flip them to EARLY_STOP.
        if self.listen_to_executor_actions_task:
            self.listen_to_executor_actions_task.cancel()

        active_markets = set(self.active_markets)
        missing_markets = [
            connector for connector in self.connectors.values()
            if connector not in active_markets
        ]
        if missing_markets:
            self.logger().warning(
                "Restoring market registrations required to close active executors during shutdown.")
            self.add_markets(missing_markets)

        await self.executor_orchestrator.stop(self.max_executors_close_attempts)
        self.market_data_provider.stop()
        self.executor_orchestrator.store_all_executors()
        if self.mqtt_enabled:
            self._pub({controller_id: {} for controller_id in self.controllers.keys()})
            self._pub = None

    def control_max_drawdown(self):
        if self.config.max_controller_drawdown_quote:
            self.check_max_controller_drawdown()
        if self.config.max_global_drawdown_quote:
            self.check_max_global_drawdown()

    def check_max_controller_drawdown(self):
        for controller_id, controller in self.controllers.items():
            if controller.status != RunnableStatus.RUNNING:
                continue
            controller_pnl = self.get_performance_report(controller_id).global_pnl_quote
            last_max_pnl = self.max_pnl_by_controller[controller_id]
            if controller_pnl > last_max_pnl:
                self.max_pnl_by_controller[controller_id] = controller_pnl
            else:
                current_drawdown = last_max_pnl - controller_pnl
                if current_drawdown > self.config.max_controller_drawdown_quote:
                    self.logger().info(f"Controller {controller_id} reached max drawdown. Stopping the controller.")
                    controller.stop()
                    executors_order_placed = self.filter_executors(
                        executors=self.get_executors_by_controller(controller_id),
                        filter_func=lambda x: x.is_active and not x.is_trading,
                    )
                    self.executor_orchestrator.execute_actions(
                        actions=[StopExecutorAction(controller_id=controller_id, executor_id=executor.id) for executor in executors_order_placed]
                    )
                    self.drawdown_exited_controllers.append(controller_id)

    def check_max_global_drawdown(self):
        current_global_pnl = sum([self.get_performance_report(controller_id).global_pnl_quote for controller_id in self.controllers.keys()])
        if current_global_pnl > self.max_global_pnl:
            self.max_global_pnl = current_global_pnl
        else:
            current_global_drawdown = self.max_global_pnl - current_global_pnl
            if current_global_drawdown > self.config.max_global_drawdown_quote:
                self.drawdown_exited_controllers.extend(list(self.controllers.keys()))
                self.logger().info("Global drawdown reached. Stopping the strategy.")
                self._is_stop_triggered = True
                HummingbotApplication.main_application().stop()

    def get_controller_report(self, controller_id: str) -> dict:
        """
        Get the full report for a controller including performance and custom info.
        """
        performance_report = self.controller_reports.get(controller_id, {}).get("performance")
        return {
            "performance": performance_report.dict() if performance_report else {},
            "custom_info": self.controllers[controller_id].get_custom_info()
        }

    def send_performance_report(self):
        if self.current_timestamp - self._last_performance_report_timestamp >= self.performance_report_interval and self._pub:
            controller_reports = {controller_id: self.get_controller_report(controller_id) for controller_id in self.controllers.keys()}
            self._pub(controller_reports)
