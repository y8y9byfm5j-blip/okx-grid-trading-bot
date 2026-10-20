"""Deterministic supplied-book PAPER replay, no second wallet or live orders."""
from decimal import Decimal
from .domain import TradingError, number, positive, precise
from .flow_contracts import book_snapshot
from .money_flow import execution_estimate
from .public_contracts import _one


@precise
def replay_roundtrip(entry, exit_book, *, instrument, entry_ms, exit_ms, notional='90', fee='0.001'):
    """Replay one synthetic/historical USDT position against both visible books.

    Evaluates execution net fees/observed depth, not predictive strategy quality.
    Caller must separate training/selection from subsequent exit observations.
    """
    if exit_ms<=entry_ms or not instrument.endswith('-USDT'):
        raise TradingError('INVALID_REPLAY_WINDOW_OR_QUOTE')
    notional,fee=positive(notional),number(fee)
    book_snapshot(entry,instrument,entry_ms)
    book_snapshot(exit_book,instrument,exit_ms)
    estimate=execution_estimate(entry,'USDT',None,entry_ms,notional=notional,fee=fee)
    quantity=number(estimate['base_quantity'])
    remaining=quantity
    proceeds=Decimal(0)
    for row in _one(exit_book)['bids']:
        price,size=positive(row[0]),positive(row[1])
        sold=min(remaining,size)
        proceeds+=sold*price
        remaining-=sold
        if not remaining:
            break
    if remaining>0:
        raise TradingError('REPLAY_EXIT_DEPTH_INCOMPLETE')
    return {'mode':'paper','live_enabled':False,'notional_usdt':str(notional),'base_quantity':str(quantity),
            'entry_ms':entry_ms,'exit_ms':exit_ms,'net_pnl_usdt':str(proceeds*(1-fee)-notional),
            'fee_each_side':str(fee),'entry_slippage':estimate['buy_slippage'],
            'meaning':'SUPPLIED_BOOK_EXECUTION_REPLAY_NOT_PROVEN_ALPHA'}
