"""Read-only host/source/TR evidence. No credentials, Telegram, orders or deploy.

Run on the authorized VPS from a reviewed staging checkout. Running on a local
machine/CI proves only that execution host, never the production VPS.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from okx_grid.domain import TradingError, paper_only
from okx_grid.profiles import load_profile
from okx_grid.public_contracts import TR_ORIGIN
from okx_grid.v7_market import V7MarketData
from tools.inspect_vps_source import inspect_source

UNITS = ("okx-telegram-commands.service", "okx-grid-v5.service", "okx-grid-v5.timer")
PROPERTIES = "LoadState,ActiveState,SubState,Type,Result,ExecMainStatus,MainPID,UnitFileState"
ENUMS = {
    "LoadState": {"loaded", "not-found", "error", "masked", "bad-setting", "stub"},
    "ActiveState": {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance"},
    "SubState": {"running", "dead", "exited", "waiting", "failed", "start", "stop", "auto-restart"},
    "Type": {"simple", "exec", "forking", "oneshot", "dbus", "notify", "notify-reload", "idle"},
    "Result": {"success", "exit-code", "signal", "core-dump", "timeout", "watchdog", "start-limit-hit", "resources", "protocol"},
    "UnitFileState": {"enabled", "enabled-runtime", "disabled", "static", "indirect", "masked", "masked-runtime", "generated", "transient", "alias"},
}


def unit_status(unit):
    if unit not in UNITS:
        raise ValueError("unit not allowed")
    try:
        result = subprocess.run(["systemctl", "show", unit, "--no-pager", "--property=" + PROPERTIES],
            capture_output=True, text=True, timeout=5, check=False,
            env={"PATH": os.defpath, "LANG": "C", "SYSTEMD_PAGER": "cat"})
        if result.returncode or len(result.stdout) > 8192:
            return {"observed": False, "reason": "SYSTEMD_STATUS_UNAVAILABLE"}
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        safe = {key: fields.get(key) if fields.get(key) in allowed else "UNKNOWN" for key, allowed in ENUMS.items()}
        status = fields.get("ExecMainStatus", "")
        safe["ExecMainStatus"] = int(status) if status.isascii() and status.isdigit() and len(status) <= 3 else None
        # Do not print command line, PID, executable path, environment or logs.
        safe["python_executable"] = "NOT_OBSERVED"
        pid = fields.get("MainPID", "")
        if pid.isascii() and pid.isdigit() and 0 < int(pid) < 2**31:
            try:
                name = Path(os.readlink("/proc/" + pid + "/exe")).name
                if re.fullmatch(r"python3(?:\.[0-9]{1,2})?", name):
                    safe["python_executable"] = name
            except OSError:
                pass
        # A timer-triggered oneshot can be inactive after successful completion.
        safe["completed_oneshot"] = (safe["Type"] == "oneshot" and safe["ActiveState"] == "inactive"
                                     and safe["Result"] == "success" and safe["ExecMainStatus"] == 0)
        return {"observed": True, **safe}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"observed": False, "reason": "SYSTEMD_STATUS_UNAVAILABLE"}


def readiness(source, *, allow_public_network=False):
    paper_only()
    if type(allow_public_network) is not bool:
        raise ValueError("explicit boolean required")
    report = {"format": "v5-readiness-v1", "host_scope": "EXECUTION_HOST_ONLY",
        "checked_at_ms": int(time.time() * 1000), "inspector_python": platform.python_version(),
        "activated": False, "production_ready": False, "telegram_e2e_verified": False,
        "LIVE_TRADING": False, "live_flag_scope": "INSPECTOR_PROCESS_ONLY",
        "production_live_trading": "NOT_INSPECTED", "positions_opened": 0,
        "runtime": {unit: unit_status(unit) for unit in UNITS}}
    try:
        first, _ = inspect_source(source)
        second, _ = inspect_source(source)
        report["source"] = {**second, "stable_between_reads": first == second,
                            "hash_scope": "FILES_ON_DISK_NOT_RUNNING_PROCESS"}
        if first != second:
            report["source"]["ready_for_review"] = False
    except (OSError, ValueError, subprocess.SubprocessError):
        report["source"] = {"ready_for_review": False, "reason": "SOURCE_INSPECTION_FAILED"}
    report["tr_public"] = {"requested": allow_public_network, "source_origin": TR_ORIGIN,
        "data_status": "DATA_UNVERIFIED", "reason": "PUBLIC_NETWORK_NOT_REQUESTED"}
    if allow_public_network:
        service = V7MarketData(load_profile("v7-paper-600").policy)
        try:
            capture = service.capture("BTC-USDT")
            if not service.real_transport or capture["data_status"] != "VERIFIED_TR_PUBLIC":
                raise TradingError("DATA_UNVERIFIED")
            report["tr_public"] = {"requested": True, **{key: capture[key] for key in
                ("source_origin", "data_status", "snapshot_id", "as_of_ms", "server_ts_ms", "book_ts_ms")},
                "instrument": "BTC-USDT", "bid_levels": capture["book"]["bid_levels"],
                "ask_levels": capture["book"]["ask_levels"], "scope": "ONE_REST_CAPTURE_ONLY"}
        except TradingError:
            report["tr_public"]["reason"] = "PUBLIC_TR_CAPTURE_FAILED"
            if service.client.last_response:
                # Client retains only enum/numeric facts, never headers/body.
                report["tr_public"]["request_diagnostics"] = service.client.last_response
    report["source_review_ready"] = report["source"]["ready_for_review"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--allow-public-network", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = readiness(args.source, allow_public_network=args.allow_public_network)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["source_review_ready"] and (not args.allow_public_network or
            result["tr_public"]["data_status"] == "VERIFIED_TR_PUBLIC") else 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print('{"ok":false,"error":"READINESS_CHECK_FAILED","production_ready":false,"activated":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
