"""Grid signal scanner: coins that rose, then went sideways, with volume cooling in the range.

The agreed pattern, found on daily bars and refined on 4H bars:

* a clear run-up came first (the coin rose at least 20% in the 30 days before the range),
* the uptrend is intact (price holds above the 50-day average),
* price then moved sideways: a range 4-20% wide with little net drift,
* volume is read in four ways ("volimetre"): the range trades less than the run-up (cooling) but
  is still liquid, the last days are neither dying nor exploding, on-balance volume is not
  falling and buying volume is at least on par with selling volume,
* price sits inside the range (not on a breakout or breakdown edge).

The grid plan is the range itself. The scan reads market data only: it never looks at the
account balance, planned loss or any risk limit.
"""
from decimal import Decimal
import math
import time

from .domain import TradingError
from .early_long import LEVERAGED_SUFFIXES, _mean
from .tr_public import STABLE

FEE = 0.001            # taker fee per side
MIN_STEP = 0.004       # per-grid price step: twice the fee plus a margin, above the 0.1% OKX floor
MAX_GRIDS = 40
STRONG, WATCH = 70, 55
RANGE_DAYS = (14, 10, 7)  # candidate range lengths, longest first
STOP_BELOW, TAKE_ABOVE = 0.03, 0.05


def _pct(value, digits=1):
    value *= 100
    return ("+" if value > 0 else "−" if value < 0 else "") + f"{abs(value):.{digits}f}%".replace(".", ",")


def range_features(daily):
    """Daily pattern facts. daily: oldest-first bars (ts, o, h, l, c, base_vol, quote_vol, confirmed)."""
    bars = [b for b in daily if b[7]]
    if len(bars) < 60:
        raise TradingError("INSUFFICIENT_DAILY_HISTORY")
    close = [float(b[4]) for b in bars]
    high = [float(b[2]) for b in bars]
    low = [float(b[3]) for b in bars]
    base = [float(b[5]) for b in bars]
    quote = [float(b[6]) for b in bars]
    if min(close) <= 0 or min(low) <= 0:
        raise TradingError("INVALID_PUBLIC_DATA")
    n = len(close)
    sma50 = _mean(close[-50:])
    best = None
    for k in RANGE_DAYS:
        window = slice(n - k, n)
        top, bottom = max(high[window]), min(low[window])
        mid = (top + bottom) / 2
        width = (top - bottom) / mid
        prior = close[n - k - 31:n - k - 1]
        runup = close[n - k - 1] / min(prior) - 1
        drift = abs(close[-1] - close[n - k]) / (top - bottom) if top > bottom else 1.0
        position = (close[-1] - bottom) / (top - bottom) if top > bottom else 0.5
        run_volume = _mean(quote[n - k - 15:n - k]) or 0.0
        range_volume = _mean(quote[window])
        recent_volume = _mean(quote[-3:])
        obv, up_volume, down_volume = 0.0, 0.0, 0.0
        for i in range(n - k, n):
            sign = 1 if close[i] > close[i - 1] else -1 if close[i] < close[i - 1] else 0
            obv += sign * base[i]
            if sign > 0:
                up_volume += quote[i]
            elif sign < 0:
                down_volume += quote[i]
        found = {"k": k, "top": top, "bottom": bottom, "width": width, "runup": runup, "drift": drift,
                 "position": position, "cooling": range_volume / run_volume if run_volume > 0 else 0.0,
                 "recent_vs_range": recent_volume / range_volume if range_volume > 0 else 0.0,
                 "range_quote_volume": range_volume,
                 "obv_share": obv / sum(base[window]) if sum(base[window]) > 0 else 0.0,
                 "up_share": up_volume / (up_volume + down_volume) if up_volume + down_volume > 0 else 0.5,
                 "above_sma50": close[-1] / sma50 - 1, "close": close[-1],
                 "ret_30d": close[-1] / close[-31] - 1}
        if gate(found) is None and best is None:
            best = found
        if best is None and k == RANGE_DAYS[-1]:
            best = found  # nothing fits: keep the shortest window for the explanation
    return best


def gate(f):
    """First hard rule the facts fail, or None when the pattern is present."""
    if f["runup"] < 0.20:
        return "öncesinde belirgin yükseliş yok"
    if f["above_sma50"] < -0.03:
        return "yükseliş trendi bozulmuş (50 günlük ortalamanın altında)"
    if not 0.04 <= f["width"] <= 0.20:
        return "yatay bant çok dar" if f["width"] < 0.04 else "bant çok geniş, yatay değil"
    if f["drift"] > 0.45:
        return "bant içinde net yön var, yatay değil"
    if not 0.10 <= f["position"] <= 0.90:
        return "fiyat bandın kenarında (kırılım/kopuş riski)"
    if f["cooling"] > 1.05:
        return "bantta hacim yükselişteki hacimden büyük"
    if f["cooling"] < 0.25 or f["range_quote_volume"] < 300_000:
        return "bantta hacim çok zayıfladı"
    return None


def score(f):
    """(score, label, reasons). Hard rules first; points rank the coins that pass them."""
    blocked = gate(f)
    if blocked:
        return 0, "UYMUYOR", [blocked]
    points, reasons = 0, []
    points += 20 if f["runup"] >= 0.35 else 15
    reasons.append("öncesinde %{:.0f} yükseliş".format(f["runup"] * 100))
    points += 10 if f["above_sma50"] > 0 else 5
    reasons.append("trend sağlam (50G ortalama {})".format("üstünde" if f["above_sma50"] > 0 else "çevresinde"))
    width = f["width"]
    points += 15 if 0.06 <= width <= 0.14 else 10
    reasons.append("{} günlük yatay bant, genişlik %{:.0f}".format(f["k"], width * 100))
    points += 10 if f["drift"] <= 0.25 else 5
    # volimetre
    cooling = f["cooling"]
    if 0.40 <= cooling <= 0.85:
        points += 15
        reasons.append("hacim soğuyor ama canlı (yükselişin %{:.0f}'i)".format(cooling * 100))
    else:
        points += 8
        reasons.append("hacim yükselişin %{:.0f}'i".format(cooling * 100))
    if 0.7 <= f["recent_vs_range"] <= 1.6:
        points += 10
        reasons.append("son 3 gün hacmi dengeli")
    elif f["recent_vs_range"] > 1.6:
        points += 3
        reasons.append("son 3 günde hacim artıyor (kırılım olabilir)")
    if f["obv_share"] >= -0.10:
        points += 10
        reasons.append("OBV düşmüyor (satış baskısı yok)")
    if f["up_share"] >= 0.47:
        points += 5
        reasons.append("alış hacmi satışa denk")
    if 0.25 <= f["position"] <= 0.75:
        points += 5
    return min(points, 100), ("GÜÇLÜ" if points >= STRONG else "İZLE" if points >= WATCH else "ZAYIF"), reasons


def grid_plan(f, h4):
    """Grid inside the observed range; step, net per-grid return and past activity from 4H bars."""
    lower, upper = f["bottom"], f["top"]
    width = (upper - lower) / upper
    grids = max(3, min(MAX_GRIDS, int(width / 0.006)))
    while grids > 3 and (upper - lower) / grids / upper < MIN_STEP:
        grids -= 1
    step = (upper - lower) / grids
    levels = [lower + step * i for i in range(grids + 1)]
    bars = [b for b in h4 if b[7]][-f["k"] * 6:]
    crossings = 0
    for a, b in zip(bars, bars[1:]):
        lo, hi = sorted((float(a[4]), float(b[4])))
        crossings += sum(1 for line in levels if lo < line <= hi)
    days = max(len(bars) / 6, 1)
    step_net = step / upper - 2 * FEE
    cycles_day = crossings / 2 / days
    return {"lower": lower, "upper": upper, "grids": grids, "step_pct": step / upper, "step_net_pct": step_net,
            "stop": lower * (1 - STOP_BELOW), "take_profit": upper * (1 + TAKE_ABOVE),
            "crossings": crossings, "days": days,
            # every completed buy-sell pair earns the net step on one grid's share of the capital
            "est_daily_pct": cycles_day * step_net / grids}


class RangeGridScanner:
    def __init__(self, reader, *, min_volume=Decimal("1000000"), max_coins=150, clock_ms=None):
        self.reader = reader
        self.min_volume, self.max_coins = Decimal(min_volume), max_coins
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        self.last = None

    def universe(self, tickers):
        pairs = [t for t in tickers if t["quote"] == "USDT" and t["base"] not in STABLE
                 and not t["base"].endswith(LEVERAGED_SUFFIXES) and t["quote_volume_24h"] >= self.min_volume]
        pairs.sort(key=lambda t: t["quote_volume_24h"], reverse=True)
        return [t["instrument"] for t in pairs[:self.max_coins]]

    def analyze(self, instrument):
        f = range_features(self.reader.candles(instrument, "1D", 100))
        points, label, reasons = score(f)
        row = {"instrument": instrument, "score": points, "label": label, "reasons": reasons,
               "price": f["close"], "k": f["k"], "width": f["width"], "runup": f["runup"],
               "cooling": f["cooling"], "position": f["position"]}
        if label in {"GÜÇLÜ", "İZLE", "ZAYIF"}:
            row["plan"] = grid_plan(f, self.reader.candles(instrument, "4H", 120))
            if row["plan"]["step_net_pct"] <= 0 or row["plan"]["crossings"] < 4:
                row.update(score=0, label="UYMUYOR",
                           reasons=["grid adımı komisyonu karşılamıyor" if row["plan"]["step_net_pct"] <= 0
                                    else "bant içinde fiyat neredeyse hiç hareket etmemiş"])
                del row["plan"]
        return row

    def scan(self):
        results, errors = [], 0
        for instrument in self.universe(self.reader.spot_tickers()):
            try:
                row = self.analyze(instrument)
            except (TradingError, ZeroDivisionError, ValueError):
                errors += 1
                continue
            results.append(row)
        results.sort(key=lambda r: r["score"], reverse=True)
        self.last = {"at_ms": self.clock_ms(), "scanned": len(results), "errors": errors, "results": results}
        return self.last


def signal_decision(row, capital, now_ms):
    """A grid decision built from a signal, in the shape the bot manager opens from.

    Nothing here reads a balance or a loss limit; the manager only applies its slot limits."""
    plan = row["plan"]
    price = Decimal(str(row["price"]))
    digits = max(0, 4 - int(math.floor(math.log10(float(price)))) - 1) if price > 0 else 4
    tick = Decimal(1).scaleb(-digits)

    def q(value):
        return Decimal(str(value)).quantize(tick)
    entry_loss = (price - q(plan["stop"])) / price * Decimal(capital)
    return {"decision_id": "range-" + row["instrument"] + "-" + str(now_ms // 3_600_000), "eligible": True,
            "instrument": row["instrument"], "data_status": "VERIFIED_TR_PUBLIC", "as_of_ms": now_ms,
            "planned_loss_usdt": str(entry_loss.quantize(Decimal("0.01"))),
            "plan": {"lower": str(q(plan["lower"])), "upper": str(q(plan["upper"])), "stop": str(q(plan["stop"])),
                     "take_profit": str(q(plan["take_profit"])), "tick_size": str(tick), "capital": str(capital),
                     "grids": plan["grids"]},
            "market_summary": {"price": str(price)}}
