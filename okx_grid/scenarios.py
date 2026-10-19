"""Synthetic fixtures only; not historical prices, recommendations, or broker orders."""
from .domain import GridPlan, Market
from .paper import PaperTrader


def sample_market(at=1000):
    return Market.from_dict({"instrument": "TEST-USDT", "price": "100", "bid": "99.99", "ask": "100.01",
                             "quote_volume_24h": "20000000", "observed_at": at,
                             "bids": [["99.99", "1000"], ["99.95", "1000"]],
                             "asks": [["100.01", "1000"], ["100.05", "1000"]]})


def sample_plan(**updates):
    values = dict(instrument="TEST-USDT", lower="98", upper="102", grids=4, capital="300",
                  stop="97", take_profit="103")
    values.update(updates)
    return GridPlan(**values)


def demo():
    with PaperTrader(":memory:") as trader:
        trader.open("synthetic", sample_plan(), sample_market(), 1000)
        trader.tick("synthetic", "down", "98", 1001, 1001)
        trader.tick("synthetic", "up", "102", 1002, 1002)
        return trader.tick("synthetic", "end", "103", 1003, 1003)
