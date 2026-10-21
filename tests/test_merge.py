from prr import diffparse, merge
from prr.util import write_json


def make_pr_dir(tmp_path, diff_text):
    files = diffparse.parse(diff_text)
    ctx = {
        "pr": {"owner": "o", "repo": "r", "number": 1, "title": "t", "url": "u", "author": "a", "body": "", "state": "OPEN",
               "is_draft": False, "base_ref": "main", "head_ref": "f", "base_sha": "b", "merge_base_sha": "b", "head_sha": "h",
               "additions": 1, "deletions": 1, "changed_files": len(files), "labels": [], "commits": []},
        "files": [{"path": f.path, "old_path": f.old_path, "status": f.status, "additions": f.additions,
                   "deletions": f.deletions, "binary": f.binary, "category": "core", "generated": False, "sensitive": False,
                   "minutes": 1, "base_blob": f.base_blob, "head_blob": f.head_blob, "tests_for": None, "tested_by": [],
                   "imports": [], "hunks": f.hunk_infos()} for f in files],
        "order": [f.path for f in files], "warnings": [],
    }
    write_json(tmp_path / "context.json", ctx)
    return ctx


def test_repairs_bad_anchors_and_places_every_file(tmp_path, tricky_repo):
    ctx = make_pr_dir(tmp_path, tricky_repo["diff"])
    write_json(tmp_path / "clusters.json", {"clusters": [{"id": "c1", "items": [{"path": "src/core.py", "part": 1, "parts": 1, "key": "k1"}]}]})
    write_json(tmp_path / "partials" / "k1.json", {"annotation": {
        "path": "src/core.py", "summary": "Core.", "risk": "high", "look_for": ["x"], "changed_symbols": ["f"],
        "notes": [
            {"side": "RIGHT", "start": 21, "end": 22, "kind": "explain", "text": "ok"},
            {"side": "LEFT", "start": 24, "end": 25, "kind": "risk", "text": "wrong side"},   # only valid on RIGHT
            {"side": "RIGHT", "start": 15, "end": 15, "kind": "flow", "text": "orphan"},       # → summary
        ],
        "suggestions": [
            {"side": "RIGHT", "line": 22, "start_line": 22, "severity": "major", "body": "fine"},
            {"side": "RIGHT", "line": 15, "start_line": None, "severity": "nit", "body": "dropped"},
        ],
    }})
    write_json(tmp_path / "synthesis.json", {
        "summary": {"tldr": "t", "why": "w", "how": "h", "risk": "high", "risk_reasons": []},
        "chapters": [{"id": "a", "title": "A", "goal": "g", "files": [{"path": "./src/core.py", "why_now": "first"},
                                                                       {"path": "nope.py", "why_now": "x"}]}],
        "flows": [{"id": "f", "title": "F", "narrative": "n", "nodes": [{"id": "1", "label": "x", "kind": "entry"}],
                   "edges": [{"source": "1", "target": "missing"}]}],
        "impact": [], "questions": [],
    })
    a = merge.build_analysis(tmp_path)
    core = next(f for f in a["files"] if f["path"] == "src/core.py")
    assert [n["side"] for n in core["notes"]] == ["RIGHT", "RIGHT"]
    assert "orphan" in core["summary"]
    assert len(a["suggestions"]) == 1 and a["suggestions"][0]["start_line"] is None
    placed = [cf["path"] for ch in a["chapters"] for cf in ch["files"]]
    assert sorted(placed) == sorted(f["path"] for f in ctx["files"]) and placed[0] == "src/core.py"
    assert a["flows"][0]["edges"] == []
    assert any("nope.py" in line for line in a["repair_log"])


def test_default_chapters_without_analysis(tmp_path, tricky_repo):
    ctx = make_pr_dir(tmp_path, tricky_repo["diff"])
    a = merge.build_analysis(tmp_path)
    assert a["status"] == "fetched" and a["summary"] is None
    assert [cf["path"] for ch in a["chapters"] for cf in ch["files"]] == ctx["order"]
