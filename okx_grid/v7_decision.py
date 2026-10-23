"""Shared plan, net-return, mandatory filters and explainable scoring."""
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
from types import MappingProxyType

from .domain import (GridPlan, Market, TradingError, canonical, ceil_step,
                     floor_step, number, positive, precise)
from .public_contracts import InstrumentRules, TR_ORIGIN

WEIGHTS = {"data": 20, "liquidity": 15, "trend": 15, "btc_regime": 15,
           "grid_economics": 20, "planned_risk": 15}
GRID_FILTERS = MappingProxyType({"min_return_15m": "0", "min_return_1h": "0", "min_return_4h": "0",
    "max_return_15m": "0.05", "btc_risk_off_1h": "-0.02", "btc_risk_off_4h": "-0.04"})


@dataclass(frozen=True)
class PlanningPolicy:
    fee_rate: str = "0.001"
    slippage_rate: str = "0.0002"
    min_net_grid_return: str = "0.0001"
    min_notional: str = "5"
    stop_buffer_fraction: str = "0.0005"
    atr_stop_multiple: str = "0.1"
    max_grids: int = 20

    def __post_init__(self):
        for key in ("fee_rate", "slippage_rate"):
            if not 0 <= number(getattr(self, key)) <= Decimal("0.02"):
                raise TradingError("INVALID_PLANNING_POLICY")
        for key in ("min_net_grid_return", "min_notional", "stop_buffer_fraction", "atr_stop_multiple"):
            positive(getattr(self, key))
        if type(self.max_grids) is not int or not 2 <= self.max_grids <= 100:
            raise TradingError("INVALID_PLANNING_POLICY")


def score_checks(checks):
    if set(checks) != set(WEIGHTS) or any(type(v) is not bool for v in checks.values()):
        raise TradingError("INVALID_MANDATORY_FILTERS")
    return {"score": sum(WEIGHTS[k] for k, passed in checks.items() if passed),
            "passed": sum(checks.values()), "required": len(WEIGHTS),
            "weights": dict(WEIGHTS), "checks": checks, "eligible": all(checks.values())}


@precise
def decide(report, profile, planning, *, capital="300", now_ms):
    capital = positive(capital)
    market = Market.from_dict(report["market"])
    checks = {k: True for k in WEIGHTS}
    reasons, plan, nets, planning_error = [], None, (), None

    def reject(key, code):
        checks[key] = False
        if code not in reasons:
            reasons.append(code)

    if (report.get("source_origin") != TR_ORIGIN or report.get("exchange") != "OKX_TR_SPOT"
            or report.get("integrity_validated") is not True
            or not -profile.policy.max_future_seconds * 1000 <= now_ms - report["as_of_ms"] <= profile.policy.max_market_age * 1000
            or now_ms - report["book_ts_ms"] > profile.policy.max_market_age * 1000
            or len(market.bids) != 400 or len(market.asks) != 400):
        reject("data", "INVALID_OR_STALE_SNAPSHOT")
    try:
        profile.policy.check_market(market, now_ms // 1000, capital)
    except TradingError as error:
        reject("liquidity", error.code)
    trend = {k: number(report["trend"][k]) for k in ("return_15m", "return_1h", "return_4h", "btc_return_1h", "btc_return_4h")}
    if any(trend[k] < number(GRID_FILTERS["min_" + k]) for k in ("return_15m", "return_1h", "return_4h")):
        reject("trend", "DOWNTREND")
    if trend["return_15m"] > number(GRID_FILTERS["max_return_15m"]):
        reject("trend", "EXCESSIVE_SHORT_MOMENTUM")
    if (trend["btc_return_1h"] <= number(GRID_FILTERS["btc_risk_off_1h"]) or
            trend["btc_return_4h"] <= number(GRID_FILTERS["btc_risk_off_4h"])):
        reject("btc_regime", "BTC_RISK_OFF")
    try:
        rules = report["rules"]
        tick, lot, min_size = (positive(rules[k]) for k in ("tick_size", "lot_size", "min_size"))
        lower = floor_step(positive(report["range"]["support"]), tick)
        upper = ceil_step(positive(report["range"]["resistance"]), tick)
        buffer = max(lower * positive(planning.stop_buffer_fraction),
                     positive(report["volatility"]["atr_20_15m"]) * positive(planning.atr_stop_multiple), tick)
        stop = floor_step(lower - buffer, tick)
        tp = ceil_step(upper * (1 + positive(planning.stop_buffer_fraction)), tick)
        # Prefer more grid intervals only while every net return is sufficient.
        for count in range(planning.max_grids, 1, -1):
            try:
                candidate = GridPlan(market.instrument, lower, upper, count, capital, stop, tp,
                                     planning.fee_rate, planning.slippage_rate, tick, lot, planning.min_notional)
                InstrumentRules(market.instrument, tick, lot, min_size).check_plan(candidate)
                if min(candidate.net_grid_returns) < positive(planning.min_net_grid_return):
                    raise TradingError("INSUFFICIENT_NET_GRID_RETURN")
                plan, nets = candidate, candidate.net_grid_returns
                break
            except TradingError as error:
                planning_error = error.code
        if plan is None:
            raise TradingError(planning_error or "NO_FEASIBLE_GRID")
        if not plan.lower < market.price < plan.upper:
            reject("grid_economics", "ENTRY_OUTSIDE_GRID")
        if capital != profile.policy.max_bot_capital:
            reject("planned_risk", "PROFILE_REQUIRES_300_USDT_PER_BOT")
        if plan.planned_loss > profile.policy.equity * profile.policy.risk_fraction:
            reject("planned_risk", "BOT_RISK_LIMIT")
    except TradingError as error:
        reject("grid_economics", error.code)
        reject("planned_risk", "RISK_NOT_CALCULABLE")
    scoring = score_checks(checks)
    verified = report.get("data_status") == "VERIFIED_TR_PUBLIC"
    eligible = scoring["eligible"] and verified
    if not verified:
        reasons.append("DATA_UNVERIFIED")
    configuration = {"profile": profile.identity, "planning": asdict(planning), "capital": str(capital),
                     "grid_filters": dict(GRID_FILTERS)}
    config_id = hashlib.sha256(canonical(configuration).encode()).hexdigest()
    decision_id = hashlib.sha256((report["snapshot_id"] + config_id).encode()).hexdigest()
    return {"decision_id": decision_id, "snapshot_id": report["snapshot_id"], "configuration_id": config_id,
            "instrument": market.instrument, "as_of_ms": report["as_of_ms"], "data_status": report["data_status"],
            "action": "PAPER_CANDIDATE" if eligible else "NO TRADE", "eligible": eligible,
            "technical_eligible": scoring["eligible"], "scoring": scoring, "reasons": reasons,
            "plan": plan.to_dict() if plan else None, "net_grid_returns": [str(v) for v in nets],
            "planned_loss_usdt": str(plan.planned_loss) if plan else None,
            "risk_caps": {"per_bot": str(profile.policy.equity * profile.policy.risk_fraction),
                          "total": str(profile.policy.equity * profile.policy.total_risk_fraction)},
            "fee_model": asdict(planning), "profile": profile.identity, "live_enabled": False,
            "market_summary": {"price": str(market.price), "bid": str(market.bid), "ask": str(market.ask),
                               "spread_bps": str(market.spread_bps), "quote_volume_24h_usdt": str(market.quote_volume_24h),
                               "book_ts_ms": report["book_ts_ms"], "ticker_ts_ms": report["ticker_ts_ms"],
                               "sequence": report["sequence"],
                               "book_levels": [len(market.bids), len(market.asks)], "trend": report["trend"],
                               "depth_0_5pct": report["book"]["0.5pct"], "depth_1pct": report["book"]["1pct"],
                               "volatility": report["volatility"], "range": report["range"]},
            "stop_note": "Planned scenario, not a guaranteed loss cap; gaps and liquidity may increase loss.",
            "trigger": {"condition": "FRESH_VALIDATED_SNAPSHOT_ALL_MANDATORY_FILTERS_AND_PORTFOLIO_RESERVATION",
                        "grid_buy": "LIMIT_ASK_AT_OR_BELOW_BUY_LEVEL", "tp_sl_semantics": "PRICE_LEVELS_NOT_PORTFOLIO_PNL"}}
