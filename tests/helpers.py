from dataclasses import asdict
from okx_grid.scenarios import sample_market, sample_plan


def market_dict(**updates):
    values = asdict(sample_market())
    for key in ("price", "bid", "ask", "quote_volume_24h"):
        values[key] = str(values[key])
    for key in ("bids", "asks"):
        values[key] = [[str(p), str(q)] for p, q in values[key]]
    values.update(updates)
    return values


def proposal():
    return {"market": market_dict(), "plan": sample_plan().to_dict(),
            "trend": {"return_15m": "0.001", "return_1h": "0.005", "btc_return_1h": "0", "observed_at": 1000}}
