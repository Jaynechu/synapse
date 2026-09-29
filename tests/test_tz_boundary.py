"""Guard: local wall-clock time only comes from synapse_core.timeutil.

Any naive `datetime.now()`, `date.today()`, `ZoneInfo(...)`, `.astimezone(...)`
or `utcnow()` in a synapse package bypasses `[core].timezone`. Strings (e.g.
scripts run inside marrow's interpreter) are not code here and are ignored.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("synapse_core", "synapse_tg", "synapse_wx")
ALLOWED = {"synapse_core/timeutil.py"}


def _attr_chain(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def violations(source: str) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _attr_chain(node.func)
        last = name.rsplit(".", 1)[-1]
        owner = name.rsplit(".", 2)[-2] if "." in name else ""
        bare = not node.args and not node.keywords
        if last == "now" and owner == "datetime" and bare:
            found.append((node.lineno, "naive datetime.now()"))
        elif last == "today" and owner in ("date", "datetime"):
            found.append((node.lineno, f"{owner}.today()"))
        elif last == "ZoneInfo":
            found.append((node.lineno, "ZoneInfo()"))
        elif last == "astimezone" and isinstance(node.func, ast.Attribute):
            found.append((node.lineno, ".astimezone()"))
        elif last == "utcnow":
            found.append((node.lineno, "utcnow()"))
    return found


def _sources():
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel not in ALLOWED:
                yield rel, path.read_text(encoding="utf-8")


def test_no_local_clock_outside_timeutil():
    hits = [f"{rel}:{line} {what}"
            for rel, src in _sources() for line, what in violations(src)]
    assert hits == [], "route through synapse_core.timeutil:\n" + "\n".join(hits)


@pytest.mark.parametrize("snippet", [
    "datetime.now()",
    "datetime.datetime.now()",
    "date.today()",
    "datetime.today()",
    "ZoneInfo('UTC')",
    "zoneinfo.ZoneInfo('UTC')",
    "dt.astimezone()",
    "dt.astimezone(UTC)",
    "datetime.utcnow()",
])
def test_guard_flags(snippet):
    assert violations(snippet)


@pytest.mark.parametrize("snippet", [
    "datetime.now(timezone.utc)",
    "datetime.now(UTC)",
    "datetime.now(tz=UTC)",
    "local_now()",
    "'datetime.now().astimezone()'",
])
def test_guard_allows(snippet):
    assert violations(snippet) == []
