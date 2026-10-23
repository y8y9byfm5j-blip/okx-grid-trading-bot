"""V7 finite-book paper fills on a separate, explicitly versioned ledger.

Displayed depth is a simulation capacity assumption, never proof of exchange
queue fills. Unfilled and partially filled exits retain capital/risk reserves.
"""
from decimal import Decimal
import hashlib
import json

from .control import receipt, record, snapshot
from .domain import Market, TradingError, build_plan, floor_step, paper_only, positive, timestamp
from .paper import PaperTrader, _amount
from .profiles import load_profile


class V7PaperTrader(PaperTrader):
    def __init__(self, path, *, synthetic=False):
        if type(synthetic) is not bool:
            raise TradingError("INVALID_SIMULATION_MODE")
        self.synthetic, self._opening, self._source = synthetic, None, None
        super().__init__(path, profile=load_profile("v7-paper-600"))
        try:
            with self._transaction():
                mode = "SYNTHETIC_TEST" if synthetic else "VERIFIED_PUBLIC_PAPER"
                saved = self._meta("v7_execution_mode")
                if saved is not None and saved != mode:
                    raise TradingError("PAPER_DATA_MODE_MISMATCH")
                self._set_meta("v7_execution_mode", mode)
        except Exception:
            self.close()
            raise

    def _orders(self, bot):
        return self._meta("v7_orders_" + bot)

    def bind_source(self, source):
        from .v7_market import V7MarketData
        if type(source) is not V7MarketData or source.policy != self.policy:
            raise TradingError("VERIFIED_SOURCE_REQUIRED")
        self._source = source

    def _require_capture(self, report):
        if self.synthetic:
            return
        if (self._source is None or not self._source.real_transport or not isinstance(report, dict)
                or report.get("data_status") != "VERIFIED_TR_PUBLIC" or not self._source.authentic(report)
                or not -2000 <= self._source._now() - report["as_of_ms"] <= self.policy.max_market_age * 1000):
            raise TradingError("DATA_UNVERIFIED")

    def _frame(self, bot, orders, at):
        self._set_meta("v7_orders_" + bot, orders)
        serial = self._db.execute("SELECT count(*) FROM events WHERE bot_id=? AND kind='v7_order_frame'", (bot,)).fetchone()[0]
        self._event("v7_frame:" + bot + ":" + str(serial), bot, "v7_order_frame", at, orders)

    def _audit_all(self):
        super()._audit_all()
        mode = self._meta("v7_execution_mode")
        expected = "SYNTHETIC_TEST" if self.synthetic else "VERIFIED_PUBLIC_PAPER"
        if mode is not None and mode != expected:
            raise TradingError("PAPER_DATA_MODE_MISMATCH")
        for bot, state in self._states():
            orders = self._orders(bot)
            row = self._db.execute("SELECT payload FROM events WHERE bot_id=? AND kind='v7_order_frame' ORDER BY rowid DESC LIMIT 1", (bot,)).fetchone()
            if self._opening == bot and row is None:
                continue
            if row is None or json.loads(row[0]) != orders or len(orders["slots"]) != len(state["positions"]):
                raise TradingError("ORDER_RECONCILIATION_MISMATCH")
            plan = build_plan(state["plan"])
            for slot, (order, position) in enumerate(zip(orders["slots"], state["positions"])):
                held = _amount(position["quantity"])
                remaining = _amount(order["remaining"])
                if (remaining < 0 or order["side"] not in {"BUY", "SELL", "CANCELLED"}
                        or order["side"] == "SELL" and remaining != held
                        or order["side"] == "BUY" and held + remaining != plan.quantity(plan.levels[slot])
                        or order["side"] == "CANCELLED" and (held != 0 or remaining != 0)):
                    raise TradingError("ORDER_RECONCILIATION_MISMATCH")

    def open(self, bot_id, plan, market, now, *, report=None):
        paper_only()
        # The read-only setup pins the same immutable stage as V7Core. Direct
        # trader calls must not bypass its command/scheduler execution gate.
        stage = self._meta("v7_execution_stage")
        if stage == snapshot({"revision": 1, "observation_only": True})[1]:
            raise TradingError("OBSERVATION_ONLY_EXECUTION_DISABLED")
        self._require_capture(report)
        if plan.capital != Decimal("300"):
            raise TradingError("PROFILE_REQUIRES_300_USDT_PER_BOT")
        if report is not None and Market.from_dict(report["market"]) != market:
            raise TradingError("SNAPSHOT_MARKET_MISMATCH")
        with self._transaction():
            self._audit_all()
            if any(s["status"] == "OPEN" and s["plan"]["instrument"] == plan.instrument for _, s in self._states()):
                raise TradingError("DUPLICATE_INSTRUMENT")
            self._set_meta("v7_orders_" + bot_id, {"exit_reason": None, "slots": [
                {"side": "BUY", "remaining": str(plan.quantity(p))} for p in plan.levels[:-1]]})
            self._opening = bot_id
            try:
                super().open(bot_id, plan, market, now)
                self._frame(bot_id, self._orders(bot_id), now)
            finally:
                self._opening = None
            self._audit_all()
        return self.status(bot_id)

    def _cancel_buys(self):
        for bot, state in self._states():
            if state["status"] != "OPEN":
                continue
            orders = self._orders(bot)
            for order, position in zip(orders["slots"], state["positions"]):
                if order["side"] == "BUY":
                    held = _amount(position["quantity"])
                    order.update(side="SELL" if held else "CANCELLED", remaining=str(held))
            self._frame(bot, orders, state["last_at"])

    def pause(self):
        with self._transaction():
            super().pause()
            self._cancel_buys()
            self._audit_all()

    def halt(self):
        with self._transaction():
            super().halt()
            self._cancel_buys()
            self._audit_all()

    def resume_entries(self):
        paper_only()
        with self._transaction():
            self._audit_all()
            if self._meta("halted"):
                raise TradingError("PAPER_HALTED")
            self._set_meta("paused", False)
            for bot, state in self._states():
                orders = self._orders(bot)
                if state["status"] == "OPEN" and not orders["exit_reason"]:
                    plan = build_plan(state["plan"])
                    for slot, order in enumerate(orders["slots"]):
                        if order["side"] == "CANCELLED":
                            order.update(side="BUY", remaining=str(plan.quantity(plan.levels[slot])))
                    self._frame(bot, orders, state["last_at"])
            self._audit_all()

    def tick(self, *args, **kwargs):
        raise TradingError("V7_BOOK_SNAPSHOT_REQUIRED")

    def process_book(self, bot_id, report, *, now):
        paper_only()
        timestamp(now)
        report, digest = snapshot(report)
        self._require_capture(report)
        market = Market.from_dict(report["market"])
        if (now - market.observed_at > self.policy.max_market_age
                or market.observed_at - now > self.policy.max_future_seconds):
            raise TradingError("STALE_OR_FUTURE_BOOK")
        event_id = "v7_book:" + bot_id + ":" + report["snapshot_id"]
        with self._transaction():
            self._audit_all()
            previous = receipt(self, event_id, "v7_book")
            if previous:
                if previous["digest"] != digest:
                    raise TradingError("BOOK_REPLAY_CONFLICT")
                return {**previous["result"], "duplicate": True}
            state = self._state(bot_id)
            plan = build_plan(state["plan"])
            if (positive(report["rules"]["tick_size"]) != plan.tick_size
                    or positive(report["rules"]["lot_size"]) != plan.lot_size):
                raise TradingError("INSTRUMENT_RULE_CHANGED")
            if state["status"] != "OPEN" or plan.instrument != market.instrument:
                raise TradingError("INVALID_BOT_BOOK")
            if market.observed_at <= state["last_at"]:
                raise TradingError("OUT_OF_ORDER_BOOK")
            orders = self._orders(bot_id)
            previous_sequence = orders.get("last_book_sequence")
            if previous_sequence is not None and report["sequence"] <= previous_sequence:
                raise TradingError("REUSED_OR_OLD_BOOK_SEQUENCE")
            orders["last_book_sequence"] = report["sequence"]
            reason = ("HALTED" if self._meta("halted") else orders["exit_reason"] or
                      ("STOP_LOSS" if market.bid <= plan.stop else "TAKE_PROFIT" if market.bid >= plan.take_profit else None))
            orders["exit_reason"] = reason
            # Consume each displayed level at most once per frame. Conservative
            # 10% participation; it is a labelled model, not observed executions.
            depth = {"BUY": [[p, floor_step(q * Decimal("0.1"), plan.lot_size)] for p, q in market.asks],
                     "SELL": [[p, floor_step(q * Decimal("0.1"), plan.lot_size)] for p, q in market.bids]}
            fills = 0
            for slot, order in enumerate(orders["slots"]):
                held = _amount(state["positions"][slot]["quantity"])
                buy, sell = plan.levels[slot:slot + 2]
                if reason or self._meta("paused") or order["side"] == "BUY" and held and market.bid >= sell:
                    order.update(side="SELL" if held else "CANCELLED", remaining=str(held))
                if order["side"] == "CANCELLED":
                    continue
                side = order["side"]
                remaining = _amount(order["remaining"])
                limit = buy if side == "BUY" else sell
                order["blocked_reason"] = None
                if side == "SELL" and (remaining < positive(report["rules"]["min_size"])
                        or remaining * (market.bid if reason else limit) < plan.min_notional):
                    order["blocked_reason"] = "EXIT_BELOW_MINIMUM_ORDER"
                    continue
                for level in depth[side]:
                    price, available = level
                    if not reason and (side == "BUY" and price > limit or side == "SELL" and price < limit):
                        break
                    quantity = min(remaining, available)
                    if quantity <= 0:
                        continue
                    fill_price = price * (1 - plan.slippage_rate) if reason else limit
                    self._fill(bot_id, state, plan, slot, side.lower(), fill_price,
                               event_id + ":fill:" + str(fills), market.observed_at, quantity=quantity)
                    fills += 1
                    remaining -= quantity
                    level[1] -= quantity
                    if not remaining:
                        break
                order["remaining"] = str(remaining)
                if not remaining:
                    if side == "BUY":
                        order.update(side="SELL", remaining=state["positions"][slot]["quantity"])
                    elif reason or self._meta("paused"):
                        order.update(side="CANCELLED", remaining="0")
                    else:
                        order.update(side="BUY", remaining=str(plan.quantity(buy)))
            if reason and not any(_amount(p["quantity"]) for p in state["positions"]):
                state.update(status="CLOSED", reason=reason)
                for order in orders["slots"]:
                    order.update(side="CANCELLED", remaining="0")
                self._event("close:" + bot_id, bot_id, "close", market.observed_at, {"reason": reason})
            state.update(last_price=str(market.price), last_at=market.observed_at)
            self._save(bot_id, state)
            self._event(event_id + ":tick", bot_id, "tick", market.observed_at,
                        {"price": str(market.price), "observed_at": market.observed_at, "snapshot_id": report["snapshot_id"]})
            self._frame(bot_id, orders, market.observed_at)
            self._audit_all()
            realized = sum((_amount(s["realized"]) for _, s in self._states()), Decimal(0))
            if realized <= -self.policy.equity * self.policy.max_realized_loss_fraction:
                self.halt()
            result = {"mode": "paper", "duplicate": False, "fills": fills,
                      "snapshot_id": report["snapshot_id"], "state": self.status(bot_id)}
            record(self, event_id, "v7_book", now, {"digest": digest, "result": result})
            return result

    def status(self, bot_id=None):
        result = super().status(bot_id)
        result.update(live_enabled=False, execution_model="FINITE_REST_BOOK_10PCT_PARTICIPATION",
                      data_mode="SYNTHETIC_TEST" if self.synthetic else "VERIFIED_PUBLIC_PAPER",
                      stop_loss_is_guaranteed=False)
        for bot in result["bots"]:
            bot["orders"] = self._orders(bot["bot_id"])
        return result
