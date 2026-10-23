"""Explicit, isolated observation setup for V5's existing single owner.

No environment loader, bot token, poll loop, sender, source import or service
activation. The reviewed host owns its existing API/sender/scheduler.
"""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import stat

from .control import pin, snapshot
from .domain import TradingError, paper_only
from .v5_owner_adapter import V5OwnerAdapter
from .v5_owner_bridge import V5OwnerBridge
from .v7_core import V7Core
from .v7_decision import GRID_FILTERS, PlanningPolicy
from .v7_market import V7MarketData
from .v7_paper import V7PaperTrader

SETUP_NAME = "okxtr-manual-observe-v1"


def manual_planning():
    # Explicit new setup, not a change to the old reference/default planning
    # or v5-paper-500. Fractions: .001 = .10% net per grid after costs.
    return PlanningPolicy(min_net_grid_return="0.001")


def _existing_setup(path, setup, allowed_chat, allowed_user, initial_offset):
    """Reject foreign ledgers before the writable trader constructor runs.

    DELETE journalling is used by our trader. A recovery sidecar is therefore
    ambiguous and needs operator reconciliation, never an automatic adoption.
    Immutable read-only mode cannot change permissions, headers or sidecars.
    """
    if any(p.exists() or p.is_symlink() for p in
           (Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        raise TradingError("OBSERVATION_LEDGER_RECOVERY_REQUIRED")
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        saved = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")}
        if saved.get("v5_manual_setup") != setup:
            raise TradingError("DEDICATED_OBSERVATION_LEDGER_REQUIRED")
        pins = {"v5_manual_setup_identity": setup, "v7_planning": setup["planning"],
                "v7_grid_filters": dict(GRID_FILTERS),
                "v7_execution_stage": {"revision": 1, "observation_only": True},
                "v5_bridge_policy": {"revision": 1, "chat": allowed_chat, "user": allowed_user}}
        if any(saved.get(key) != snapshot(value)[1] for key, value in pins.items()):
            raise TradingError("CONTROL_POLICY_MISMATCH")
        offset = saved.get("v5_owner_next_offset")
        if type(offset) is not int or initial_offset > offset:
            raise TradingError("UNRECONCILED_LEGACY_OFFSET")
    except TradingError:
        raise
    except (sqlite3.Error, ValueError, TypeError):
        raise TradingError("DEDICATED_OBSERVATION_LEDGER_REQUIRED") from None
    finally:
        if db is not None:
            db.close()


@dataclass
class ObservationRuntime:
    trader: V7PaperTrader
    core: V7Core
    bridge: V5OwnerBridge
    adapter: V5OwnerAdapter

    def close(self):
        self.trader.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def create_observer(*, ledger, allowed_chat, allowed_user, initial_offset, client=None, clock_ms=None):
    """Configure callbacks for later reviewed V5 binding; perform no GETs.

    The supplied ledger must be dedicated to this immutable observation setup.
    Injected clients remain DATA_UNVERIFIED under the normal source gate.
    """
    paper_only()
    if (type(allowed_chat) is not int or type(allowed_user) is not int or
            allowed_chat <= 0 or allowed_user <= 0):
        raise TradingError("TELEGRAM_ACL_REQUIRED")
    if type(initial_offset) is not int or initial_offset < 0:
        raise TradingError("TELEGRAM_OFFSET_REQUIRED")
    path = Path(ledger)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise TradingError("DEDICATED_OBSERVATION_LEDGER_REQUIRED")
    path = path.resolve()
    live = Path("/opt/okx-grid-demo")
    if live == path or live in path.parents or path.suffix != ".sqlite3":
        raise TradingError("DEDICATED_OBSERVATION_LEDGER_REQUIRED")
    if path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1):
        raise TradingError("DEDICATED_OBSERVATION_LEDGER_REQUIRED")
    setup = {"name": SETUP_NAME, "revision": 1, "planning": asdict(manual_planning())}
    if path.exists():
        _existing_setup(path, setup, allowed_chat, allowed_user, initial_offset)
    trader = V7PaperTrader(path)
    try:
        with trader._transaction():
            if trader._meta("v5_manual_setup") is None and (
                    trader.status()["bots"] or trader._meta("v7_started") or trader._meta("v7_planning") is not None):
                raise TradingError("DEDICATED_OBSERVATION_LEDGER_REQUIRED")
            # Store the non-secret setup and separately pin its full identity.
            pin(trader, "v5_manual_setup_identity", setup)
            trader._set_meta("v5_manual_setup", setup)
        market = V7MarketData(trader.policy, client=client, clock_ms=clock_ms)
        core = V7Core(trader, market, planning=manual_planning(), observation_only=True)
        bridge = V5OwnerBridge(core, allowed_chat=allowed_chat, allowed_user=allowed_user,
                               initial_offset=initial_offset)
        adapter = V5OwnerAdapter(bridge, clock=lambda: market._now() / 1000)
        return ObservationRuntime(trader, core, bridge, adapter)
    except Exception:
        trader.close()
        raise
