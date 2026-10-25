"""Pack changed files into token-budgeted batches for parallel annotation.

Related files share a batch (imports, test↔source, same directory) so the model sees them together.
Generated/vendored/lock files and binaries never go to the model. A single oversized file is split by hunks.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field

from .diffparse import FileDiff, render_for_llm

CHARS_PER_TOKEN = 3.5
DELETED_FILE_PREVIEW_LINES = 60


@dataclass
class Item:
    path: str
    part: int = 1
    parts: int = 1
    hunk_ids: list[str] | None = None
    text: str = ""

    @property
    def tokens(self) -> int:
        return int(len(self.text) / CHARS_PER_TOKEN) + 30


@dataclass
class Cluster:
    id: str
    items: list[Item] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)

    @property
    def paths(self) -> list[str]:
        return [i.path for i in self.items]


def _render(fd: FileDiff, hunk_ids: list[str] | None = None) -> str:
    if fd.status == "deleted":
        body = render_for_llm(fd).split("\n")
        if len(body) > DELETED_FILE_PREVIEW_LINES:
            body = body[:DELETED_FILE_PREVIEW_LINES] + [f"… file deleted; {fd.deletions} lines removed in total"]
        return "\n".join(body)
    return render_for_llm(fd, set(hunk_ids) if hunk_ids else None)


class _UnionFind:
    def __init__(self, items: list[str]):
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        if a in self.parent and b in self.parent:
            self.parent[self.find(a)] = self.find(b)


def eligible(ctx_file: dict, fd: FileDiff) -> bool:
    return not ctx_file["generated"] and not fd.binary and bool(fd.hunks)


def build(ctx: dict, diffs: dict[str, FileDiff], budget_tokens: int, max_files: int) -> list[Cluster]:
    files = {f["path"]: f for f in ctx["files"] if f["path"] in diffs and eligible(f, diffs[f["path"]])}
    order_index = {p: i for i, p in enumerate(ctx["order"])}
    paths = sorted(files, key=lambda p: order_index.get(p, 1 << 30))

    uf = _UnionFind(paths)
    by_dir: dict[str, list[str]] = {}
    for p in paths:
        for dep in files[p]["imports"]:
            uf.union(p, dep)
        if files[p]["tests_for"]:
            uf.union(p, files[p]["tests_for"])
        by_dir.setdefault(posixpath.dirname(p), []).append(p)
    for siblings in by_dir.values():
        for s in siblings[1:]:
            uf.union(siblings[0], s)

    # Linear order that keeps components contiguous and puts each test right after its source.
    comp_first: dict[str, int] = {}
    for p in paths:
        comp_first.setdefault(uf.find(p), order_index.get(p, 1 << 30))

    def key(p: str) -> tuple:
        anchor = files[p]["tests_for"] or p
        return (comp_first[uf.find(p)], order_index.get(anchor, 1 << 30), files[p]["category"] == "test", p)

    linear = sorted(paths, key=key)

    items: list[Item] = []
    for p in linear:
        fd = diffs[p]
        whole = Item(p, text=_render(fd))
        if whole.tokens <= budget_tokens or len(fd.hunks) == 1:
            items.append(whole)
            continue
        groups: list[list[str]] = [[]]
        size = 0
        for h in fd.hunks:
            t = int(len(render_for_llm(fd, {h.id})) / CHARS_PER_TOKEN)
            if groups[-1] and size + t > budget_tokens:
                groups.append([])
                size = 0
            groups[-1].append(h.id)
            size += t
        for n, g in enumerate(groups, 1):
            items.append(Item(p, part=n, parts=len(groups), hunk_ids=g, text=_render(fd, g)))

    clusters: list[Cluster] = []
    current = Cluster(id="c1")
    for it in items:
        if current.items and (current.tokens + it.tokens > budget_tokens or len(current.items) >= max_files):
            clusters.append(current)
            current = Cluster(id=f"c{len(clusters) + 1}")
        current.items.append(it)
    if current.items:
        clusters.append(current)
    # A tiny trailing batch costs a whole extra call; fold it in when the overshoot is small.
    if len(clusters) > 1:
        last, prev = clusters[-1], clusters[-2]
        if last.tokens < 0.15 * budget_tokens and prev.tokens + last.tokens <= 1.15 * budget_tokens:
            prev.items += last.items
            clusters.pop()
    return clusters


def cache_key(ctx_file: dict, item: Item, prompt_version: str, focus: str | None, rules: str = "") -> str:
    """sha256 of the file and its blobs, the part, the prompt version, the focus and the repo rules' hash."""
    raw = "|".join([ctx_file["path"], ctx_file["base_blob"] or "", ctx_file["head_blob"] or "",
                    f"{item.part}/{item.parts}", prompt_version, focus or "", rules])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
