"""Scan staged Git blobs by default; never print matching secret values.

This is a deterministic guard, not a substitute for reviewing source before
import. Only synthetic JSON fixtures under tests/fixtures may be committed.
"""
import argparse
import ast
from pathlib import Path, PurePosixPath
import re
import subprocess


SENSITIVE_NAME = re.compile(r"(?i)(?:token|secret|password|api_?key|passphrase|credential)")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)
TEXT_ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*[\"']?([\w.-]*(?:token|secret|password|api_?key|passphrase|credential)[\w.-]*)"
    r"[\"']?\s*[:=]\s*[\"']?([^\s\"'#,;]{8,})")
PRIVATE_PARTS = {"state", "data", "logs", "backups", "secrets", "private", "runtime", ".ssh", ".git"}
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3", ".tar", ".tar.gz",
                    ".tgz", ".zip", ".bak", ".backup", ".log", ".csv", ".tsv", ".jsonl", ".ndjson")


def placeholder(value):
    return not value or value.startswith(("<", "${", "$", "os.environ", "os.getenv")) or value in {"REDACTED", "CHANGE_ME"}


def scan_content(raw_path, content):
    path = PurePosixPath(raw_path)
    name = path.name.lower()
    issues = []
    if any(p.lower() in PRIVATE_PARTS for p in path.parts):
        issues.append("runtime/private path")
    if (name == ".env" or name.startswith(".env.") and name != ".env.example"
            or name.endswith(PRIVATE_SUFFIXES) or name.startswith(("id_rsa", "id_ed25519"))
            or ".pre_" in name or "_backup" in name or "_broken" in name):
        issues.append("prohibited filename")
    if path.suffix.lower() == ".json" and not raw_path.startswith("tests/fixtures/"):
        issues.append("JSON outside synthetic fixtures")
    try:
        source = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeDecodeError:
        return issues + ["unreviewed binary file"]
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(source):
            issues.append("credential pattern at line " + str(source.count("\n", 0, match.start()) + 1))
    for match in TEXT_ASSIGNMENT.finditer(source):
        if not placeholder(match[2]):
            issues.append("possible credential assignment at line " + str(source.count("\n", 0, match.start()) + 1))
    if path.suffix == ".py":
        try:
            tree = ast.parse(source, filename=raw_path)
        except SyntaxError as error:
            return issues + ["invalid Python at line " + str(error.lineno)]
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [getattr(n, "id", getattr(n, "attr", "")) for target in targets for n in ast.walk(target)]
                value = node.value
                if (any(SENSITIVE_NAME.search(n) for n in names)
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and len(value.value) >= 8 and not placeholder(value.value)):
                    issues.append("possible embedded credential at line " + str(node.lineno))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and isinstance(key.value, str) and SENSITIVE_NAME.search(key.value)
                            and isinstance(value, ast.Constant) and isinstance(value.value, str)
                            and len(value.value) >= 8 and not placeholder(value.value)):
                        issues.append("possible credential in mapping at line " + str(node.lineno))
    return sorted(set(issues))


def scan_repository(root, worktree=False):
    root = Path(root)
    rows = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root).split(b"\0")
    issues, count = [], 0
    for row in filter(None, rows):
        meta, raw_path = row.split(b"\t", 1)
        mode, sha, stage = meta.decode().split()
        path = raw_path.decode()
        count += 1
        if mode not in {"100644", "100755"} or stage != "0":
            issues.append((path, "symlink, submodule or unresolved merge"))
            continue
        if worktree:
            local = root / path
            if local.is_symlink() or any(p.is_symlink() for p in local.parents if p != root.parent):
                issues.append((path, "symlink path"))
                continue
            if not local.is_file():
                issues.append((path, "missing tracked file"))
                continue
            content = local.read_bytes()
        else:
            content = subprocess.check_output(["git", "cat-file", "blob", sha], cwd=root)
        issues.extend((path, issue) for issue in scan_content(path, content))
    if count == 0:
        issues.append(("repository", "no tracked files"))
    return count, issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", action="store_true")
    args = parser.parse_args(argv)
    try:
        count, issues = scan_repository(Path(__file__).resolve().parents[1], args.worktree)
    except (OSError, subprocess.CalledProcessError, ValueError):
        print("Repository safety scan could not complete.")
        return 1
    if issues:
        for path, issue in issues:
            print(f"{path}: {issue}")
        return 1
    print(f"Repository safety checks passed ({count} tracked files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
