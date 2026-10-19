"""Fail CI if runtime data, credential files, or suspicious secret assignments are tracked."""
import ast
import re
import subprocess
import sys
from pathlib import PurePosixPath

paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
bad = []
for raw in filter(None, paths):
    path = PurePosixPath(raw)
    parts = set(path.parts)
    name = path.name.lower()
    if parts & {"state", "data", "logs", "backups", "secrets", "private"}:
        bad.append(f"runtime/private path: {raw}")
    if (name == ".env" or name.startswith(".env.") and name != ".env.example"
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3", ".tar", ".zip", ".bak", ".log"))
            or name.startswith(("id_rsa", "id_ed25519"))):
        bad.append(f"prohibited filename: {raw}")
    if path.suffix != ".py":
        continue
    source = open(raw, encoding="utf-8").read()
    try:
        tree = ast.parse(source, filename=raw)
    except SyntaxError as exc:
        bad.append(f"invalid Python: {raw}:{exc.lineno}")
        continue
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)]
        if not any(re.search(r"(?i)(token|secret|password|api_?key|passphrase|credential)", n) for n in names):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and len(node.value.value) >= 8:
            bad.append(f"possible embedded secret: {raw}:{node.lineno}")
if bad:
    print("Repository safety checks failed:")
    for issue in bad:
        print("-", issue)
    sys.exit(1)
print(f"Repository safety checks passed ({len(list(filter(None, paths)))} tracked files).")
