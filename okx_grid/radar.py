"""Screen supplied proposals; this module does not fetch or invent market data."""
from .domain import Market, TradingError, build_plan, number, timestamp


def screen(proposals, policy, now):
    timestamp(now)
    if not isinstance(proposals, list) or len(proposals) > 500:
        raise TradingError("INVALID_RADAR_INPUT")
    accepted, rejected, seen = [], [], set()
    for index, proposal in enumerate(proposals):
        label = f"candidate-{index + 1}"
        try:
            market = Market.from_dict(proposal["market"])
            label = market.instrument
            if label in seen:
                raise TradingError("DUPLICATE_INSTRUMENT")
            seen.add(label)
            plan = build_plan(proposal["plan"])
            plan.check(policy, market, now)
            trend = proposal["trend"]
            at = timestamp(trend["observed_at"])
            if now - at > policy.max_market_age or at - now > policy.max_future_seconds:
                raise TradingError("STALE_TREND")
            short, hour, btc = (number(trend[k]) for k in ("return_15m", "return_1h", "btc_return_1h"))
            if any(abs(v) > 1 for v in (short, hour, btc)):
                raise TradingError("INVALID_TREND")
            if short < 0 or hour < 0:
                raise TradingError("DOWNTREND")
            if btc <= number("-0.02"):
                raise TradingError("BTC_RISK_OFF")
            if short > number("0.05"):
                raise TradingError("EXCESSIVE_SHORT_MOMENTUM")
            accepted.append({"instrument": label, "spread_bps": str(market.spread_bps),
                             "quote_volume_24h": str(market.quote_volume_24h),
                             "planned_stop_loss": str(plan.planned_loss), "plan_digest": plan.digest,
                             "plan": plan.to_dict(), "market_observed_at": market.observed_at})
        except TradingError as error:
            rejected.append({"instrument": label, "reason": error.code})
        except (KeyError, TypeError):
            rejected.append({"instrument": label, "reason": "INVALID_RADAR_INPUT"})
    accepted.sort(key=lambda row: (number(row["spread_bps"]), -number(row["quote_volume_24h"]), row["instrument"]))
    return {"mode": "paper", "accepted": accepted, "rejected": rejected}
