"""Deterministic blast radius: who outside the PR references the symbols it changed. No LLM involved."""

from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .gitgh import run

MAX_SYMBOLS = 60
MAX_REFS_PER_SYMBOL = 200  # beyond this a name is too common to trace by text search
MIN_SYMBOL_LEN = 4
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMMON = {
    "self", "this", "init", "main", "test", "name", "value", "data", "type", "error", "result", "string", "list",
    "dict", "true", "false", "none", "null", "config", "options", "params", "args", "kwargs", "ctx", "context",
    "request", "response", "handler", "client", "server", "file", "path", "index", "default", "get", "set", "run",
    "new", "close", "open", "start", "stop", "update", "create", "delete", "read", "write", "load", "save",
}


def _bare(symbol: str) -> str | None:
    """`pkg.Foo.bar()` → `bar`; returns None for names too short/common to trace."""
    idents = _IDENT.findall(symbol)
    if not idents:
        return None
    name = idents[-1]
    if len(name) < MIN_SYMBOL_LEN or name.lower() in _COMMON:
        return None
    return name


def _git_grep(root: Path, head_sha: str, name: str) -> list[dict]:
    out = run(["git", "-c", "core.quotePath=false", "grep", "-n", "-I", "-w", "-F", "--no-color", "-e", name, head_sha, "--"],
              cwd=root, check=False, timeout=60)
    refs = []
    for line in out.splitlines():
        # <sha>:<path>:<line>:<text>
        parts = line.split(":", 3)
        if len(parts) == 4 and parts[2].isdigit():
            refs.append({"path": parts[1], "line": int(parts[2]), "text": parts[3].strip()[:200]})
        if len(refs) > MAX_REFS_PER_SYMBOL:
            break
    return refs


def _codegraph_callers(root: Path, name: str) -> list[dict] | None:
    out = run(["codegraph", "callers", "--json", "-l", "50", "-p", str(root), name], cwd=root, check=False, timeout=60)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return [{"path": c["filePath"], "line": c.get("startLine"), "text": f"{c.get('kind', '')} {c.get('name', '')}".strip()}
            for c in data.get("callers", []) if c.get("filePath")]


def compute(root: Path, head_sha: str, changed: set[str], symbols_by_file: dict[str, list[str]]) -> dict:
    """{"engine", "symbols": [{symbol, defined_in, outside_refs: [...], count, too_common}]}"""
    use_codegraph = (root / ".codegraph").is_dir() and shutil.which("codegraph") is not None
    wanted: dict[str, str] = {}
    for path, syms in symbols_by_file.items():
        for s in syms:
            name = _bare(s)
            if name and name not in wanted:
                wanted[name] = path
            if len(wanted) >= MAX_SYMBOLS:
                break

    def lookup(item: tuple[str, str]) -> dict:
        name, defined_in = item
        refs = _codegraph_callers(root, name) if use_codegraph else None
        if refs is None:
            refs = _git_grep(root, head_sha, name)
        too_common = len(refs) > MAX_REFS_PER_SYMBOL
        outside = [r for r in refs if r["path"] not in changed]
        return {"symbol": name, "defined_in": defined_in, "outside_refs": outside[:25], "count": len(outside),
                "too_common": too_common}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lookup, wanted.items()))
    results.sort(key=lambda r: (r["too_common"], -r["count"]))
    return {"engine": "codegraph" if use_codegraph else "git grep", "symbols": results}
