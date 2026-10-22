"""Single-owner V5 integration seam. No polling, scheduling or Telegram sending.

The host calls the command hook and this cycle from its existing scheduler only
after original V5 compatibility and staging acceptance have been verified.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import hashlib

from .control import notices, pin, queue_notice, receipt, record, snapshot
from .domain import Market, TradingError, build_plan, identifier, paper_only, symbol, timestamp
from .v7_decision import PlanningPolicy, decide
from .v7_paper import V7PaperTrader


class V7Core:
    def __init__(self, trader, market, *, planning=None, flow=None):
        paper_only()
        if not isinstance(trader, V7PaperTrader) or market.policy != trader.policy:
            raise TradingError("V7_PROFILE_POLICY_REQUIRED")
        self.trader, self.market = trader, market
        trader.bind_source(market)
        self.planning = planning if planning is not None else PlanningPolicy()
        from .flow_contracts import FlowPolicy
        from .money_flow import MoneyFlowScanner
        self.flow = flow if flow is not None else MoneyFlowScanner(client=market.client,
            policy=FlowPolicy(paper_notional="300"), clock_ms=market.clock_ms, fee_rate=self.planning.fee_rate)
        if self.flow.policy.paper_notional != 300 or self.flow.fee_rate != Decimal(self.planning.fee_rate):
            raise TradingError("FLOW_PLANNING_MODEL_MISMATCH")
        self._decisions = {}
        with trader._transaction():
            pin(trader, "v7_planning", asdict(self.planning))

    def _data_gate(self, report):
        if not self.market.authentic(report):
            raise TradingError("UNRECOGNIZED_SNAPSHOT")
        if (not self.trader.synthetic and
                (not self.market.real_transport or report["data_status"] != "VERIFIED_TR_PUBLIC")):
            raise TradingError("DATA_UNVERIFIED")
        now = self.market._now()
        if not -2000 <= now - report["as_of_ms"] <= self.trader.policy.max_market_age * 1000:
            raise TradingError("STALE_PUBLIC_DATA")

    def decision(self, instrument, capital="300", *, refresh=False):
        report = self.market.capture(instrument, refresh=refresh)
        if not self.market.authentic(report):
            raise TradingError("UNRECOGNIZED_SNAPSHOT")
        value = decide(report, load_v7(), self.planning, capital=capital, now_ms=self.market._now())
        # A snapshot/configuration pair always has exactly the same analysis.
        previous = self._decisions.get(value["decision_id"])
        if previous is not None and previous != value:
            raise TradingError("INCONSISTENT_DECISION")
        self._decisions[value["decision_id"]] = deepcopy(value)
        self._decisions = dict(list(self._decisions.items())[-100:])
        with self.trader._transaction():
            key = "v7_plan:" + value["decision_id"]
            old = receipt(self.trader, key, "v7_plan")
            if old is not None and old["decision"] != value:
                raise TradingError("INCONSISTENT_DECISION")
            if old is None:
                record(self.trader, key, "v7_plan", report["as_of_ms"] // 1000, {"decision": value})
            pending = self.trader._meta("v7_pending_plans") or {}
            pending[instrument] = {"capital": str(capital), "decision_id": value["decision_id"]}
            self.trader._set_meta("v7_pending_plans", dict(list(pending.items())[-100:]))
        return value

    def radar(self, *, limit=3):
        if type(limit) is not int or not 1 <= limit <= 3:
            raise TradingError("INVALID_GRID_BATCH")
        names = self.market.discover_symbols()
        if not names:
            raise TradingError("NO_DISCOVERED_USDT_PAIRS")
        with self.trader._transaction():
            offset = (self.trader._meta("v7_grid_offset") or 0) % len(names)
        batch = names[offset:offset + limit]
        results = []
        for name in batch:
            try:
                results.append(self.decision(name))
            except TradingError as error:
                results.append({"instrument": name, "eligible": False, "technical_eligible": False,
                                "action": "NO TRADE", "reasons": [error.code]})
        now = self.market._now()
        # Never rank an earlier candidate whose capture expired during the batch.
        for value in results:
            if "as_of_ms" in value and now - value["as_of_ms"] > self.trader.policy.max_market_age * 1000:
                value.update(eligible=False, technical_eligible=False, action="NO TRADE",
                             reasons=value["reasons"] + ["STALE_BATCH_CANDIDATE"])
        with self.trader._transaction():
            self.trader._set_meta("v7_grid_offset", (offset + len(batch)) % len(names))
        return {"radar": "A_SPOT_GRID", "scope": "CURRENT_BOUNDED_BATCH", "discovered": len(names),
                "next_offset": (offset + len(batch)) % len(names), "decisions": results,
                "action": "PAPER_CANDIDATES" if any(r["eligible"] for r in results) else "NO TRADE",
                "live_enabled": False}

    def momentum(self, *, target=None):
        if self.flow is None:
            raise TradingError("FLOW_SCANNER_NOT_CONFIGURED")
        if target is not None:
            target = symbol(target)
        with self.trader._transaction():
            offset = (self.trader._meta("v7_flow_offset") or 0) if target is None else 0
            expected = self.trader._meta("v7_flow_universe") if offset else None
            baseline = self.trader._meta("v7_flow_rotation_baseline")
        # Compare only distinct snapshots within the existing one-hour limit.
        if baseline is not None and not 0 < self.flow._now() - baseline['at_ms'] <= 3600000:
            baseline = None
        try:
            scan = self.flow.scan_batch(offset=offset, limit=3, expected_universe=expected,
                                        previous=baseline, target=target)
        except TradingError as error:
            with self.trader._transaction():
                self.trader._set_meta('v7_flow_health',{'ok':False,'data_status':'DATA_UNVERIFIED',
                    'reason':error.code,'checked_at_ms':self.flow._now()})
                if error.code in {"FLOW_UNIVERSE_CHANGED_RESTART_SCAN", "INCOMPARABLE_FLOW_ROTATION_BASELINE"}:
                    self.trader._set_meta("v7_flow_offset", 0)
                    self.trader._set_meta("v7_flow_universe", None)
                    self.trader._set_meta("v7_flow_rotation_baseline", None)
            raise
        complete_volume = not scan.get('volume_errors') and scan.get('rotation_baseline') is not None
        scan['rotation_status'] = ('INCOMPLETE_UNIVERSE_VOLUME' if not complete_volume else
                                   'COMPARABLE' if baseline is not None else 'BASELINE_STARTED')
        scan['rotation_reference_at_ms'] = baseline['at_ms'] if baseline is not None and complete_volume else None
        state = self.trader.status()
        active = {b["instrument"].split("-")[0] for b in state["bots"] if b["status"] == "OPEN"}
        scan["snapshot_id"] = snapshot(scan)[1]
        scan["radar"] = "B_EARLY_MOMENTUM"
        scan["profile"] = self.trader.config_identity
        scan["fee_model"] = asdict(self.planning)
        scan["risk_admission"] = [{"coin": c["coin"], "allowed": False,
                                   "reason": "CROSS_RADAR_EXPOSURE_CONFLICT" if c["coin"] in active else
                                             "DIRECTIONAL_EXECUTION_PLAN_REQUIRED"} for c in scan["coins"]]
        scan["portfolio"] = state["portfolio"]
        scan["execution_action"] = "NO TRADE"
        scan["data_status"] = ("VERIFIED_TR_PUBLIC" if self.market.real_transport and
                               self.flow.client is self.market.client and
                               any('error' not in report for report in scan['pairs'].values()) else "DATA_UNVERIFIED")
        with self.trader._transaction():
            self.trader._set_meta('v7_flow_health',{'ok':any('error' not in r for r in scan['pairs'].values()),
                'data_status':scan['data_status'],'checked_at_ms':scan.get('as_of_ms',self.flow._now()),
                'rotation_status':scan['rotation_status'],'volume_coverage_complete':complete_volume})
            if target is None:
                self.trader._set_meta("v7_flow_offset", scan["next_offset"] or 0)
                self.trader._set_meta("v7_flow_universe", scan.get("universe_hash"))
            if complete_volume:
                self.trader._set_meta("v7_flow_rotation_baseline", scan['rotation_baseline'])
        return scan

    def start(self, *, now):
        paper_only()
        timestamp(now)
        self._data_gate(self.market.capture("BTC-USDT", refresh=True))
        with self.trader._transaction():
            self.trader._audit_all()
            if self.trader._meta("halted"):
                raise TradingError("PAPER_HALTED")
            if any(n["status"] == "UNKNOWN" for n in notices(self.trader)):
                raise TradingError("NOTIFICATION_OUTCOME_UNKNOWN")
            self.trader._set_meta("v7_started", True)
            self.trader.resume_entries()
        return self.status()

    def status(self):
        with self.trader._transaction():
            state = self.trader.status()
            started = bool(self.trader._meta("v7_started"))
            flow_health = self.trader._meta('v7_flow_health') or {
                'ok':False,'data_status':'DATA_UNVERIFIED','reason':'NOT_CHECKED'}
            row = self.trader._db.execute("SELECT id FROM events WHERE kind='v7_cycle' ORDER BY rowid DESC LIMIT 1").fetchone()
            last_id = row[0].removeprefix("v7_cycle:") if row else None
            last = receipt(self.trader, row[0], "v7_cycle") if row else None
            # Read the durable cycle outcome, not just the last GET. A later
            # good pair must not hide an earlier pair's unresolved failure.
            data_errors = deepcopy(last["result"]["data_errors"]) if last else []
        health = deepcopy(self.market.last_health)
        if health.get("ok") and self.market._now() - health.get("checked_at_ms", 0) > self.trader.policy.max_market_age * 1000:
            health.update(ok=False, data_status="DATA_UNVERIFIED", reason="STALE_CONNECTION_HEALTH")
        if data_errors:
            health.update(ok=False, data_status="DATA_UNVERIFIED", reason="UNRESOLVED_CYCLE_DATA_ERROR",
                          failed_cycle_id=last_id, data_errors=data_errors)
        flow_age = self.flow._now()-flow_health.get('checked_at_ms',0)
        if flow_health.get('ok') and not -2000<=flow_age<=self.trader.policy.max_market_age*1000:
            flow_health.update(ok=False,data_status='DATA_UNVERIFIED',
                               reason='STALE_FLOW_HEALTH' if flow_age>=0 else 'FUTURE_FLOW_HEALTH')
        return {"mode": "paper", "market": health, "flow": flow_health, "paper": state,
                "engine_armed": started and not state["paused"] and not state["halted"] and not data_errors,
                "service": "V5_NOT_CONNECTED", "scheduler": "NOT_VERIFIED", "telegram": "NOT_VERIFIED",
                "live_enabled": False, "LIVE_TRADING": False}

    def run_cycle(self, *, cycle_id, now, notification_healthy=False):
        """Finite, caller-invoked cycle. At most two atomic 300 USDT reservations.

        A data/notification failure persistently pauses new buys. Valid remaining
        books still service existing exits; reconnection never silently resumes.
        """
        paper_only()
        identifier(cycle_id)
        timestamp(now)
        if type(notification_healthy) is not bool:
            raise TradingError("INVALID_NOTIFICATION_HEALTH")
        inputs = {"notification_healthy": notification_healthy}
        with self.trader._transaction():
            old = receipt(self.trader, "v7_cycle:" + cycle_id, "v7_cycle")
            if old:
                if old["inputs"] != inputs:
                    raise TradingError("PAPER_CYCLE_CONFLICT")
                return {**old["result"], "duplicate": True}
            started = bool(self.trader._meta("v7_started"))
            state = self.trader.status()
            pending = self.trader._meta("v7_pending_plans") or {}
        reports, decisions, errors = {}, [], []
        active = [b for b in state["bots"] if b["status"] == "OPEN"]
        names = {b["instrument"] for b in active}
        if started:
            names.update(sorted(pending)[:3])
        for name in sorted(names):
            try:
                report = self.market.capture(name, refresh=True)
                self._data_gate(report)
                reports[name] = report
                if name in pending and started:
                    decisions.append(decide(report, load_v7(), self.planning,
                                            capital=pending[name]["capital"], now_ms=self.market._now()))
            except TradingError as error:
                errors.append({"instrument": name, "reason": error.code})
        with self.trader._transaction():
            old = receipt(self.trader, "v7_cycle:" + cycle_id, "v7_cycle")
            if old:
                return {**old["result"], "duplicate": True}
            last = self.trader._meta("v7_cycle_last_at")
            if last is not None and now <= last:
                raise TradingError("OUT_OF_ORDER_CYCLE")
            # Verify wall-clock freshness again after all GETs and before fills.
            for name, report in list(reports.items()):
                try:
                    self._data_gate(report)
                except TradingError as error:
                    errors.append({"instrument": name, "reason": error.code})
                    del reports[name]
            if errors or not notification_healthy or any(n["status"] == "UNKNOWN" for n in notices(self.trader)):
                self.trader.pause()
            updates = []
            for bot in active:
                report = reports.get(bot["instrument"])
                if report is not None and report["market"]["observed_at"] > bot["last_at"]:
                    try:
                        updates.append(self.trader.process_book(bot["bot_id"], report, now=self.market._now() // 1000))
                    except TradingError as error:
                        if error.code not in {"STALE_OR_FUTURE_BOOK", "OUT_OF_ORDER_BOOK", "REUSED_OR_OLD_BOOK_SEQUENCE", "INSTRUMENT_RULE_CHANGED", "DATA_UNVERIFIED"}:
                            raise
                        errors.append({"instrument": bot["instrument"], "reason": error.code})
                        self.trader.pause()
            openings, rejections = [], []
            for decision in decisions:
                name = decision["instrument"]
                if name not in reports:
                    continue
                allowed = decision["eligible"] or self.trader.synthetic and decision["technical_eligible"]
                if not allowed:
                    rejections.append({"instrument": name, "reasons": decision["reasons"]})
                    continue
                bot_id = "v7_" + hashlib.sha256((cycle_id + name).encode()).hexdigest()[:28]
                try:
                    opened = self.trader.open(bot_id, build_plan(decision["plan"]),
                                              Market.from_dict(reports[name]["market"]), self.market._now() // 1000,
                                              report=reports[name])
                    openings.append(opened["bots"][0]["bot_id"])
                except TradingError as error:
                    rejections.append({"instrument": name, "reasons": [error.code]})
            result = {"mode": "paper", "duplicate": False, "opened": openings, "updates": updates,
                      "rejections": rejections, "data_errors": errors, "state": self.trader.status(),
                      "data_mode": ("SYNTHETIC_TEST" if self.trader.synthetic else
                                    "VERIFIED_TR_PUBLIC" if reports and not errors else "DATA_UNVERIFIED"),
                      "live_enabled": False}
            record(self.trader, "v7_cycle:" + cycle_id, "v7_cycle", now, {"inputs": inputs, "result": result})
            self.trader._set_meta("v7_cycle_last_at", now)
            queue_notice(self.trader, "v7c_" + hashlib.sha256(cycle_id.encode()).hexdigest()[:28],
                         "PAPER 600 USDT | " + ("SYNTHETIC TEST | " if self.trader.synthetic else "") +
                         "opened=" + str(len(openings)) + " | live orders OFF", now)
            return result


def load_v7():
    from .profiles import load_profile
    return load_profile("v7-paper-600")
