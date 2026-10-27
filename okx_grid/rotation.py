"""Money rotation tracker: which coins money leaves and which it enters.

Two independent OKX measurements are combined, both from tr.okx.com:

* 24h quote-volume share per coin from all spot tickers, stored every scan so
  share changes over 4h / 24h / 7d show where trading activity moves. TRY
  pairs are tracked separately as the Turkish-lira side of the market.
* Aggregated taker buy and sell volume per coin (rubik/stat). Net taker buy
  in USDT shows whether aggressive buyers or sellers dominated.

Neither number is customer balances or exchange deposits; they describe
executed trading only.
"""
from decimal import Decimal
import json
import sqlite3
import threading
import time

from .tr_public import coin_volumes

HORIZONS = (("4s", 4 * 3_600_000), ("24s", 24 * 3_600_000), ("7g", 7 * 24 * 3_600_000))
KEEP_MS = 8 * 24 * 3_600_000  # the longest comparison window is 7 days
STORE_COINS = 200  # most traded coins only; the rest add size but no signal


class RotationTracker:
    def __init__(self, path, reader, *, clock_ms=None, flow_coins=40):
        self.reader = reader
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        self.flow_coins = flow_coins
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS snaps (at_ms INTEGER, coin TEXT, usdt_vol TEXT, try_vol TEXT,
                price TEXT, change_24h TEXT, PRIMARY KEY(at_ms, coin));
            CREATE INDEX IF NOT EXISTS snaps_at ON snaps(at_ms);
            CREATE TABLE IF NOT EXISTS flows (at_ms INTEGER PRIMARY KEY, body TEXT);
        """)
        self.latest_coins = None

    # -- collection -------------------------------------------------------------------
    def collect(self):
        """One cheap all-tickers call; store per-coin volumes for share history."""
        with self._lock:
            now = self.clock_ms()
            coins, rate = coin_volumes(self.reader.spot_tickers())
            top = sorted(coins.items(), key=lambda kv: kv[1]["usdt_volume"] + kv[1]["try_volume"], reverse=True)
            self._db.execute("BEGIN")
            self._db.executemany("INSERT OR REPLACE INTO snaps VALUES(?,?,?,?,?,?)", [
                (now, c, str(v["usdt_volume"]), str(v["try_volume"]), str(v["price"]),
                 str(v["change_24h"]) if v["change_24h"] is not None else None) for c, v in top[:STORE_COINS]])
            self._db.execute("DELETE FROM snaps WHERE at_ms < ?", (now - KEEP_MS,))
            self._db.execute("COMMIT")
            self.latest_coins = coins
            return {"at_ms": now, "coins": len(coins), "usdt_try": rate}

    def collect_flows(self):
        """Net taker buy (USDT) per top coin over 24h and 7d. ~2 requests per coin."""
        with self._lock:
            coins = self.latest_coins or self._snapshot(self._last_snapshot_time())
        if not coins:
            return None
        ranked = sorted(coins.items(), key=lambda kv: kv[1]["usdt_volume"] + kv[1]["try_volume"], reverse=True)
        result = {}
        for coin, info in ranked[:self.flow_coins]:
            price = info["price"]
            try:
                hourly = self.reader.taker_volume(coin, "1H")[-24:]
                daily = self.reader.taker_volume(coin, "1D")[-7:]
            except Exception:  # one coin's failure never hides the others
                continue
            buy24 = sum(r[2] for r in hourly) * price
            sell24 = sum(r[1] for r in hourly) * price
            buy7 = sum(r[2] for r in daily) * price
            sell7 = sum(r[1] for r in daily) * price
            result[coin] = {"net_24h": str(buy24 - sell24), "gross_24h": str(buy24 + sell24),
                            "net_7d": str(buy7 - sell7), "gross_7d": str(buy7 + sell7)}
        now = self.clock_ms()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO flows VALUES(?,?)", (now, json.dumps(result)))
            self._db.execute("DELETE FROM flows WHERE at_ms < ?", (now - KEEP_MS,))
        return {"at_ms": now, "coins": len(result)}

    def close(self):
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    # -- queries ----------------------------------------------------------------------
    def _last_snapshot_time(self):
        row = self._db.execute("SELECT MAX(at_ms) FROM snaps").fetchone()
        return row[0] if row else None

    def _snapshot_near(self, target_ms, tolerance_ms):
        row = self._db.execute("SELECT at_ms FROM snaps WHERE at_ms BETWEEN ? AND ? "
                               "ORDER BY ABS(at_ms - ?) LIMIT 1",
                               (target_ms - tolerance_ms, target_ms + tolerance_ms, target_ms)).fetchone()
        return row[0] if row else None

    def _snapshot(self, at_ms):
        if at_ms is None:
            return {}
        out = {}
        for coin, usdt, lira, price, change in self._db.execute(
                "SELECT coin, usdt_vol, try_vol, price, change_24h FROM snaps WHERE at_ms=?", (at_ms,)):
            out[coin] = {"usdt_volume": Decimal(usdt), "try_volume": Decimal(lira), "price": Decimal(price),
                         "change_24h": Decimal(change) if change is not None else None}
        return out

    def coins(self):
        """Most recent per-coin volumes: this process's scan, else the last stored snapshot."""
        with self._lock:
            return self.latest_coins or self._snapshot(self._last_snapshot_time())

    def latest_flows(self):
        with self._lock:
            row = self._db.execute("SELECT at_ms, body FROM flows ORDER BY at_ms DESC LIMIT 1").fetchone()
        if not row:
            return None, {}
        return row[0], {c: {k: Decimal(v) for k, v in d.items()} for c, d in json.loads(row[1]).items()}

    @staticmethod
    def _shares(coins, field):
        total = sum((c[field] for c in coins.values()), Decimal(0))
        if total <= 0:
            return {}, total
        return {k: v[field] / total for k, v in coins.items()}, total

    def analyze(self, *, top=5, min_volume=Decimal("1000000")):
        with self._lock:
            now_at = self._last_snapshot_time()
            current = self._snapshot(now_at)
            if not current:
                return None
            flows_at, flows = self.latest_flows()
            report = {"at_ms": now_at, "flows_at_ms": flows_at, "horizons": {}, "try": {}}
            for market, field, floor in (("usdt", "usdt_volume", min_volume), ("try", "try_volume", min_volume / 10)):
                shares, total = self._shares(current, field)
                for label, span in HORIZONS:
                    past_at = self._snapshot_near(now_at - span, max(span // 4, 20 * 60_000))
                    if past_at is None:
                        continue
                    past_shares, _ = self._shares(self._snapshot(past_at), field)
                    moves = []
                    for coin, share in shares.items():
                        if current[coin][field] < floor or coin not in past_shares:
                            continue
                        delta = share - past_shares[coin]
                        moves.append((coin, delta, share, current[coin]["change_24h"]))
                    moves.sort(key=lambda m: m[1], reverse=True)
                    target = report["horizons"] if market == "usdt" else report["try"]
                    target[label] = {"total_usdt": total, "gainers": moves[:top],
                                     "losers": list(reversed(moves[-top:])) if len(moves) > top else []}
            if flows:
                ranked = sorted(flows.items(), key=lambda kv: kv[1]["net_24h"], reverse=True)
                report["inflow"] = [(c, f) for c, f in ranked if f["net_24h"] > 0][:top]
                report["outflow"] = [(c, f) for c, f in reversed(ranked) if f["net_24h"] < 0][:top]
                report["net_total_24h"] = sum((f["net_24h"] for f in flows.values()), Decimal(0))
                report["net_total_7d"] = sum((f["net_7d"] for f in flows.values()), Decimal(0))
            return report

    def accumulation_ratio(self, coin):
        """7d net taker buy / gross for the early-long scanner; None when not tracked."""
        _, flows = self.latest_flows()
        entry = flows.get(coin)
        if not entry or entry["gross_7d"] <= 0:
            return None
        return entry["net_7d"] / entry["gross_7d"]
