from prr import anchors, diffparse, github


def ctx_for(diff_text: str) -> dict:
    files = diffparse.parse(diff_text)
    return {
        "pr": {"owner": "o", "repo": "r", "number": 1, "head_sha": "h" * 40, "merge_base_sha": "b" * 40},
        "files": [{"path": f.path, "old_path": f.old_path, "binary": f.binary, "hunks": f.hunk_infos()} for f in files],
    }


def test_check_rules(tricky_repo):
    ctx = ctx_for(tricky_repo["diff"])
    core = next(f for f in ctx["files"] if f["path"] == "src/core.py")
    assert anchors.check(core, "RIGHT", 21, None) is None
    assert anchors.check(core, "RIGHT", 21, 19) is None            # same hunk
    assert "outside" in anchors.check(core, "RIGHT", 15, None)
    assert "one hunk" in anchors.check(core, "RIGHT", 21, 5)        # spans two hunks
    assert anchors.check(core, "LEFT", 36, None) is None
    logo = next(f for f in ctx["files"] if f["path"] == "logo.png")
    assert "binary" in anchors.check(logo, "RIGHT", 1, None)


def test_map_line_follows_insertions_and_deletions():
    # old → new: lines 3-4 replaced by one line, 2 lines inserted after old line 10
    diff = """diff --git a/f b/f
index 1..2 100644
--- a/f
+++ b/f
@@ -3,2 +3 @@
-x
-y
+z
@@ -10,0 +10,2 @@
+n1
+n2
"""
    hunks = diffparse.parse(diff)[0].hunks
    assert anchors.map_line(hunks, 1) == 1
    assert anchors.map_line(hunks, 3) is None
    assert anchors.map_line(hunks, 5) == 4
    assert anchors.map_line(hunks, 10) == 9
    assert anchors.map_line(hunks, 11) == 12


def test_payload_moves_unanchored_drafts_to_body(tricky_repo):
    ctx = ctx_for(tricky_repo["diff"])
    drafts = [
        {"path": "src/core.py", "side": "RIGHT", "line": 22, "start_line": 21, "body": "range", "anchored": True, "outdated": False},
        {"path": "src/core.py", "side": "LEFT", "line": 36, "start_line": None, "body": "deleted", "anchored": True, "outdated": False},
        {"path": "src/core.py", "side": "RIGHT", "line": 15, "start_line": None, "body": "outside", "anchored": False, "outdated": False},
    ]
    payload, unanchored = github.build_payload(ctx, drafts, "Overall", "COMMENT", "h" * 40)
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "h" * 40
    assert payload["comments"][0] == {"path": "src/core.py", "body": "range", "line": 22, "side": "RIGHT",
                                      "start_line": 21, "start_side": "RIGHT"}
    assert payload["comments"][1]["side"] == "LEFT"
    assert len(unanchored) == 1 and "blob/" + "h" * 40 + "/src/core.py#L15" in payload["body"]
    pending, _ = github.build_payload(ctx, drafts[:1], "", "PENDING", "h" * 40)
    assert "event" not in pending


def test_range_from_deleted_line_to_added_line(tricky_repo):
    ctx = ctx_for(tricky_repo["diff"])
    core = next(f for f in ctx["files"] if f["path"] == "src/core.py")
    # hunk 1: "-line 5" (old 5) then "+line 5 changed" (new 5)
    assert anchors.check(core, "RIGHT", 5, 5, "LEFT") is None           # red line → green line, like GitHub
    assert anchors.check(core, "RIGHT", 7, 4, "LEFT") is None
    assert "new line" in anchors.check(core, "LEFT", 5, 5, "RIGHT")     # green → red is not one comment
    assert "one hunk" in anchors.check(core, "RIGHT", 21, 5, "LEFT")    # ends in another hunk


def test_payload_keeps_start_side(tricky_repo):
    ctx = ctx_for(tricky_repo["diff"])
    d = {"path": "src/core.py", "side": "RIGHT", "line": 6, "start_line": 5, "start_side": "LEFT", "body": "x",
         "anchored": True, "outdated": False}
    payload, _ = github.build_payload(ctx, [d], "", "COMMENT", "h" * 40)
    assert payload["comments"][0] == {"path": "src/core.py", "body": "x", "line": 6, "side": "RIGHT",
                                      "start_line": 5, "start_side": "LEFT"}
