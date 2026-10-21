from prr import diffparse


def by_path(files):
    return {f.path: f for f in files}


def test_parses_every_file_kind(tricky_repo):
    files = by_path(diffparse.parse(tricky_repo["diff"]))
    assert set(files) == {"src/core.py", "src/new_name.py", "gone.txt", "logo.png", "run.sh", "noeol.txt",
                          "crlf.txt", "with space.md", "café.txt"}
    assert files["src/new_name.py"].status == "renamed"
    assert files["src/new_name.py"].old_path == "src/old_name.py"
    assert files["gone.txt"].status == "deleted" and files["gone.txt"].head_blob is None
    assert files["café.txt"].status == "added" and files["café.txt"].base_blob is None
    assert files["logo.png"].binary and not files["logo.png"].hunks
    assert files["run.sh"].status == "modified" and not files["run.sh"].hunks  # mode-only
    assert len(files["src/core.py"].base_blob) == 40  # --full-index


def test_line_numbers_and_counts(tricky_repo):
    core = by_path(diffparse.parse(tricky_repo["diff"]))["src/core.py"]
    assert (core.additions, core.deletions) == (3, 2)
    added = [ln for h in core.hunks for ln in h.lines if ln.kind == "+"]
    assert [(ln.new, ln.text) for ln in added] == [(5, "line 5 changed"), (21, "inserted A"), (22, "inserted B")]
    deleted = [ln for h in core.hunks for ln in h.lines if ln.kind == "-"]
    assert [(ln.old, ln.text) for ln in deleted] == [(5, "line 5"), (34, "line 34")]


def test_crlf_and_no_eol_preserved(tricky_repo):
    files = by_path(diffparse.parse(tricky_repo["diff"]))
    crlf_added = [ln.text for h in files["crlf.txt"].hunks for ln in h.lines if ln.kind == "+"]
    assert crlf_added == ["TWO\r"]
    noeol = [(ln.kind, ln.text) for h in files["noeol.txt"].hunks for ln in h.lines]
    assert ("+", "B") in noeol and ("-", "b") in noeol


def test_hunk_contains_and_patch_roundtrip(tricky_repo):
    files = diffparse.parse(tricky_repo["diff"])
    assert "".join(f.patch for f in files) == tricky_repo["diff"]
    core = by_path(files)["src/core.py"]
    assert core.hunk_for("RIGHT", 21) is not None
    assert core.hunk_for("RIGHT", 15) is None  # unchanged region between hunks
    assert core.hunk_for("LEFT", 36) is not None


def test_render_for_llm_tags_lines():
    text = """diff --git a/x.py b/x.py
index 1111111111111111111111111111111111111111..2222222222222222222222222222222222222222 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +1,3 @@ def f():
 a
-b
+B
 c
"""
    fd = diffparse.parse(text)[0]
    out = diffparse.render_for_llm(fd)
    assert "    R1  a" in out and "    L2 -b" in out and "    R2 +B" in out and "@@ h1 -1,3 +1,3 @@ def f():" in out
