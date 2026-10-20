"""Transactional paper ledger, limit-grid fills and deterministic tick replay.

All amounts are quote-currency decimals. Limit orders fill at their limit on a
crossing; forced exits use the observed tick with adverse slippage. This is not
an exchange fill/queue simulator. No network client exists in this module.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import json
import os
import sqlite3
import stat

from .domain import (TradingError, RiskPolicy, build_plan, canonical, identifier,
                     paper_only, positive, timestamp)


def _amount(value):
    try:
        if not isinstance(value, str) or len(value) > 256:
            raise TradingError("CORRUPT_STATE")
        result = Decimal(value)
        if not result.is_finite() or abs(result) > Decimal("1e54") or result.as_tuple().exponent < -108:
            raise TradingError("CORRUPT_STATE")
        return result
    except InvalidOperation:
        raise TradingError("CORRUPT_STATE") from None


class PaperTrader:
    def __init__(self, path, policy=None, *, profile=None):
        paper_only()
        from .profiles import PaperProfile
        if profile is not None and (not isinstance(profile, PaperProfile) or policy is not None):
            raise TradingError("AMBIGUOUS_PAPER_CONFIGURATION")
        self.policy = profile.policy if profile else policy or RiskPolicy()
        self.config_identity = profile.identity if profile else None
        self._savepoint_counter = 0
        self._db = None
        try:
            if str(path) != ":memory:":
                path = Path(path).absolute()
                if path.suffix != ".sqlite3":
                    raise TradingError("INVALID_STATE_PATH")
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise TradingError("SYMLINK_STATE_PATH")
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not path.exists():
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                if not stat.S_ISREG(path.stat().st_mode):
                    raise TradingError("INVALID_STATE_PATH")
                os.chmod(path, 0o600)
            self._db = sqlite3.connect(str(path), timeout=3, isolation_level=None)
            existing = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and existing != {"meta", "bots", "events"}:
                raise TradingError("UNSUPPORTED_STATE")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bots (id TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, bot_id TEXT REFERENCES bots(id), kind TEXT NOT NULL,
                    at INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_bot ON events(bot_id, kind);
                CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'immutable paper events'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'immutable paper events'); END;
            """)
            with self._transaction():
                saved = self._meta("policy")
                if saved is None:
                    if any(self._db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                           for table in ("bots", "events", "meta")):
                        raise TradingError("CORRUPT_STATE")
                    self._set_meta("policy", self.policy.to_dict())
                    self._set_meta("schema", 1)
                    self._set_meta("halted", False)
                    self._set_meta("profile", self.config_identity)
                elif saved != self.policy.to_dict():
                    raise TradingError("POLICY_MISMATCH")
                if self._meta("schema") != 1 or type(self._meta("halted")) is not bool:
                    raise TradingError("UNSUPPORTED_STATE")
                self._audit_all()
        except (OSError, sqlite3.Error):
            self.close()
            raise TradingError("STORAGE_ERROR") from None
        except Exception:
            self.close()
            raise

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self):
        with localcontext() as ctx:
            ctx.prec = 120
            nested = self._db.in_transaction
            self._savepoint_counter += 1
            savepoint = "paper_nested_" + str(self._savepoint_counter)
            try:
                self._db.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
                yield
                self._db.execute("RELEASE " + savepoint if nested else "COMMIT")
            except Exception as error:
                if self._db.in_transaction:
                    if nested:
                        self._db.execute("ROLLBACK TO " + savepoint)
                        self._db.execute("RELEASE " + savepoint)
                    else:
                        self._db.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise TradingError("STORAGE_ERROR") from None
                if isinstance(error, (KeyError, TypeError, IndexError, AttributeError, json.JSONDecodeError, InvalidOperation)):
                    raise TradingError("CORRUPT_STATE") from None
                raise

    def _meta(self, key):
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_meta(self, key, value):
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, canonical(value)))

    def _states(self):
        return [(key, json.loads(value)) for key, value in self._db.execute("SELECT id,state FROM bots ORDER BY id")]

    def _state(self, bot_id):
        row = self._db.execute("SELECT state FROM bots WHERE id=?", (identifier(bot_id),)).fetchone()
        if row is None:
            raise TradingError("BOT_NOT_FOUND")
        return json.loads(row[0])

    def _save(self, bot_id, state):
        self._db.execute("UPDATE bots SET state=? WHERE id=?", (canonical(state), bot_id))

    def _event(self, event_id, bot_id, kind, at, payload):
        self._db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (event_id, bot_id, kind, at, canonical(payload)))

    def _audit(self, bot_id, state):
        plan = build_plan(state["plan"])
        opening = self._db.execute("SELECT payload FROM events WHERE id=? AND kind='open'", ("open:" + bot_id,)).fetchone()
        if not opening or json.loads(opening[0])["plan_digest"] != plan.digest:
            raise TradingError("ACCOUNTING_MISMATCH")
        timestamp(state["last_at"])
        positive(state["last_price"])
        latest = self._db.execute("SELECT payload FROM events WHERE bot_id=? AND kind='tick' ORDER BY rowid DESC LIMIT 1", (bot_id,)).fetchone()
        receipt = json.loads(latest[0]) if latest else json.loads(opening[0])
        if state["last_at"] != receipt["observed_at"] or _amount(state["last_price"]) != _amount(receipt["price"]):
            raise TradingError("ACCOUNTING_MISMATCH")
        closure = self._db.execute("SELECT payload FROM events WHERE bot_id=? AND kind='close'", (bot_id,)).fetchone()
        if ((state["status"] == "CLOSED") != bool(closure)
                or state["reason"] != (json.loads(closure[0])["reason"] if closure else None)):
            raise TradingError("ACCOUNTING_MISMATCH")
        cash, realized, fees = plan.capital, Decimal(0), Decimal(0)
        quantities, costs = [Decimal(0)] * plan.grids, [Decimal(0)] * plan.grids
        for (raw,) in self._db.execute("SELECT payload FROM events WHERE bot_id=? AND kind='fill' ORDER BY rowid", (bot_id,)):
            fill = json.loads(raw)
            slot = fill["slot"]
            if type(slot) is not int or not 0 <= slot < plan.grids:
                raise TradingError("ACCOUNTING_MISMATCH")
            quantity, fee = _amount(fill["quantity"]), _amount(fill["fee"])
            price = _amount(fill["price"])
            if quantity <= 0 or price <= 0 or fee < 0 or fee != quantity * price * plan.fee_rate:
                raise TradingError("ACCOUNTING_MISMATCH")
            fees += fee
            if fill["side"] == "buy":
                if quantities[slot] != 0:
                    raise TradingError("ACCOUNTING_MISMATCH")
                cost = quantity * price + fee
                quantities[slot], costs[slot] = quantity, cost
                cash -= cost
            elif fill["side"] == "sell":
                if quantity != quantities[slot]:
                    raise TradingError("ACCOUNTING_MISMATCH")
                net = quantity * price - fee
                pnl = net - costs[slot]
                if pnl != _amount(fill["realized"]):
                    raise TradingError("ACCOUNTING_MISMATCH")
                realized += pnl
                cash += net
                quantities[slot], costs[slot] = Decimal(0), Decimal(0)
            else:
                raise TradingError("ACCOUNTING_MISMATCH")
            if cash < 0:
                raise TradingError("NEGATIVE_CASH")
        actual_quantities = [_amount(p["quantity"]) for p in state["positions"]]
        actual_costs = [_amount(p["cost"]) for p in state["positions"]]
        if (quantities != actual_quantities or costs != actual_costs
                or cash != _amount(state["cash"]) or realized != _amount(state["realized"])
                or fees != _amount(state["fees"])
                or cash + sum(costs) != plan.capital + realized
                or state["status"] not in {"OPEN", "CLOSED"}
                or (state["status"] == "CLOSED" and any(quantities))):
            raise TradingError("ACCOUNTING_MISMATCH")

    def _audit_all(self):
        if self._meta("policy") != self.policy.to_dict():
            raise TradingError("POLICY_MISMATCH")
        if self._meta("profile") != self.config_identity:
            raise TradingError("PROFILE_MISMATCH")
        for bot_id, state in self._states():
            self._audit(bot_id, state)
        self._portfolio()

    def _portfolio(self):
        states = [s for _, s in self._states()]
        active = [s for s in states if s["status"] == "OPEN"]
        reserved = sum((build_plan(s["plan"]).capital for s in active), Decimal(0))
        closed_pnl = sum((_amount(s["realized"]) for s in states if s["status"] == "CLOSED"), Decimal(0))
        unallocated = self.policy.equity + closed_pnl - reserved
        cash = unallocated + sum((_amount(s["cash"]) for s in active), Decimal(0))
        inventory_cost = sum((_amount(p["cost"]) for s in active for p in s["positions"]), Decimal(0))
        net_inventory = Decimal(0)
        for state in active:
            plan = build_plan(state["plan"])
            quantity = sum((_amount(p["quantity"]) for p in state["positions"]), Decimal(0))
            net_inventory += quantity * _amount(state["last_price"]) * (1 - plan.slippage_rate) * (1 - plan.fee_rate)
        realized = sum((_amount(s["realized"]) for s in states), Decimal(0))
        if unallocated < 0 or cash < 0 or cash + inventory_cost != self.policy.equity + realized:
            raise TradingError("PORTFOLIO_ACCOUNTING_MISMATCH")
        return {"initial_equity": str(self.policy.equity), "unallocated_cash": str(unallocated),
                "reserved_capital": str(reserved), "cash": str(cash), "inventory_cost": str(inventory_cost),
                "net_inventory_value": str(net_inventory), "equity": str(cash + net_inventory),
                "realized_pnl": str(realized), "unrealized_pnl": str(net_inventory - inventory_cost),
                "fees": str(sum((_amount(s["fees"]) for s in states), Decimal(0))),
                "reserved_planned_risk": str(sum((build_plan(s["plan"]).planned_loss for s in active), Decimal(0))),
                "max_bot_planned_risk": str(self.policy.equity * self.policy.risk_fraction),
                "max_total_planned_risk": str(self.policy.equity * self.policy.total_risk_fraction),
                "active_bots": len(active)}

    def open(self, bot_id, plan, market, now):
        paper_only()
        identifier(bot_id)
        with self._transaction():
            self._audit_all()
            if self._meta("halted"):
                raise TradingError("PAPER_HALTED")
            if self._meta("paused"):
                raise TradingError("PAPER_PAUSED")
            plan.check(self.policy, market, now)
            states = self._states()
            if any(key == bot_id for key, _ in states):
                raise TradingError("DUPLICATE_BOT")
            active = [s for _, s in states if s["status"] == "OPEN"]
            reserved = sum((build_plan(s["plan"]).capital for s in active), Decimal(0))
            booked_risk = sum((build_plan(s["plan"]).planned_loss for s in active), Decimal(0))
            # Profits never increase the configured risk/equity budget automatically.
            net_realized = sum((_amount(s["realized"]) for _, s in states), Decimal(0))
            available_equity = self.policy.equity + min(net_realized, Decimal(0))
            if len(active) >= self.policy.max_bots:
                raise TradingError("MAX_BOTS")
            if plan.capital > _amount(self._portfolio()["unallocated_cash"]):
                raise TradingError("ALLOCATION_LIMIT")
            if reserved + plan.capital > available_equity * self.policy.max_allocation_fraction:
                raise TradingError("ALLOCATION_LIMIT")
            if booked_risk + plan.planned_loss > available_equity * self.policy.total_risk_fraction:
                raise TradingError("TOTAL_RISK_LIMIT")
            if plan.planned_loss > available_equity * self.policy.risk_fraction:
                raise TradingError("BOT_RISK_LIMIT")
            state = {"plan": plan.to_dict(), "cash": str(plan.capital), "realized": "0", "fees": "0",
                     "status": "OPEN", "reason": None, "last_price": str(market.price),
                     "last_at": market.observed_at,
                     "positions": [{"quantity": "0", "cost": "0"} for _ in range(plan.grids)]}
            self._db.execute("INSERT INTO bots VALUES (?,?)", (bot_id, canonical(state)))
            self._event("open:" + bot_id, bot_id, "open", now,
                        {"plan_digest": plan.digest, "price": str(market.price), "observed_at": market.observed_at})
            self._audit_all()
        return self.status(bot_id)

    def _fill(self, bot_id, state, plan, slot, side, price, event_id, at):
        position = state["positions"][slot]
        quantity = plan.quantity(price) if side == "buy" else _amount(position["quantity"])
        gross, fee = quantity * price, quantity * price * plan.fee_rate
        cash, realized = _amount(state["cash"]), _amount(state["realized"])
        fill = {"slot": slot, "side": side, "price": str(price), "quantity": str(quantity), "fee": str(fee)}
        if side == "buy":
            cost = gross + fee
            if quantity <= 0 or cash < cost:
                raise TradingError("INSUFFICIENT_PAPER_CASH")
            cash -= cost
            position.update(quantity=str(quantity), cost=str(cost))
        else:
            pnl = gross - fee - _amount(position["cost"])
            cash += gross - fee
            realized += pnl
            fill["realized"] = str(pnl)
            position.update(quantity="0", cost="0")
        state.update(cash=str(cash), realized=str(realized), fees=str(_amount(state["fees"]) + fee))
        self._event(event_id, bot_id, "fill", at, fill)

    def tick(self, bot_id, tick_id, price, observed_at, now, force_close=False):
        paper_only()
        identifier(bot_id)
        identifier(tick_id)
        price, observed_at, now = positive(price), timestamp(observed_at), timestamp(now)
        if type(force_close) is not bool:
            raise TradingError("INVALID_CLOSE_FLAG")
        payload = {"price": str(price.normalize()), "observed_at": observed_at, "force_close": force_close}
        event_id = "tick:" + bot_id + ":" + tick_id
        duplicate = False
        with self._transaction():
            self._audit_all()
            state = self._state(bot_id)
            previous = self._db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
            if previous:
                if json.loads(previous[0]) != payload:
                    raise TradingError("TICK_ID_CONFLICT")
                duplicate = True
            else:
                if now - observed_at > self.policy.max_market_age:
                    raise TradingError("STALE_TICK")
                if observed_at - now > self.policy.max_future_seconds:
                    raise TradingError("FUTURE_TICK")
                if state["status"] != "OPEN":
                    raise TradingError("BOT_CLOSED")
                if observed_at <= state["last_at"]:
                    raise TradingError("OUT_OF_ORDER_TICK")
                plan, last = build_plan(state["plan"]), _amount(state["last_price"])
                reason = ("HALTED" if self._meta("halted") else "MANUAL" if force_close else
                          "STOP_LOSS" if price <= plan.stop else "TAKE_PROFIT" if price >= plan.take_profit else None)
                for slot, (buy, sell) in enumerate(zip(plan.levels, plan.levels[1:])):
                    held = _amount(state["positions"][slot]["quantity"]) > 0
                    # Resting sells fill at their limits before a higher gap triggers TP.
                    if held and last <= sell <= price and price > last:
                        self._fill(bot_id, state, plan, slot, "sell", sell, event_id + ":" + str(slot), observed_at)
                    elif held and reason:
                        self._fill(bot_id, state, plan, slot, "sell", price * (1 - plan.slippage_rate),
                                   event_id + ":" + str(slot), observed_at)
                    elif not reason and not self._meta("paused") and not held and last >= buy >= price and price < last:
                        self._fill(bot_id, state, plan, slot, "buy", buy, event_id + ":" + str(slot), observed_at)
                if reason:
                    state.update(status="CLOSED", reason=reason)
                    self._event("close:" + bot_id, bot_id, "close", observed_at, {"reason": reason})
                state.update(last_price=str(price), last_at=observed_at)
                self._save(bot_id, state)
                self._event(event_id, bot_id, "tick", observed_at, payload)
                self._audit_all()
                realized = sum((_amount(s["realized"]) for _, s in self._states()), Decimal(0))
                if realized <= -self.policy.equity * self.policy.max_realized_loss_fraction:
                    self._set_meta("halted", True)
        result = self.status(bot_id)
        result["duplicate"] = duplicate
        return result

    def halt(self):
        """Persist a halt. Positions flatten on their next valid price, never at an invented quote."""
        with self._transaction():
            self._audit_all()
            self._set_meta("halted", True)

    def pause(self):
        """Persistently block new entries/buys; existing sell/TP/SL handling continues."""
        paper_only()
        with self._transaction():
            self._audit_all()
            self._set_meta("paused", True)

    def status(self, bot_id=None):
        with self._transaction():
            self._audit_all()
            states = [(bot_id, self._state(bot_id))] if bot_id else self._states()
            result = []
            for key, state in states:
                plan = build_plan(state["plan"])
                price = _amount(state["last_price"])
                quantity = sum((_amount(p["quantity"]) for p in state["positions"]), Decimal(0))
                net_inventory = quantity * price * (1 - plan.slippage_rate) * (1 - plan.fee_rate)
                cost = sum((_amount(p["cost"]) for p in state["positions"]), Decimal(0))
                equity = _amount(state["cash"]) + net_inventory
                result.append({"bot_id": key, "instrument": plan.instrument, "status": state["status"],
                               "reason": state["reason"], "cash": state["cash"], "quantity": str(quantity),
                               "fees": state["fees"], "realized_pnl": state["realized"],
                               "unrealized_pnl": str(net_inventory - cost), "equity": str(equity),
                               "total_pnl": str(equity - plan.capital), "last_at": state["last_at"],
                               "planned_stop_loss": str(plan.planned_loss), "plan_digest": plan.digest})
            return {"mode": "paper", "halted": self._meta("halted"), "paused": bool(self._meta("paused")), "bots": result,
                    "profile": self.config_identity, "portfolio": self._portfolio()}
