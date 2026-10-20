"""Private, append-only control receipts and notification outbox; no transport."""
from decimal import Decimal
import hashlib
import json

from .domain import TradingError, canonical, identifier, paper_only, timestamp


def snapshot(value):
    def decimal(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise TypeError
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=decimal)
        if len(raw) > 1024 * 1024:
            raise TradingError("CONTROL_INPUT_TOO_LARGE")
        return json.loads(raw), hashlib.sha256(raw.encode()).hexdigest()
    except (TypeError, ValueError, RecursionError):
        raise TradingError("INVALID_CONTROL_INPUT") from None


def receipt(trader, key, kind):
    row = trader._db.execute("SELECT kind,payload FROM events WHERE id=?", (key,)).fetchone()
    if row is None:
        return None
    if row[0] != kind:
        raise TradingError("CONTROL_RECEIPT_CONFLICT")
    try:
        value = json.loads(row[1])
        if not isinstance(value, dict):
            raise TradingError("CORRUPT_CONTROL_RECEIPT")
        return value
    except (TypeError, ValueError):
        raise TradingError("CORRUPT_CONTROL_RECEIPT") from None


def record(trader, key, kind, at, value):
    trader._event(key, None, kind, timestamp(at), value)


def pin(trader, key, value):
    _, digest = snapshot(value)
    saved = trader._meta(key)
    if saved is None:
        trader._set_meta(key, digest)
    elif saved != digest:
        raise TradingError("CONTROL_POLICY_MISMATCH")


def queue_notice(trader, notice_id, text, now):
    paper_only()
    identifier(notice_id)
    if not isinstance(text, str) or not 1 <= len(text) <= 3500:
        raise TradingError("INVALID_NOTICE")
    with trader._transaction():
        trader._audit_all()
        key = "notice:" + notice_id
        existing = receipt(trader, key, "paper_notice")
        value = {"text": text, "mode": "paper"}
        if existing:
            if existing != value:
                raise TradingError("NOTICE_ID_CONFLICT")
            return
        record(trader, key, "paper_notice", now, value)


def mark_notice(trader, notice_id, outcome, now):
    """Record a caller's simulated acknowledgement/unknown result, never send."""
    paper_only()
    identifier(notice_id)
    if outcome not in {"ACK", "UNKNOWN"}:
        raise TradingError("INVALID_NOTICE_OUTCOME")
    with trader._transaction():
        trader._audit_all()
        if receipt(trader, "notice:" + notice_id, "paper_notice") is None:
            raise TradingError("NOTICE_NOT_FOUND")
        if receipt(trader, "notice_ack:" + notice_id, "paper_notice_ack"):
            return
        key, kind = ("notice_ack:", "paper_notice_ack") if outcome == "ACK" else ("notice_unknown:", "paper_notice_unknown")
        if receipt(trader, key + notice_id, kind) is None:
            record(trader, key + notice_id, kind, now, {"outcome": outcome})


def notices(trader):
    with trader._transaction():
        trader._audit_all()
        result = []
        for key, raw in trader._db.execute("SELECT id,payload FROM events WHERE kind='paper_notice' ORDER BY rowid"):
            notice_id = key.removeprefix("notice:")
            value = json.loads(raw)
            if receipt(trader, "notice_ack:" + notice_id, "paper_notice_ack"):
                continue
            value.update(notice_id=notice_id, status="UNKNOWN" if receipt(trader, "notice_unknown:" + notice_id, "paper_notice_unknown") else "PENDING")
            result.append(value)
        return result
