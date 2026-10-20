"""Read only named legacy sources. Never execute them or inspect runtime files.

Optional export is local only, fails on any safety finding, and never invokes
Git or makes a network request. Review the exported source manually as well.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

try:
    from .check_repo_safety import scan_content
except ImportError:
    from check_repo_safety import scan_content

KNOWN = ("grid_plan_engine.py", "paper_trader_v5.py", "accounting_v52.py", "grid_reservation_v55.py",
         "radar_v5_base.py", "telegram_commands_v5.py", "telegram_notifier_v5_base.py", "telegram_plan_v5.py", "run_v5.sh")


def inspect_source(source):
    source = Path(source).absolute()
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError("symlink source refused")
    report, blobs = {"files": [], "missing": [], "issues": []}, {}
    directory = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in KNOWN:
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                report["missing"].append(name)
                continue
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 1024 * 1024:
                    raise ValueError("non-regular, linked or oversized source refused")
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    blob = handle.read(1024 * 1024 + 1)
                if len(blob) > 1024 * 1024:
                    raise ValueError("oversized source refused")
            finally:
                os.close(descriptor)
            findings = scan_content(name, blob)
            if name.endswith(".py") and not findings:
                ast.parse(blob.decode("utf-8"), filename=name)
            if name.endswith(".sh"):
                check = subprocess.run(["bash", "-n"], input=blob, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10)
                if check.returncode:
                    findings.append("invalid shell syntax")
            report["files"].append({"path": name, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
            report["issues"].extend({"path": name, "finding": item} for item in findings)
            blobs[name] = blob
    finally:
        os.close(directory)
    report["ready_for_review"] = not report["issues"] and not report["missing"]
    return report, blobs


def export_source(destination, report, blobs):
    if not report["ready_for_review"]:
        raise ValueError("incomplete or flagged source refused")
    destination = Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError("existing or symlink destination refused")
    # Parent must already exist; no live directory is created or modified.
    with tempfile.TemporaryDirectory(prefix=".okx-source-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        for name, blob in blobs.items():
            if name not in KNOWN:
                raise ValueError("unknown source refused")
            (temporary / name).write_bytes(blob)
            (temporary / name).chmod(0o600)
        (temporary / "SOURCE_REVIEW.txt").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (temporary / "SOURCE_REVIEW.txt").chmod(0o600)
        # A unique destination is mandatory; no existing export can be overwritten.
        destination.mkdir(mode=0o700)
        for file in temporary.iterdir():
            file.rename(destination / file.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--export", type=Path)
    args = parser.parse_args(argv)
    try:
        report, blobs = inspect_source(args.source)
        if args.export:
            export_source(args.export, report, blobs)
        print(json.dumps(report, indent=2))
        return 0 if report["ready_for_review"] else 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print('{"error":"SOURCE_INSPECTION_FAILED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
