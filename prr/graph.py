"""Regex-level import graph among the PR's changed files, and a dependency-first ordering.

Good enough to order a review and group files for annotation; it is not a compiler.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from pathlib import Path

MAX_READ_BYTES = 400_000

_PY_FROM = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\s+([\w*, ()]+)", re.M)
_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", re.M)
_JS = re.compile(r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]|import\s*\(\s*['"]([^'"]+)['"]\s*\)|require\(\s*['"]([^'"]+)['"]\s*\)|^\s*import\s+['"]([^'"]+)['"]""", re.M)
_GO_BLOCK = re.compile(r"^import\s*\(([^)]*)\)", re.M | re.S)
_GO_SINGLE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_GO_SPEC = re.compile(r'"([^"]+)"')
_JAVA = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)
_RUST = re.compile(r"^\s*(?:pub\s+)?use\s+(?:crate|super|self)?::?([\w:]+)", re.M)
_INCLUDE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)
_RUBY = re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""", re.M)

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


def _strip_ext(p: str) -> str:
    root, _ = posixpath.splitext(p)
    return root


class _Index:
    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.by_noext: dict[str, list[str]] = defaultdict(list)
        self.by_dir: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            noext = _strip_ext(p)
            self.by_dir[posixpath.dirname(p)].append(p)
            parts = noext.split("/")
            if parts[-1] in ("index", "__init__", "mod"):
                parts = parts[:-1]
            for i in range(len(parts)):
                self.by_noext["/".join(parts[i:])].append(p)

    def suffix(self, key: str) -> list[str]:
        return self.by_noext.get(key.strip("/"), []) if key.strip("/") else []

    def relative(self, from_path: str, spec: str) -> list[str]:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
        if target in self.paths:
            return [target]
        hits = [target + ext for ext in _JS_EXTS if target + ext in self.paths]
        hits += [f"{target}/index{ext}" for ext in _JS_EXTS if f"{target}/index{ext}" in self.paths]
        if not hits and _strip_ext(target) != target:
            hits = [p for p in self.by_noext.get(_strip_ext(target), [])]
        return hits

    def go_package(self, import_path: str) -> list[str]:
        hits: list[str] = []
        for d, files in self.by_dir.items():
            if d and (import_path == d or import_path.endswith("/" + d)):
                hits += [f for f in files if f.endswith(".go") and not f.endswith("_test.go")]
        return hits


def _imports(path: str, text: str, idx: _Index) -> set[str]:
    ext = posixpath.splitext(path)[1].lower()
    found: list[str] = []
    if ext == ".py":
        for mod, names in _PY_FROM.findall(text):
            dots = len(mod) - len(mod.lstrip("."))
            base = mod.lstrip(".").replace(".", "/")
            if dots:
                anchor = posixpath.dirname(path)
                for _ in range(dots - 1):
                    anchor = posixpath.dirname(anchor)
                base = posixpath.join(anchor, base) if base else anchor
            found += idx.suffix(base)
            for name in re.split(r"[,\s()]+", names):
                if name and name != "*":
                    found += idx.suffix(posixpath.join(base, name))
        for group in _PY_IMPORT.findall(text):
            for mod in group.split(","):
                found += idx.suffix(mod.strip().replace(".", "/"))
    elif ext in _JS_EXTS:
        for groups in _JS.findall(text):
            spec = next(g for g in groups if g)
            if spec.startswith("."):
                found += idx.relative(path, spec)
            else:
                found += idx.suffix(re.sub(r"^(@/|~/|#/|src/)", "", spec))
    elif ext == ".go":
        specs = _GO_SINGLE.findall(text)
        for block in _GO_BLOCK.findall(text):
            specs += _GO_SPEC.findall(block)
        for spec in specs:
            found += idx.go_package(spec)
    elif ext in (".java", ".kt", ".scala"):
        for spec in _JAVA.findall(text):
            found += idx.suffix(spec.replace(".", "/"))
    elif ext == ".rs":
        for spec in _RUST.findall(text):
            parts = [p for p in spec.split("::") if p]
            for i in range(len(parts), 0, -1):
                hits = idx.suffix("/".join(parts[:i]))
                if hits:
                    found += hits
                    break
    elif ext in (".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm"):
        for spec in _INCLUDE.findall(text):
            found += idx.relative(path, spec) or idx.suffix(_strip_ext(spec))
    elif ext == ".rb":
        for spec in _RUBY.findall(text):
            found += idx.relative(path, spec + ".rb") if spec.startswith(".") else idx.suffix(spec)
    return {f for f in found if f != path}


def import_edges(paths: list[str], worktree: Path) -> dict[str, list[str]]:
    """path → sorted list of other changed paths it imports (read from the PR head)."""
    idx = _Index(paths)
    edges: dict[str, list[str]] = {}
    for p in paths:
        fp = worktree / p
        try:
            if not fp.is_file() or fp.stat().st_size > MAX_READ_BYTES:
                continue
            text = fp.read_text(errors="replace")
        except OSError:
            continue
        deps = _imports(p, text, idx)
        if deps:
            edges[p] = sorted(deps)
    return edges


def dependency_order(paths: list[str], edges: dict[str, list[str]]) -> list[str]:
    """Dependencies before dependents (Kahn's algorithm); cycles are broken alphabetically."""
    deps = {p: set(edges.get(p, [])) & set(paths) for p in paths}
    dependents: dict[str, set[str]] = defaultdict(set)
    for p, ds in deps.items():
        for d in ds:
            dependents[d].add(p)
    remaining = {p: len(ds) for p, ds in deps.items()}
    order: list[str] = []
    ready = sorted(p for p, n in remaining.items() if n == 0)
    while remaining:
        if not ready:
            ready = [min(remaining, key=lambda p: (remaining[p], p))]
        p = ready.pop(0)
        if p not in remaining:
            continue
        del remaining[p]
        order.append(p)
        newly = []
        for q in dependents[p]:
            if q in remaining:
                remaining[q] -= 1
                if remaining[q] <= 0:
                    newly.append(q)
        ready = sorted(set(ready) | set(newly))
    return order
