"""Automatic OKX TR spot GRID bot manager with hard capital and loss limits.

Every bot comes from a V7 decision that passed all mandatory filters. The
manager enforces the owner's limits before anything reaches OKX:
max bots, capital per bot, total capital, one bot per pair, a cooldown after a
bot stops and a realized-loss fuse that halts automation. ``paper`` mode
records the same bots without calling OKX. A request whose outcome is unknown
is resolved by its client algo ID before any new bot is considered.
"""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading
import time

from .domain import TradingError, floor_step
from .tr_private import OutcomeUnknown

ACTIVE = ("PENDING", "UNKNOWN", "RUNNING", "STOPPING")


@dataclass(frozen=True)
class GridLimits:
    total_capital: Decimal = Decimal("600")
    bot_capital: Decimal = Decimal("300")
    max_bots: int = 2
    risk_per_bot: Decimal = Decimal("3")
    max_realized_loss: Decimal = Decimal("12")
    cooldown_hours: int = 6
    # OKX rejects grids whose per-grid profit is <= 0.1%; keep a margin above fees.
    min_step_fraction: Decimal = Decimal("0.003")

    def __post_init__(self):
        for name in ("total_capital", "bot_capital", "risk_per_bot", "max_realized_loss", "min_step_fraction"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value <= 0:
                raise TradingError("INVALID_GRID_LIMITS")
            object.__setattr__(self, name, value)
        if (type(self.max_bots) is not int or not 1 <= self.max_bots <= 10
                or self.bot_capital * self.max_bots > self.total_capital
                or type(self.cooldown_hours) is not int or not 0 <= self.cooldown_hours <= 168):
            raise TradingError("INVALID_GRID_LIMITS")


def okx_grid_params(decision, limits):
    """Map a V7 plan onto OKX spot grid parameters, keeping each grid step tradable."""
    plan = decision.get("plan")
    if not decision.get("eligible") or not plan:
        raise TradingError("DECISION_NOT_ELIGIBLE")
    lower, upper = Decimal(plan["lower"]), Decimal(plan["upper"])
    stop, take_profit = Decimal(plan["stop"]), Decimal(plan["take_profit"])
    tick, capital = Decimal(plan["tick_size"]), Decimal(plan["capital"])
    grids = int(plan["grids"])
    if capital != limits.bot_capital:
        raise TradingError("BOT_CAPITAL_MISMATCH")
    if Decimal(decision.get("planned_loss_usdt") or "Infinity") > limits.risk_per_bot:
        raise TradingError("BOT_RISK_LIMIT")
    while grids > 2 and (upper - lower) / grids / upper < limits.min_step_fraction:
        grids -= 1
    if (upper - lower) / grids / upper < limits.min_step_fraction:
        raise TradingError("OKX_GRID_STEP_TOO_SMALL")
    price = Decimal(decision["market_summary"]["price"])
    if not stop < lower < price < upper < take_profit:
        raise TradingError("ENTRY_OUTSIDE_GRID")
    return {"instrument": decision["instrument"], "lower": floor_step(lower, tick), "upper": upper,
            "grids": grids, "quote_size": capital, "stop": stop, "take_profit": take_profit,
            "entry_price": price}


class GridBotManager:
    def __init__(self, path, *, mode="paper", client=None, limits=None, clock_ms=None, price=None):
        if mode not in {"paper", "live"}:
            raise TradingError("INVALID_EXECUTION_MODE")
        if mode == "live" and client is None:
            raise TradingError("OKX_ACCESS_NOT_CONFIGURED")
        self.mode, self.client = mode, client
        self.limits = limits if limits is not None else GridLimits()
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        self.price = price
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS bots (id INTEGER PRIMARY KEY, client_id TEXT UNIQUE, algo_id TEXT,
                instrument TEXT, mode TEXT, state TEXT, capital TEXT, lower TEXT, upper TEXT, grids INTEGER,
                stop TEXT, take_profit TEXT, entry_price TEXT, planned_loss TEXT, decision_id TEXT,
                created_ms INTEGER, updated_ms INTEGER, pnl TEXT, close_reason TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at_ms INTEGER, kind TEXT, detail TEXT);
        """)

    # -- small persistence helpers -------------------------------------------------
    # The Telegram poller and the scheduler share one connection; the RLock
    # serializes every statement.
    def _meta(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_meta(self, key, value):
        with self._lock:
            self._db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(value)))

    def _event(self, kind, detail):
        with self._lock:
            self._db.execute("INSERT INTO events(at_ms, kind, detail) VALUES(?,?,?)",
                             (self.clock_ms(), kind, json.dumps(detail, default=str)))

    def _rows(self, where="1=1", args=()):
        with self._lock:
            cursor = self._db.execute(f"SELECT * FROM bots WHERE {where} ORDER BY id", args)
            names = [c[0] for c in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _update(self, bot_id, **values):
        values["updated_ms"] = self.clock_ms()
        sets = ", ".join(f"{k}=?" for k in values)
        with self._lock:
            self._db.execute(f"UPDATE bots SET {sets} WHERE id=?", (*values.values(), bot_id))

    def close(self):
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    # -- state ------------------------------------------------------------------------
    def auto_enabled(self):
        return bool(self._meta("auto", True))

    def set_auto(self, enabled):
        with self._lock:
            self._set_meta("auto", bool(enabled))
            self._event("AUTO", {"enabled": bool(enabled)})

    def halted(self):
        return self._meta("halt")

    def resume(self):
        with self._lock:
            self._set_meta("halt", None)
            self._event("RESUME", {})

    def active(self):
        """Bots of the current mode only; paper history never blocks live limits."""
        return self._rows("mode=? AND state IN (?,?,?,?)", (self.mode, *ACTIVE))

    def closed(self, limit=10):
        return self._rows("mode=? AND state IN ('CLOSED','REJECTED')", (self.mode,))[-limit:]

    def realized_pnl(self):
        return sum((Decimal(r["pnl"]) for r in self._rows("mode=? AND state='CLOSED' AND pnl IS NOT NULL",
                                                           (self.mode,))), Decimal(0))

    def summary(self):
        active = self.active()
        return {"mode": self.mode, "auto": self.auto_enabled(), "halt": self.halted(),
                "active": active, "reserved": sum((Decimal(r["capital"]) for r in active), Decimal(0)),
                "realized_pnl": self.realized_pnl(), "limits": self.limits}

    def blocking_reasons(self, instrument):
        now = self.clock_ms()
        reasons = []
        if self.halted():
            reasons.append("HALTED")
        active = self.active()
        if any(r["state"] == "UNKNOWN" for r in active):
            reasons.append("UNRESOLVED_OKX_REQUEST")
        if any(r["instrument"] == instrument for r in active):
            reasons.append("PAIR_ALREADY_ACTIVE")
        if len(active) >= self.limits.max_bots:
            reasons.append("MAX_BOTS_REACHED")
        reserved = sum((Decimal(r["capital"]) for r in active), Decimal(0))
        if reserved + self.limits.bot_capital > self.limits.total_capital:
            reasons.append("TOTAL_CAPITAL_LIMIT")
        if -self.realized_pnl() >= self.limits.max_realized_loss:
            reasons.append("REALIZED_LOSS_LIMIT")
        cooldown = self.limits.cooldown_hours * 3_600_000
        if any(r["instrument"] == instrument and now - r["updated_ms"] < cooldown
               for r in self._rows("mode=? AND state='CLOSED'", (self.mode,))):
            reasons.append("PAIR_COOLDOWN")
        return reasons

    # -- actions ----------------------------------------------------------------------
    def open_from_decision(self, decision):
        with self._lock:
            if self.mode == "live" and decision.get("data_status") != "VERIFIED_TR_PUBLIC":
                raise TradingError("DATA_UNVERIFIED")
            params = okx_grid_params(decision, self.limits)
            reasons = self.blocking_reasons(params["instrument"])
            if reasons:
                raise TradingError(reasons[0])
            if self.mode == "live":
                available = Decimal(str(self.client.usdt_available()))
                if available < params["quote_size"]:
                    raise TradingError("INSUFFICIENT_USDT_BALANCE")
            client_id = "tg" + hashlib.sha256(decision["decision_id"].encode()).hexdigest()[:30]
            if self._rows("client_id=?", (client_id,)):
                raise TradingError("DUPLICATE_DECISION")
            now = self.clock_ms()
            cursor = self._db.execute(
                "INSERT INTO bots(client_id, instrument, mode, state, capital, lower, upper, grids, stop, take_profit,"
                " entry_price, planned_loss, decision_id, created_ms, updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, params["instrument"], self.mode, "PENDING", str(params["quote_size"]), str(params["lower"]),
                 str(params["upper"]), params["grids"], str(params["stop"]), str(params["take_profit"]),
                 str(params["entry_price"]), decision.get("planned_loss_usdt"), decision["decision_id"], now, now))
            bot_id = cursor.lastrowid
            if self.mode == "paper":
                self._update(bot_id, state="RUNNING", algo_id=f"paper-{bot_id}")
            else:
                try:
                    algo_id = self.client.place_spot_grid(
                        instrument=params["instrument"], lower=params["lower"], upper=params["upper"],
                        grids=params["grids"], quote_size=params["quote_size"], stop=params["stop"],
                        take_profit=params["take_profit"], client_id=client_id)
                    self._update(bot_id, state="RUNNING", algo_id=str(algo_id))
                except OutcomeUnknown:
                    self._update(bot_id, state="UNKNOWN")
                    self._event("UNKNOWN", {"bot": bot_id})
                    raise
                except TradingError as error:
                    self._update(bot_id, state="REJECTED", close_reason=error.code)
                    self._event("REJECTED", {"bot": bot_id, "code": error.code})
                    raise
                except Exception:
                    # An unparseable reply may still mean OKX created the bot.
                    self._update(bot_id, state="UNKNOWN")
                    self._event("UNKNOWN", {"bot": bot_id})
                    raise OutcomeUnknown() from None
            self._event("OPEN", {"bot": bot_id, "instrument": params["instrument"], "mode": self.mode})
            return self._rows("id=?", (bot_id,))[0]

    def _close(self, row, pnl, reason, events):
        self._update(row["id"], state="CLOSED", pnl=str(pnl) if pnl is not None else None, close_reason=reason)
        events.append({"kind": "CLOSED", "instrument": row["instrument"], "pnl": pnl, "reason": reason,
                       "mode": row["mode"]})

    def reconcile(self):
        """Bring local state in line with OKX (live) or with the last price (paper)."""
        with self._lock:
            events = []
            rows = self.active()
            if not rows:
                return events
            if self.mode == "paper":
                for row in rows:
                    try:
                        price = Decimal(str(self.price(row["instrument"])))
                    except (TradingError, TypeError, ValueError):
                        price = None
                    if row["state"] == "STOPPING":
                        self._close(row, self._paper_pnl(row, price), row["close_reason"] or "MANUAL_STOP", events)
                    elif price is None:
                        continue
                    elif price <= Decimal(row["stop"]):
                        self._close(row, -Decimal(row["planned_loss"] or row["capital"]), "STOP_LOSS", events)
                    elif price >= Decimal(row["take_profit"]):
                        self._close(row, self._paper_pnl(row, price), "TAKE_PROFIT", events)
            else:
                pending = self.client.pending_grids()
                by_algo = {p.get("algoId"): p for p in pending}
                by_client = {p.get("algoClOrdId"): p for p in pending if p.get("algoClOrdId")}
                history = None
                for row in rows:
                    live = by_algo.get(row["algo_id"]) or by_client.get(row["client_id"])
                    if live is not None:
                        values = {"pnl": str(live.get("totalPnl") or "0")}
                        if row["state"] in ("UNKNOWN", "PENDING"):
                            values.update(state="RUNNING", algo_id=str(live.get("algoId")))
                            events.append({"kind": "RESOLVED", "instrument": row["instrument"]})
                            if self.halted() == "EMERGENCY_STOP":
                                # Created after the emergency stop was requested: stop it too.
                                self.client.stop_spot_grid(values["algo_id"], row["instrument"], sell_base=True)
                                values.update(state="STOPPING", close_reason="EMERGENCY_STOP")
                        self._update(row["id"], **values)
                        continue
                    if history is None:
                        history = self.client.grid_history()
                    done = next((h for h in history if h.get("algoId") == row["algo_id"]
                                 or h.get("algoClOrdId") == row["client_id"]), None)
                    if done is not None:
                        reason = {"1": "MANUAL_STOP", "2": "TAKE_PROFIT", "3": "STOP_LOSS",
                                  "4": "RISK_CONTROL"}.get(
                            str(done.get("cancelType")), "STOPPED")
                        self._close(row, Decimal(str(done.get("totalPnl") or "0")), reason, events)
                    elif row["state"] in ("UNKNOWN", "PENDING") and self.clock_ms() - row["updated_ms"] > 120_000:
                        self._update(row["id"], state="REJECTED", close_reason="NOT_FOUND_AFTER_UNKNOWN")
                        events.append({"kind": "NOT_CREATED", "instrument": row["instrument"]})
            if -self.realized_pnl() >= self.limits.max_realized_loss and not self.halted():
                self._set_meta("halt", "REALIZED_LOSS_LIMIT")
                self._set_meta("auto", False)
                events.append({"kind": "HALT", "reason": "REALIZED_LOSS_LIMIT"})
            return events

    @staticmethod
    def _paper_pnl(row, price):
        """Rough paper estimate: the grid ends fully in quote at the exit price."""
        if price is None:
            return Decimal(0)
        entry = Decimal(row["entry_price"])
        return (Decimal(row["capital"]) * (price / entry - 1)).quantize(Decimal("0.01"))

    def stop(self, instrument, reason="MANUAL_STOP"):
        with self._lock:
            rows = [r for r in self.active() if r["instrument"] == instrument and r["state"] == "RUNNING"]
            if not rows:
                raise TradingError("NO_RUNNING_BOT")
            for row in rows:
                if row["mode"] == "live":
                    self.client.stop_spot_grid(row["algo_id"], row["instrument"], sell_base=True)
                self._update(row["id"], state="STOPPING", close_reason=reason)
                self._event("STOP", {"bot": row["id"], "reason": reason})
            return len(rows)

    def emergency_stop(self):
        """Disable automation, halt, and ask OKX to stop every bot this service created."""
        with self._lock:
            self._set_meta("auto", False)
            self._set_meta("halt", "EMERGENCY_STOP")
            stopped, failed = 0, []
            for row in self.active():
                if row["state"] != "RUNNING":
                    continue
                try:
                    if row["mode"] == "live":
                        self.client.stop_spot_grid(row["algo_id"], row["instrument"], sell_base=True)
                    self._update(row["id"], state="STOPPING", close_reason="EMERGENCY_STOP")
                    stopped += 1
                except TradingError as error:
                    failed.append((row["instrument"], error.code))
            self._event("EMERGENCY_STOP", {"stopped": stopped, "failed": failed})
            return stopped, failed
