"""Explicit, paper-only CLI. Inputs are JSON snapshots; nothing is sent to a broker."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

from .domain import Market, RiskPolicy, TradingError, build_plan, paper_only
from .paper import PaperTrader


def read_json(path):
    try:
        source = Path(path)
        if source.stat().st_size > 1024 * 1024:
            raise TradingError("INPUT_TOO_LARGE")
        return json.loads(source.read_text(encoding="utf-8"), parse_float=Decimal)
    except TradingError:
        raise
    except (OSError, ValueError, RecursionError):
        raise TradingError("INVALID_INPUT_FILE") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline OKX grid paper core; live trading unavailable")
    parser.add_argument("--state", help="Private .sqlite3 path; required for persistent paper operations")
    parser.add_argument("--policy", help="Risk policy JSON; it must match the saved ledger policy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Run a synthetic buy/sell scenario entirely in memory")
    sub.add_parser("health", help="Verify the paper guard; audit the ledger if --state is supplied")
    p = sub.add_parser("radar", help="Screen supplied market/plan/trend proposals")
    p.add_argument("proposals")
    p = sub.add_parser("plan")
    p.add_argument("plan")
    p.add_argument("market")
    p = sub.add_parser("open")
    p.add_argument("bot_id")
    p.add_argument("plan")
    p.add_argument("market")
    p = sub.add_parser("tick")
    p.add_argument("bot_id")
    p.add_argument("tick_id")
    p.add_argument("price")
    p.add_argument("observed_at", type=int, help="UTC Unix seconds; checked against the local clock")
    p.add_argument("--close", action="store_true")
    sub.add_parser("status")
    sub.add_parser("halt")
    args = parser.parse_args(argv)
    try:
        paper_only()
        policy = RiskPolicy(**read_json(args.policy)) if args.policy else RiskPolicy()
        now = int(time.time())
        if args.command == "demo":
            from .scenarios import demo
            result = demo()
        elif args.command == "radar":
            from .radar import screen
            result = screen(read_json(args.proposals), policy, now)
        elif args.command == "plan":
            plan, market = build_plan(read_json(args.plan)), Market.from_dict(read_json(args.market))
            plan.check(policy, market, now)
            result = {"mode": "paper", "plan": plan.to_dict(), "digest": plan.digest,
                      "levels": [str(p) for p in plan.levels], "planned_stop_loss": str(plan.planned_loss)}
        elif args.command == "health" and not args.state:
            result = {"mode": "paper", "ok": True, "ledger_checked": False}
        else:
            if not args.state:
                raise TradingError("STATE_PATH_REQUIRED")
            with PaperTrader(args.state, policy) as trader:
                if args.command == "open":
                    result = trader.open(args.bot_id, build_plan(read_json(args.plan)),
                                         Market.from_dict(read_json(args.market)), now)
                elif args.command == "tick":
                    result = trader.tick(args.bot_id, args.tick_id, args.price, args.observed_at, now, args.close)
                elif args.command == "halt":
                    trader.halt()
                    result = trader.status()
                else:
                    result = trader.status()
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except TradingError as error:
        print(json.dumps({"ok": False, "error": error.code}), file=sys.stderr)
        return 2
    except (TypeError, KeyError):
        print(json.dumps({"ok": False, "error": "INVALID_INPUT"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
