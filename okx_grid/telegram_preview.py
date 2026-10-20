"""Deterministic, offline Telegram *message previews* for paper events.

No token, chat ID, bot client, HTTP transport or polling. Only reviewed
structured engine outputs are accepted. In particular, this is not the V5
Telegram command implementation and must not be presented as one.
"""
from decimal import Decimal

from .domain import TradingError, build_plan, identifier, number, symbol


def _amount(value):
    result = number(value)
    return format(result, "f")


def _portfolio(payload):
    if not isinstance(payload, dict):
        raise TradingError("INVALID_PAPER_NOTIFICATION")
    summary = payload.get("portfolio")
    if not isinstance(summary, dict) or summary.get("initial_equity") != "500":
        raise TradingError("INVALID_PAPER_NOTIFICATION")
    return summary


def render_paper_notification(payload):
    """Render PAPER-only status text, or None for non-notifiable replays.

    The caller may forward it through the existing single V5 Telegram owner
    only *after* checking V5 authorization, formatting and routing contracts.
    """
    if not isinstance(payload, dict) or payload.get("mode") != "paper":
        raise TradingError("INVALID_PAPER_NOTIFICATION")

    portfolio = _portfolio(payload)
    if payload.get("decision") in {"DUPLICATE_CYCLE", "NO_ELIGIBLE_CANDIDATE", "BLOCKED"}:
        return None
    if payload.get("decision") == "OPENED":
        identifier(payload["bot_id"])
        instrument = symbol(payload["instrument"])
        plan = build_plan(payload["plan"])
        if plan.instrument != instrument:
            raise TradingError("INVALID_PAPER_NOTIFICATION")
        return (
            "🧪 PAPER — OTOMATİK GRID AÇILDI\n"
            f"Parite: {instrument}\n"
            f"Alt / üst: {_amount(plan.lower)} / {_amount(plan.upper)} USDT\n"
            f"Grid: {plan.grids} (aritmetik)\n"
            f"Sermaye: {_amount(plan.capital)} USDT\n"
            f"TP / SL: {_amount(plan.take_profit)} / {_amount(plan.stop)} USDT\n"
            f"Planlanan risk: {_amount(plan.planned_loss)} USDT (garanti değil)\n"
            f"Ayrılmış toplam: {_amount(portfolio['reserved_capital'])} USDT\n"
            "İşlemler sanaldır; OKX hesabına emir gönderilmedi."
        )

    if payload.get("duplicate") is True or payload.get("event") == "REPLAY":
        return None
    if payload.get("event") not in {"UPDATED", "CLOSED"}:
        raise TradingError("INVALID_PAPER_NOTIFICATION")
    identifier(payload["bot_id"])
    instrument = symbol(payload["instrument"])
    status = payload["position_status"]
    reason = payload["close_reason"]
    if (payload["event"] == "CLOSED" and (status != "CLOSED" or reason not in
            {"HALTED", "MANUAL", "STOP_LOSS", "TAKE_PROFIT"})
            or payload["event"] == "UPDATED" and (status != "OPEN" or reason is not None)):
        raise TradingError("INVALID_PAPER_NOTIFICATION")
    title = "SANAL BOT KAPANDI" if status == "CLOSED" else "SANAL BOT GÜNCELLENDİ"
    lines = [f"🧪 PAPER — {title}", f"Parite: {instrument}",
             f"Nakit: {_amount(payload['cash'])} USDT",
             f"Envanter: {_amount(payload['quantity'])}",
             f"Komisyon: {_amount(payload['fees'])} USDT",
             f"Gerçekleşen PnL: {_amount(payload['realized_pnl'])} USDT",
             f"Gerçekleşmemiş PnL: {_amount(payload['unrealized_pnl'])} USDT",
             f"Portföy değeri: {_amount(portfolio['equity'])} USDT"]
    if status == "CLOSED":
        lines.append(f"Kapanış nedeni: {reason}")
    if payload["halted"] is True:
        lines.append("UYARI: PAPER HALT DEVREDE")
    lines.append("İşlemler sanaldır; OKX hesabına emir gönderilmedi.")
    return "\n".join(lines)
