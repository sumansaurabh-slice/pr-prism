from pathlib import Path

from prr import classify, cluster, diffparse, graph


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_import_edges_across_languages(tmp_path):
    write(tmp_path, "pkg/store/store.go", "package store\n")
    write(tmp_path, "pkg/api/api.go", 'package api\nimport (\n  "fmt"\n  "github.com/o/r/pkg/store"\n)\n')
    write(tmp_path, "app/models.py", "class A: pass\n")
    write(tmp_path, "app/views.py", "from app.models import A\nfrom . import models\n")
    write(tmp_path, "web/src/lib/util.ts", "export const x = 1\n")
    write(tmp_path, "web/src/App.tsx", "import { x } from './lib/util'\n")
    paths = ["pkg/store/store.go", "pkg/api/api.go", "app/models.py", "app/views.py", "web/src/lib/util.ts", "web/src/App.tsx"]
    edges = graph.import_edges(paths, tmp_path)
    assert edges["pkg/api/api.go"] == ["pkg/store/store.go"]
    assert edges["app/views.py"] == ["app/models.py"]
    assert edges["web/src/App.tsx"] == ["web/src/lib/util.ts"]
    order = graph.dependency_order(paths, edges)
    assert order.index("pkg/store/store.go") < order.index("pkg/api/api.go")
    assert order.index("app/models.py") < order.index("app/views.py")


def test_dependency_order_survives_cycles():
    order = graph.dependency_order(["a", "b", "c"], {"a": ["b"], "b": ["a"], "c": ["a"]})
    assert sorted(order) == ["a", "b", "c"] and order[-1] == "c"


def _fd(path: str, n_lines: int, hunks: int = 1) -> diffparse.FileDiff:
    hs = []
    for i in range(hunks):
        start = 1 + i * (n_lines + 10)
        lines = [diffparse.Line("+", None, start + j, f"code line {j} " + "x" * 60) for j in range(n_lines)]
        hs.append(diffparse.Hunk(f"h{i + 1}", start, 0, start, n_lines, "", lines))
    return diffparse.FileDiff(path, None, "modified", False, "a" * 40, "b" * 40, n_lines * hunks, 0, hs, "")


def test_clusters_respect_budget_split_big_files_and_skip_generated():
    diffs = {
        "core/a.go": _fd("core/a.go", 50),
        "core/a_test.go": _fd("core/a_test.go", 50),
        "big/huge.go": _fd("big/huge.go", 400, hunks=6),
        "vendor/x/lib.go": _fd("vendor/x/lib.go", 50),
    }
    cats = {p: classify.category(fd) for p, fd in diffs.items()}
    pairs = classify.pair_tests(cats)
    ctx = {"order": list(diffs), "files": [
        {"path": p, "generated": cats[p] == "generated", "category": cats[p], "imports": [], "tests_for": pairs.get(p),
         "base_blob": "a", "head_blob": "b"} for p in diffs]}
    clusters = cluster.build(ctx, diffs, budget_tokens=8000, max_files=40)
    paths = [it.path for c in clusters for it in c.items]
    assert "vendor/x/lib.go" not in paths
    assert pairs["core/a_test.go"] == "core/a.go"
    assert paths.index("core/a_test.go") == paths.index("core/a.go") + 1
    huge = [it for c in clusters for it in c.items if it.path == "big/huge.go"]
    assert len(huge) > 1 and all(it.parts == len(huge) for it in huge)
    assert all(c.tokens <= 8000 or len(c.items) == 1 for c in clusters)
