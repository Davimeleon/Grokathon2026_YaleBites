"""Definition-of-done checks on the source tree itself (integrator-owned)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config import REPO_ROOT

PY_DIRS = ("app", "tests", "scripts")
WEB_SRC = REPO_ROOT / "web" / "src"
# Built from pieces so this file does not match itself.
MARKERS = re.compile(r"\b(" + "TO" + "DO|FIX" + "ME|X" + "XX|NotImplemented" + "Error)\b")


def _source_files() -> list[Path]:
    files: list[Path] = []
    for folder in PY_DIRS:
        files += sorted((REPO_ROOT / folder).rglob("*.py"))
    if WEB_SRC.exists():
        files += sorted(p for p in WEB_SRC.rglob("*") if p.suffix in {".ts", ".css", ".html"})
    files += [p for p in (REPO_ROOT / "web" / "index.html",) if p.exists()]
    return [f for f in files if "node_modules" not in f.parts and "__pycache__" not in f.parts]


def test_no_unfinished_markers():
    offenders = []
    for path in _source_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if MARKERS.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "unfinished-work markers found:\n" + "\n".join(offenders)


def _is_stub(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant)
                                       and isinstance(n.value.value, str))]
    if not body:
        return True
    if len(body) == 1:
        node = body[0]
        if isinstance(node, ast.Pass):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis:
            return True
    return False


def test_no_stub_functions():
    """No function whose body is only ``pass``/``...``/a docstring (Protocol members excepted)."""
    offenders = []
    for folder in ("app", "scripts"):
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            protocol_classes = {
                node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                and any(getattr(b, "id", getattr(b, "attr", "")) == "Protocol" for b in node.bases)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in protocol_classes:
                    continue
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and _is_stub(child):
                        if isinstance(node, ast.ClassDef) and node.name in protocol_classes:
                            continue
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{child.lineno} {child.name}")
    assert not offenders, "stub functions:\n" + "\n".join(offenders)


COMMENTED_CODE = re.compile(
    r"^\s*#\s*(def |class |import |from \S+ import |return\b|if .+:\s*$|for .+:\s*$|while .+:\s*$|"
    r"[a-z_][a-z0-9_.]*\s*=\s*[^=]|[a-z_][a-z0-9_.]*\(.*\)\s*$|await |print\()"
)


def test_no_commented_out_code():
    offenders = []
    for folder in PY_DIRS:
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if COMMENTED_CODE.match(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "commented-out code:\n" + "\n".join(offenders)


def test_load_settings_uses_given_environ_only(monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("YALE_BITES_SCHEDULER", "1")
    assert load_settings({"YALE_BITES_SCHEDULER": "0", "YALE_BITES_ONLINE_GEOCODING": "0"}).scheduler_enabled is False
    assert load_settings({}).online_geocoding is True
