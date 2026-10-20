"""Read-only command hook for an existing consumer; no Telegram polling/sending.

V5 call signatures remain BLOCKED until actual source contracts are inspected.
"""
from decimal import Decimal
import json

from .automation import _paper_500_only
from .control import pin, receipt, record, snapshot
from .domain import TradingError, build_plan, paper_only, symbol
from .market_data import LABEL
from .telegram_control import handle_update


def render(result):
    if not result["ok"]:
        return "VERİ ALINAMADI - ANALİZ YOK\n" + result["reason"]
    value = result["analysis"]
    command = result["command"]
    if command.startswith("/analiz") or command == "/grid":
        report = value["analysis"] if command == "/grid" else value
        market, book = report["market"], report["book"]
        lines = [LABEL, "Kaynak: https://tr.okx.com | anlık REST snapshot", f"{market['instrument']} | as-of ms: {report['as_of_ms']}",
                 f"Fiyat {market['price']} | Bid {market['bid']} / Ask {market['ask']} | Spread {report['spread_bps']} bps",
                 f"24h quote hacmi {market['quote_volume_24h']} USDT",
                 f"15m {report['trend']['return_15m']} | 1H {report['trend']['return_1h']} | BTC 1H {report['trend']['btc_return_1h']} (oran)",
                 f"Book ts {report['book_ts_ms']} | yaş {report['book_age_ms']} ms | seq {report['sequence']}",
                 f"İstenen 400/400; dönen {book['bid_levels']}/{book['ask_levels']} seviye",
                 "Top5 bid/ask USDT: " + book['top5']['bids']['notional'] + " / " + book['top5']['asks']['notional']]
        for band in ("0.5pct", "1pct"):
            depth = book[band]
            lines.append(f"±{band}: bid {depth['bid']} / ask {depth['ask']} USDT | dengesizlik {depth['imbalance']} | kapsama {depth['bid_band_covered']}/{depth['ask_band_covered']}")
        for side in ("bids", "asks"):
            wall = book["observed_walls"][side][0]
            lines.append(f"Gözlenen {side} duvarı: {wall['price']} @ {wall['quantity_base']} baz ({wall['notional_usdt']} USDT)")
        lines.append("Filtre: " + (", ".join(report["filter_reasons"]) or "GEÇTİ"))
        if command == "/grid":
            plan = value["plan"]
            lines.extend([f"PAPER ÖNERİ; rezerv/emir YOK | Alt {plan['lower']} / Üst {plan['upper']} | {plan['grids']} aritmetik grid",
                          f"Sanal sermaye {plan['capital']} USDT | TP {plan['take_profit']} / SL {plan['stop']}",
                          f"Her yön fee {plan['fee_rate']} / slippage {plan['slippage_rate']} varsayımı",
                          f"Planlanan zarar {value['planned_loss_usdt']} | bot risk {value['bot_risk_cap_usdt']} / toplam {value['total_risk_cap_usdt']} USDT",
                          "Tahmini net grid getiri (oran): " + ", ".join(value["net_grid_returns"])])
        lines.extend(["Duvarlar iptal edilebilir; fiyat hedefi değildir. Derinlik dönen seviyelerle sınırlı.", "Gerçek para emirleri: KAPALI"])
        text = "\n".join(lines)
    elif command == "/radar":
        lines = [LABEL, "TR anlık REST snapshot | yapılandırılmış parite listesi"]
        lines.extend("KABUL " + r["market"]["instrument"] + " | spread " + r["spread_bps"] + " bps" for r in value["accepted"])
        lines.extend("RET " + r["instrument"] + " | " + ",".join(r["reasons"]) for r in value["rejected"])
        text = "\n".join(lines + ["Gerçek para emirleri: KAPALI"])
    else:
        text = "PAPER | bağlantı/defter durumu\n" + json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(text) > 3500:
        return "VERİ ALINAMADI - ANALİZ YOK\nTELEGRAM_REPORT_TOO_LARGE"
    return text


def handle_market_update(trader, service, update, *, allowed_chat, allowed_user, now, universe, grid_template=None):
    """Authenticate and persist acceptance BEFORE network; never retry an uncertain
    analysis on duplicate update. Return a message for V5's existing sender.
    """
    paper_only()
    _paper_500_only(trader)
    if service.policy != trader.policy:
        raise TradingError("MARKET_POLICY_MISMATCH")
    update, _ = snapshot(update)
    # Reuse the tested ACL, parser, freshness, cursor and replay boundary.
    accepted = handle_update(trader, update, allowed_chat=allowed_chat, allowed_user=allowed_user, now=now)
    uid, command = update["update_id"], accepted["command"]
    if not isinstance(universe, (list, tuple)) or not 1 <= len(universe) <= 10:
        raise TradingError("RADAR_UNIVERSE_REQUIRED_MAX_10")
    config, _ = snapshot({"universe": [symbol(s) for s in universe], "grid_template": grid_template})
    grid_template = config["grid_template"]
    with trader._transaction():
        pin(trader, "market_command_configuration", config)
        old = receipt(trader, "market_command:" + str(uid), "market_command")
        if old:
            return {**old["result"], "duplicate": True}
        if accepted["duplicate"]:
            return {"ok": False, "duplicate": True, "reason": "ANALYSIS_OUTCOME_UNKNOWN_NEW_UPDATE_REQUIRED",
                    "message": "VERİ ALINAMADI - ANALİZ YOK\nÖnceki analiz sonucu belirsiz; yeni komut gerekli.", "live_enabled": False, "delivery": "NOT_SENT"}
    try:
        if command.startswith("/analiz "):
            instrument = command.split()[1]
            if instrument not in config["universe"]:
                raise TradingError("SYMBOL_NOT_ALLOWED")
            analysis = service.analyze(instrument)
        elif command == "/radar":
            analysis = service.radar(config["universe"])
            if not analysis["accepted"]:
                raise TradingError("NO_ELIGIBLE_MARKETS")
        elif command == "/grid":
            if grid_template is None:
                raise TradingError("REVIEWED_GRID_TEMPLATE_REQUIRED")
            plan = build_plan(grid_template)
            if plan.instrument not in config["universe"]:
                raise TradingError("SYMBOL_NOT_ALLOWED")
            with trader._transaction():
                state = trader.status()
                if state["halted"] or state["paused"]:
                    raise TradingError("PAPER_ENTRY_DISABLED")
                if state["portfolio"]["active_bots"] >= trader.policy.max_bots:
                    raise TradingError("MAX_BOTS")
                if Decimal(state["portfolio"]["unallocated_cash"]) < plan.capital:
                    raise TradingError("ALLOCATION_LIMIT")
                if sum((Decimal(b["planned_stop_loss"]) for b in state["bots"] if b["status"] == "OPEN"), Decimal(0)) + plan.planned_loss > trader.policy.equity * trader.policy.total_risk_fraction:
                    raise TradingError("TOTAL_RISK_LIMIT")
            analysis = service.grid(grid_template)
            analysis["portfolio_snapshot"] = state["portfolio"]
        elif command == "/durum":
            health = dict(service.last_health)
            if health.get("ok") and service._now() - health.get("checked_at_ms", 0) > trader.policy.max_market_age * 1000:
                health.update(ok=False, reason="STALE_CONNECTION_HEALTH")
            analysis = {"market_connection": health, "paper": trader.status(), "v5_integration": "NOT_VERIFIED"}
        else:
            analysis = {"paper": trader.status(), "market_query": "NOT_REQUESTED"}
        result = {"ok": True, "command": command, "analysis": analysis, "duplicate": False, "live_enabled": False, "delivery": "NOT_SENT"}
    except TradingError as error:
        result = {"ok": False, "command": command, "reason": error.code, "duplicate": False, "live_enabled": False, "delivery": "NOT_SENT"}
    result["message"] = render(result)
    with trader._transaction():
        record(trader, "market_command:" + str(uid), "market_command", now, {"result": result})
    return result
