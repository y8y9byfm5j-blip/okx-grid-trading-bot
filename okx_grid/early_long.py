"""Early long scanner: coins being accumulated before a large move.

Looks for the structure agreed with the owner on daily bars, confirmed on a
weekly view (days + weeks horizon):

* price has started to rise only slightly (not already pumped),
* volume expands against the previous weeks,
* on-balance volume rises while price is still below its recent high,
* volatility was compressed (a squeeze before an expansion),
* the daily trend turns up and weekly lows are rising,
* the coin is stronger than BTC, and aggressive buyers dominate (when known).

The score is an explainable heuristic for a watch list, not a forecast.
"""
from decimal import Decimal
import math
import time

from .domain import TradingError
from .tr_public import STABLE

STRONG, WATCH = 70, 55
LEVERAGED_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN")


def _pct(value):
    value *= 100
    return ("+" if value > 0 else "−" if value < 0 else "") + f"%{abs(value):.1f}".replace(".", ",")


def _mean(values):
    return sum(values) / len(values)


def _std(values):
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _sma(values, n, end=None):
    end = len(values) if end is None else end
    return _mean(values[end - n:end])


def features(daily, btc_daily=None):
    """daily: oldest-first confirmed bars (ts, o, h, l, c, base_vol, quote_vol, confirmed)."""
    bars = [b for b in daily if b[7]]
    if len(bars) < 60:
        raise TradingError("INSUFFICIENT_DAILY_HISTORY")
    close = [float(b[4]) for b in bars]
    low = [float(b[3]) for b in bars]
    base_vol = [float(b[5]) for b in bars]
    quote_vol = [float(b[6]) for b in bars]
    if min(close) <= 0:
        raise TradingError("INVALID_PUBLIC_DATA")

    def ret(n):
        return close[-1] / close[-1 - n] - 1

    obv = [0.0]
    for i in range(1, len(close)):
        sign = 1 if close[i] > close[i - 1] else -1 if close[i] < close[i - 1] else 0
        obv.append(obv[-1] + sign * base_vol[i])
    widths = []
    for end in range(20, len(close) + 1):
        window = close[end - 20:end]
        mean = _mean(window)
        widths.append(4 * _std(window) / mean if mean else 0)
    recent_widths = widths[-60:]
    squeeze_now = sum(w <= widths[-1] for w in recent_widths) / len(recent_widths)
    squeeze_recent = min(sum(w <= widths[-1 - k] for w in recent_widths) / len(recent_widths) for k in range(6))
    weekly_lows = [min(low[len(low) - 7 * (k + 1):len(low) - 7 * k]) for k in range(4)][::-1]
    prior_vol = _mean(quote_vol[-28:-7])
    result = {
        "close": close[-1], "ret_1d": ret(1), "ret_7d": ret(7), "ret_14d": ret(14), "ret_30d": ret(30),
        "from_high_90d": close[-1] / max(close[-90:]) - 1,
        "volume_ratio": _mean(quote_vol[-7:]) / prior_vol if prior_vol > 0 else 0.0,
        "avg_quote_volume_7d": _mean(quote_vol[-7:]),
        "obv_rising": obv[-1] > obv[-15] and obv[-1] >= max(obv[-30:]) * (0.98 if max(obv[-30:]) > 0 else 1.02),
        "obv_up_14d": obv[-1] > obv[-15],
        "below_high_30d": close[-1] / max(close[-30:]) - 1,
        "squeeze_percentile": squeeze_now, "squeeze_recent_percentile": squeeze_recent,
        "above_sma20": close[-1] > _sma(close, 20),
        "sma20_slope": _sma(close, 20) / _sma(close, 20, len(close) - 5) - 1,
        "sma20_over_sma50": _sma(close, 20) > _sma(close, 50),
        "sma_cross_10d": any(_sma(close, 20, len(close) - k) > _sma(close, 50, len(close) - k) and
                             _sma(close, 20, len(close) - k - 1) <= _sma(close, 50, len(close) - k - 1)
                             for k in range(10)),
        "weekly_higher_lows": sum(1 for a, b in zip(weekly_lows, weekly_lows[1:]) if b > a),
        "ret_28d": ret(28),
    }
    if btc_daily:
        btc = [float(b[4]) for b in btc_daily if b[7]]
        if len(btc) > 15:
            result["rs_btc_14d"] = result["ret_14d"] - (btc[-1] / btc[-15] - 1)
    return result


def score(f, accumulation=None):
    """Return (score, label, reasons in Turkish). Late movers are excluded, not ranked."""
    late = []
    if f["ret_30d"] > 0.60:
        late.append("30 günde %{:.0f} yükselmiş".format(f["ret_30d"] * 100))
    if f["ret_7d"] > 0.35:
        late.append("7 günde %{:.0f} yükselmiş".format(f["ret_7d"] * 100))
    if f["ret_1d"] > 0.20:
        late.append("son gün %{:.0f} yükselmiş".format(f["ret_1d"] * 100))
    if late:
        return 0, "GEÇ", ["Geç kalındı: " + ", ".join(late)]
    points, reasons = 0, []
    r7 = f["ret_7d"]
    if 0.02 <= r7 <= 0.20:
        points += 20
        reasons.append("hafif yükseliş başladı (7g {})".format(_pct(r7)))
    elif 0 <= r7 < 0.02:
        points += 10
        reasons.append("fiyat yatay/hafif artı (7g {})".format(_pct(r7)))
    elif r7 > 0.20:
        points += 5
        reasons.append("yükseliş hızlanmış (7g {})".format(_pct(r7)))
    vr = f["volume_ratio"]
    if vr >= 2:
        points += 20
    elif vr >= 1.5:
        points += 15
    elif vr >= 1.2:
        points += 8
    if vr >= 1.2:
        reasons.append("hacim önceki 3 haftanın {} katı".format(f"{vr:.1f}".replace(".", ",")))
    if f["obv_rising"] and -0.15 <= f["below_high_30d"] <= 0:
        points += 15
        reasons.append("OBV zirvede, fiyat henüz zirvenin altında (birikim)")
    elif f["obv_up_14d"]:
        points += 8
        reasons.append("OBV 14 günde artıyor")
    if f["squeeze_percentile"] <= 0.30 or f["squeeze_recent_percentile"] <= 0.30:
        points += 10
        reasons.append("volatilite sıkışması (kırılım öncesi)")
    if f["above_sma20"] and f["sma20_slope"] > 0:
        points += 10
        reasons.append("fiyat yükselen 20 günlük ortalamanın üstünde")
        if f["sma20_over_sma50"] or f["sma_cross_10d"]:
            points += 5
            reasons.append("20G ortalama 50G'nin üstünde" if not f["sma_cross_10d"] else "20G/50G yukarı kesişim")
    if f["weekly_higher_lows"] >= 3:
        points += 10
        reasons.append("haftalık dipler yükseliyor (3 hafta)")
    elif f["weekly_higher_lows"] == 2:
        points += 5
        reasons.append("haftalık dipler yükseliyor")
    rs = f.get("rs_btc_14d")
    if rs is not None and rs > 0.05:
        points += 10
        reasons.append("BTC'den güçlü (14g {})".format(_pct(rs)))
    elif rs is not None and rs > 0:
        points += 5
        reasons.append("BTC'den biraz güçlü")
    if accumulation is not None and accumulation > Decimal("0.05"):
        points += 5
        reasons.append("7 günde agresif alıcılar baskın")
    points = min(points, 100)
    label = "GÜÇLÜ" if points >= STRONG else "İZLE" if points >= WATCH else "ZAYIF"
    return points, label, reasons


class EarlyLongScanner:
    def __init__(self, reader, *, rotation=None, min_volume=Decimal("1000000"), max_coins=120, clock_ms=None):
        self.reader, self.rotation = reader, rotation
        self.min_volume, self.max_coins = Decimal(min_volume), max_coins
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        self.last = None

    def universe(self, tickers):
        pairs = [t for t in tickers if t["quote"] == "USDT" and t["base"] not in STABLE
                 and not t["base"].endswith(LEVERAGED_SUFFIXES) and t["quote_volume_24h"] >= self.min_volume]
        pairs.sort(key=lambda t: t["quote_volume_24h"], reverse=True)
        return [t["instrument"] for t in pairs[:self.max_coins]]

    def scan(self):
        tickers = self.reader.spot_tickers()
        btc = self.reader.candles("BTC-USDT", "1D", 100)
        results, errors = [], 0
        for instrument in self.universe(tickers):
            try:
                f = features(self.reader.candles(instrument, "1D", 100), btc)
            except TradingError:
                errors += 1
                continue
            coin = instrument.split("-")[0]
            acc = self.rotation.accumulation_ratio(coin) if self.rotation is not None else None
            points, label, reasons = score(f, acc)
            results.append({"instrument": instrument, "score": points, "label": label, "reasons": reasons,
                            "ret_7d": f["ret_7d"], "ret_30d": f["ret_30d"], "volume_ratio": f["volume_ratio"],
                            "price": f["close"]})
        results.sort(key=lambda r: r["score"], reverse=True)
        self.last = {"at_ms": self.clock_ms(), "scanned": len(results), "errors": errors, "results": results}
        return self.last
