"""Unified registered command hook for V5's existing consumer. Never sends."""
from .commands import REGISTRY, parse_command
from .control import queue_notice, receipt, record, snapshot
from .domain import TradingError, paper_only
from .telegram_control import handle_update
from .telegram_flow import render_flow
from decimal import Decimal


def render_decision(value):
    s = value["scoring"]
    m = value["market_summary"]
    lines = ["SPOT GRID | PAPER | " + value["data_status"],
             value["instrument"] + " | " + value["action"],
             "Snapshot " + value["snapshot_id"], "Karar " + value["decision_id"],
             "as-of ms " + str(value["as_of_ms"]) + " | profil " + value["profile"]["name"],
             f"Skor {s['score']}/100 | zorunlu filtre {s['passed']}/{s['required']}",
             "Ağırlıklar: " + ", ".join(k + "=" + str(v) for k, v in s["weights"].items()),
             "Ret: " + (", ".join(value["reasons"]) or "YOK")]
    lines.extend([f"Fiyat {m['price']} | bid/ask {m['bid']}/{m['ask']} | spread {m['spread_bps']} bps",
                  f"24h quote hacmi {m['quote_volume_24h_usdt']} USDT | book {m['book_levels']} | seq {m['sequence']}",
                  "15m/1H/4H oran: " + "/".join(m["trend"][k] for k in ("return_15m", "return_1h", "return_4h")),
                  "BTC 1H/4H oran: " + "/".join(m["trend"][k] for k in ("btc_return_1h", "btc_return_4h")),
                  f"±%0,5 derinlik bid/ask {m['depth_0_5pct']['bid']}/{m['depth_0_5pct']['ask']} USDT",
                  f"±%1 derinlik bid/ask {m['depth_1pct']['bid']}/{m['depth_1pct']['ask']} USDT"])
    plan = value["plan"]
    if plan:
        lines.extend([f"Alt {plan['lower']} | üst {plan['upper']} | {plan['grids']} aritmetik grid | {plan['capital']} USDT",
                      f"Fiyat TP {plan['take_profit']} / SL {plan['stop']} (toplam portföy PnL hedefi değildir)",
                      "Net/grid: " + ", ".join(format(Decimal(v), ".8%") for v in value["net_grid_returns"]),
                      f"Her yön fee {plan['fee_rate']} | kayma {plan['slippage_rate']}",
                      "Planlanan risk " + value["planned_loss_usdt"] + " USDT | bot limit " + value["risk_caps"]["per_bot"]])
    else:
        lines.append("Grid/net/risk: HESAPLANAMADI; eksik değerler sıfır değildir.")
    lines.extend(["Stop, gerçekleşen zararı 3 USDT ile garanti etmez; gap/likidite kaybı zararı artırabilir.",
                  "Kaynak https://tr.okx.com | anlık REST snapshot | gerçek emirler KAPALI"])
    return "\n".join(lines)


def handle_v7_update(core, update, *, allowed_chat, allowed_user, now):
    paper_only()
    update, _ = snapshot(update)
    # ACL/replay acceptance commits before any GET. Crash-after-accept requires
    # a new update instead of blindly repeating an uncertain action.
    accepted = handle_update(core.trader, update, allowed_chat=allowed_chat, allowed_user=allowed_user, now=now)
    uid, command = update["update_id"], parse_command(accepted["command"])
    key = "v7_command:" + str(uid)
    with core.trader._transaction():
        old = receipt(core.trader, key, "v7_command")
        if old:
            return {**old["result"], "duplicate": True}
        if accepted["duplicate"]:
            return {"ok": False, "duplicate": True, "reason": "COMMAND_OUTCOME_UNKNOWN_NEW_UPDATE_REQUIRED",
                    "message": "Komut sonucu belirsiz; yeni komut gerekli.", "delivery": "NOT_SENT", "live_enabled": False}
    try:
        if command.name in {"/analiz", "/grid", "/gridplan"}:
            if not command.args:
                raise TradingError("SYMBOL_REQUIRED")
            if not command.args[0].endswith("-USDT"):
                raise TradingError("UNSUPPORTED_GRID_QUOTE_USDT_REQUIRED")
            value = core.decision(command.args[0], command.args[1] if command.name == "/gridplan" else "300")
            message = render_decision(value)
        elif command.name == "/radar":
            value = core.radar()
            message = "RADAR A | SPOT GRID | " + value["action"] + "\nKapsam: bu batch; tüm piyasa eşzamanlı sıralaması değildir.\n" + "\n".join(
                r["instrument"] + " | " + r["action"] + " | " + ",".join(r["reasons"]) +
                " | snapshot " + r.get("snapshot_id", "HESAPLANAMADI") for r in value["decisions"])
        elif command.name in {"/paraakisi", "/momentum"}:
            value = core.momentum(target=command.args[0] if command.args else None)
            message = render_flow(value, detailed=bool(command.args), include_watch=True) + "\n" + value["data_status"] + " | yönlü işlem planı yok: NO TRADE"
        elif command.name == "/papergrid" and command.args[0] == "start":
            value = core.start(now=now)
            message = "PAPER motoru hazırlandı. Mevcut V5 scheduler bağlantısı: DOĞRULANMADI.\nGerçek emirler KAPALI."
        elif command.name in {"/durum", "/diagnostics", "/papergrid", "/emergency_stop", "/paper"}:
            value = core.status()
            p = value["paper"]["portfolio"]
            message = (f"PAPER | V5 servis/scheduler/Telegram: DOĞRULANMADI\nVeri: {value['market']['data_status']}\n"
                       f"Akış verisi: {value['flow']['data_status']} | {value['flow'].get('reason',value['flow'].get('rotation_status','N/A'))}\n"
                       f"Sermaye {p['initial_equity']} | rezerv {p['reserved_capital']} | boş {p['unallocated_cash']} USDT\n"
                       f"Bot {p['active_bots']}/2 | risk rezerv {p['reserved_planned_risk']} / 6 USDT\n"
                       f"Realize PnL {p['realized_pnl']} | açık PnL {p['unrealized_pnl']} | ücret {p['fees']} USDT\n"
                       f"Durdurma {value['paper']['halted']} | pause {value['paper']['paused']} | hazır {value['engine_armed']}\n"
                       "Stop zararı garanti etmez. LIVE_TRADING=false | gerçek emirler KAPALI.")
            if value['market'].get('reason'):
                message += "\nVeri gerekçesi: " + value['market']['reason']
            for error in value['market'].get('data_errors', []):
                message += "\n" + error['instrument'] + " | " + error['reason']
            for bot in value["paper"]["bots"][-4:]:
                slots = bot["orders"]["slots"]
                pending = sum(o["side"] != "CANCELLED" for o in slots)
                blocked = sorted({o["blocked_reason"] for o in slots if o.get("blocked_reason")})
                message += (f"\n{bot['instrument']} {bot['status']} | emir {pending} | miktar {bot['quantity']} | "
                            f"PnL {bot['total_pnl']} | ücret {bot['fees']} | risk {bot['planned_stop_loss']} USDT" +
                            (" | " + ",".join(blocked) if blocked else ""))
        else:
            raise TradingError("REGISTERED_COMMAND_HANDLER_MISSING")
        result = {"ok": True, "command": command.text, "value": value, "message": message,
                  "duplicate": False, "delivery": "NOT_SENT", "live_enabled": False}
    except TradingError as error:
        result = {"ok": False, "command": command.text, "reason": error.code,
                  "message": "NO TRADE | " + error.code + "\nGerçek emirler KAPALI.",
                  "duplicate": False, "delivery": "NOT_SENT", "live_enabled": False}
    if len(result["message"]) > 3500:
        result["message"] = "NO TRADE | TELEGRAM_REPORT_TOO_LARGE\nGerçek emirler KAPALI."
        result["ok"] = False
    with core.trader._transaction():
        queue_notice(core.trader, "v7u_" + str(uid), result["message"], now)
        record(core.trader, key, "v7_command", now, {"result": result})
    return result
