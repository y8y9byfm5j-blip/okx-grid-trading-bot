"""Build a reviewed commit and stage it in isolation; never activate a service."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

try:
    from .check_repo_safety import scan_content
except ImportError:
    from check_repo_safety import scan_content

ROOT = Path(__file__).resolve().parents[1]
LIVE_PATH = Path("/opt/okx-grid-demo")
MANIFEST = "RELEASE_MANIFEST.json"
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
REQUIRED = {"okx_grid/__init__.py", "okx_grid/__main__.py", "okx_grid/domain.py", "okx_grid/paper.py",
            "okx_grid/radar.py", "okx_grid/automation.py", "okx_grid/control.py", "okx_grid/cycle.py", "okx_grid/telegram_control.py", "tools/run_tests.py", "tests/__init__.py", "okx_grid/profiles.py",
            "okx_grid/config/v5-paper-500.toml", "okx_grid/public_contracts.py", "okx_grid/public_http.py", "V5_COMPATIBILITY.md"}


def git(*args, root=ROOT):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)


def safe_path(raw):
    path = PurePosixPath(raw)
    return (not path.is_absolute() and bool(path.parts) and all(p not in {".", ".."} for p in path.parts)
            and str(path) == raw and "\\" not in raw and "\0" not in raw)


def permitted(raw):
    path = PurePosixPath(raw)
    return safe_path(raw) and (raw in {"README.md", ".gitignore", "V5_COMPATIBILITY.md"} or path.parts[0] in {"okx_grid", "tests", "tools", "docs"})


def build(commit, output, root=ROOT, validate=True):
    root, output = Path(root), Path(output).absolute()
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or git("rev-parse", "HEAD", root=root).decode().strip() != commit:
        raise ValueError("exact HEAD commit required")
    if git("status", "--porcelain", "--untracked-files=normal", root=root).strip():
        raise ValueError("clean checkout required")
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("new non-symlink output required")
    if validate:
        subprocess.run([sys.executable, "tools/validate.py"], cwd=root, check=True, timeout=60)
    files = {}
    for row in filter(None, git("ls-tree", "-rz", "--full-tree", commit, root=root).split(b"\0")):
        metadata, path = row.split(b"\t", 1)
        mode, kind, sha = metadata.decode().split()
        name = path.decode()
        if not permitted(name):
            if name.startswith(".github/"):
                continue
            raise ValueError("unreviewed release path")
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("links and submodules refused")
        content = git("cat-file", "blob", sha, root=root)
        if scan_content(name, content):
            raise ValueError("unsafe release file")
        files[name] = content
    if not REQUIRED <= files.keys() or sum(map(len, files.values())) > MAX_ARCHIVE_BYTES:
        raise ValueError("incomplete or oversized release")
    manifest = {"format": 1, "commit": commit, "mode": "paper", "activation": "disabled",
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    files[MANIFEST] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(prefix=".okx-release-", dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with tarfile.open(temporary_path, "w:gz") as archive:
            for name, content in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(content), 0o600, 0
                archive.addfile(info, io.BytesIO(content))
        os.link(temporary_path, output)  # Exclusive: never overwrite an existing release.
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"commit": commit, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "mode": "paper"}


def read_archive(archive_path, expected_hash):
    archive_path = Path(archive_path)
    if (archive_path.is_symlink() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ValueError("invalid archive")
    raw = archive_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("archive digest mismatch")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            if (not member.isfile() or not safe_path(member.name) or member.name in files
                    or not (member.name == MANIFEST or permitted(member.name)) or member.size < 0):
                raise ValueError("unsafe archive member")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("expanded archive too large")
            content = archive.extractfile(member).read()
            if member.name != MANIFEST and scan_content(member.name, content):
                raise ValueError("unsafe archive content")
            files[member.name] = content
    if MANIFEST not in files:
        raise ValueError("missing manifest")
    manifest = json.loads(files.pop(MANIFEST))
    if (manifest.get("format") != 1 or manifest.get("mode") != "paper" or manifest.get("activation") != "disabled"
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("commit", ""))
            or not REQUIRED <= files.keys()
            or manifest.get("files") != {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}):
        raise ValueError("manifest mismatch")
    return manifest, files


def stage(archive_path, expected_hash, staging_root):
    manifest, files = read_archive(archive_path, expected_hash)
    staging_root = Path(staging_root).absolute()
    if (staging_root == LIVE_PATH or LIVE_PATH in staging_root.parents or staging_root in LIVE_PATH.parents
            or any(p.is_symlink() for p in (staging_root, *staging_root.parents))):
        raise ValueError("protected live path or symlink refused")
    releases = staging_root / "releases"
    if releases.is_symlink():
        raise ValueError("symlink releases path refused")
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = releases / manifest["commit"]
    lock = staging_root / ".stage.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    temporary = None
    try:
        if destination.exists() or destination.is_symlink():
            raise ValueError("release already staged")
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=releases))
        for name, content in files.items():
            target = temporary / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(content)
            target.chmod(0o600)
        (temporary / MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (temporary / MANIFEST).chmod(0o600)
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TRADING_MODE": "paper",
                       "LIVE_TRADING": "false", "PYTHONDONTWRITEBYTECODE": "1"}
        for command in ([sys.executable, "tools/run_tests.py"], [sys.executable, "-m", "okx_grid", "health"],
                        [sys.executable, "-m", "okx_grid", "demo"]):
            subprocess.run(command, cwd=temporary, env=environment, check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        temporary.rename(destination)
        temporary = None
    finally:
        if temporary:
            shutil.rmtree(temporary)
        lock.unlink(missing_ok=True)
    return {"commit": manifest["commit"], "mode": "paper", "staged": True, "activated": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build")
    p.add_argument("--commit", required=True)
    p.add_argument("--output", required=True, type=Path)
    p = sub.add_parser("stage")
    p.add_argument("--archive", required=True, type=Path)
    p.add_argument("--sha256", required=True)
    p.add_argument("--root", type=Path, default=Path("/opt/okx-grid-staging"))
    args = parser.parse_args(argv)
    try:
        result = build(args.commit, args.output) if args.command == "build" else stage(args.archive, args.sha256, args.root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError, subprocess.SubprocessError):
        print('{"ok":false,"error":"RELEASE_VALIDATION_FAILED"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
