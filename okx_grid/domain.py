"""Validated market inputs, decimal arithmetic and conservative grid planning."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
from functools import wraps
import hashlib
import json
import os
import re


class TradingError(ValueError):
    """A stable public error code; callers must not log untrusted payloads."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def precise(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 120
            return function(*args, **kwargs)
    return wrapped


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise TradingError("INVALID_NUMBER")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise TradingError("INVALID_NUMBER") from None
    if not result.is_finite() or abs(result) > Decimal("1e18"):
        raise TradingError("INVALID_NUMBER")
    if result.as_tuple().exponent < -18:
        raise TradingError("EXCESS_PRECISION")
    return result


def positive(value):
    value = number(value)
    if value <= 0:
        raise TradingError("NON_POSITIVE_NUMBER")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise TradingError("INVALID_IDENTIFIER")
    return value


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{2,20}-USDT", value):
        raise TradingError("INVALID_SPOT_SYMBOL")
    return value


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TradingError("INVALID_TIMESTAMP")
    return value


def paper_only():
    if (os.environ.get("TRADING_MODE", "paper").lower() != "paper"
            or os.environ.get("LIVE_TRADING", "false").lower() not in {"", "0", "false", "no"}):
        raise TradingError("LIVE_TRADING_DISABLED")


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Market:
    instrument: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    quote_volume_24h: Decimal
    observed_at: int
    bids: tuple
    asks: tuple

    @precise
    def __post_init__(self):
        object.__setattr__(self, "instrument", symbol(self.instrument))
        for name in ("price", "bid", "ask"):
            object.__setattr__(self, name, positive(getattr(self, name)))
        object.__setattr__(self, "quote_volume_24h", number(self.quote_volume_24h))
        timestamp(self.observed_at)
        try:
            for name in ("bids", "asks"):
                rows = getattr(self, name)
                if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 400:
                    raise TradingError("INVALID_ORDER_BOOK")
                rows = tuple((positive(p), positive(q)) for p, q in rows)
                object.__setattr__(self, name, rows)
        except (ValueError, TypeError) as error:
            if isinstance(error, TradingError):
                raise
            raise TradingError("INVALID_ORDER_BOOK") from None
        if (self.quote_volume_24h < 0 or self.bid >= self.ask
                or self.bids[0][0] != self.bid or self.asks[0][0] != self.ask
                or any(a[0] <= b[0] for a, b in zip(self.bids, self.bids[1:]))
                or any(a[0] >= b[0] for a, b in zip(self.asks, self.asks[1:]))):
            raise TradingError("INVALID_ORDER_BOOK")

    @classmethod
    def from_dict(cls, data):
        try:
            def levels(key):
                rows = data[key]
                if not isinstance(rows, list) or not 1 <= len(rows) <= 400:
                    raise TradingError("INVALID_ORDER_BOOK")
                if any(not isinstance(r, list) or len(r) != 2 for r in rows):
                    raise TradingError("INVALID_ORDER_BOOK")
                return tuple((positive(p), positive(q)) for p, q in rows)
            value = cls(symbol(data["instrument"]), positive(data["price"]),
                        positive(data["bid"]), positive(data["ask"]),
                        number(data["quote_volume_24h"]), timestamp(data["observed_at"]),
                        levels("bids"), levels("asks"))
        except (KeyError, TypeError):
            raise TradingError("INVALID_MARKET") from None
        return value

    @property
    @precise
    def spread_bps(self):
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 10000


@dataclass(frozen=True)
class RiskPolicy:
    equity: Decimal = Decimal("2000")
    max_bot_capital: Decimal = Decimal("500")
    max_allocation_fraction: Decimal = Decimal("0.8")
    risk_fraction: Decimal = Decimal("0.01")
    total_risk_fraction: Decimal = Decimal("0.03")
    max_bots: int = 3
    min_quote_volume: Decimal = Decimal("10000000")
    max_spread_bps: Decimal = Decimal("10")
    max_market_age: int = 30
    max_future_seconds: int = 2
    min_depth_multiple: Decimal = Decimal("5")
    max_realized_loss_fraction: Decimal = Decimal("0.02")

    def __post_init__(self):
        for name in ("equity", "max_bot_capital", "max_allocation_fraction", "risk_fraction",
                     "total_risk_fraction", "min_quote_volume", "max_spread_bps", "min_depth_multiple", "max_realized_loss_fraction"):
            object.__setattr__(self, name, positive(getattr(self, name)))
        if (not 0 < self.risk_fraction <= self.total_risk_fraction <= 1
                or not 0 < self.max_allocation_fraction <= 1
                or not 0 < self.max_realized_loss_fraction <= 1
                or type(self.max_bots) is not int or not 1 <= self.max_bots <= 100
                or type(self.max_market_age) is not int or not 1 <= self.max_market_age <= 300
                or type(self.max_future_seconds) is not int or not 0 <= self.max_future_seconds <= 5):
            raise TradingError("INVALID_RISK_POLICY")

    def to_dict(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}

    @precise
    def check_market(self, market, now, capital):
        timestamp(now)
        if now - market.observed_at > self.max_market_age:
            raise TradingError("STALE_MARKET")
        if market.observed_at - now > self.max_future_seconds:
            raise TradingError("FUTURE_MARKET")
        if market.quote_volume_24h < self.min_quote_volume:
            raise TradingError("LOW_QUOTE_VOLUME")
        if market.spread_bps > self.max_spread_bps:
            raise TradingError("WIDE_SPREAD")
        if abs(market.price / ((market.bid + market.ask) / 2) - 1) > Decimal("0.01"):
            raise TradingError("PRICE_BOOK_DIVERGENCE")
        # Only count displayed liquidity within 0.5% of the touch, on both sides.
        bid_depth = sum((p * q for p, q in market.bids if p >= market.bid * Decimal("0.995")), Decimal(0))
        ask_depth = sum((p * q for p, q in market.asks if p <= market.ask * Decimal("1.005")), Decimal(0))
        if min(bid_depth, ask_depth) < capital * self.min_depth_multiple:
            raise TradingError("INSUFFICIENT_DEPTH")


@dataclass(frozen=True)
class GridPlan:
    instrument: str
    lower: Decimal
    upper: Decimal
    grids: int
    capital: Decimal
    stop: Decimal
    take_profit: Decimal
    fee_rate: Decimal = Decimal("0.001")
    slippage_rate: Decimal = Decimal("0.001")
    tick_size: Decimal = Decimal("0.01")
    lot_size: Decimal = Decimal("0.001")
    min_notional: Decimal = Decimal("5")

    @precise
    def __post_init__(self):
        object.__setattr__(self, "instrument", symbol(self.instrument))
        for name in ("lower", "upper", "capital", "stop", "take_profit", "tick_size", "lot_size", "min_notional"):
            object.__setattr__(self, name, positive(getattr(self, name)))
        for name in ("fee_rate", "slippage_rate"):
            object.__setattr__(self, name, number(getattr(self, name)))
        if (type(self.grids) is not int or not 2 <= self.grids <= 100
                or not self.stop < self.lower < self.upper < self.take_profit
                or not 0 <= self.fee_rate <= Decimal("0.02")
                or not 0 <= self.slippage_rate <= Decimal("0.05")):
            raise TradingError("INVALID_PLAN")
        if any(v % self.tick_size for v in (self.lower, self.upper, self.stop, self.take_profit)):
            raise TradingError("UNALIGNED_PRICE")
        levels = self.levels
        if any(a >= b for a, b in zip(levels, levels[1:])):
            raise TradingError("GRID_BELOW_TICK_SIZE")
        for buy, sell in zip(levels, levels[1:]):
            quantity = self.quantity(buy)
            number(quantity)
            if quantity <= 0 or quantity * buy < self.min_notional:
                raise TradingError("GRID_BELOW_MIN_NOTIONAL")
            if sell * (1 - self.fee_rate) <= buy * (1 + self.fee_rate):
                raise TradingError("GRID_NOT_PROFITABLE_AFTER_FEES")

    @property
    @precise
    def levels(self):
        return tuple(floor_step(self.lower + (self.upper - self.lower) * i / self.grids, self.tick_size)
                     for i in range(self.grids + 1))

    @precise
    def quantity(self, price):
        return floor_step(self.capital / self.grids / (price * (1 + self.fee_rate)), self.lot_size)

    @property
    @precise
    def planned_loss(self):
        # Conservative full-capital entry at the top; this is a scenario, not a guaranteed maximum.
        entry_cost = self.upper * (1 + self.slippage_rate) * (1 + self.fee_rate)
        exit_net = self.stop * (1 - self.slippage_rate) * (1 - self.fee_rate)
        return ceil_step(self.capital * (1 - exit_net / entry_cost), Decimal("0.00000001"))

    def to_dict(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()}

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.to_dict()).encode()).hexdigest()

    @precise
    def check(self, policy, market, now):
        policy.check_market(market, now, self.capital)
        if market.instrument != self.instrument or not self.lower < market.price < self.upper:
            raise TradingError("ENTRY_OUTSIDE_GRID")
        if self.capital > policy.max_bot_capital:
            raise TradingError("BOT_CAPITAL_LIMIT")
        if self.planned_loss > policy.equity * policy.risk_fraction:
            raise TradingError("BOT_RISK_LIMIT")


def build_plan(data):
    try:
        return GridPlan(**data)
    except (TypeError, KeyError):
        raise TradingError("INVALID_PLAN") from None
