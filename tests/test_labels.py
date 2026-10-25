"""E3 labels: labels.jsonl (append, lock, compaction, what counts toward recall) and match_label."""

import fcntl
import os
import stat

import pytest

from prr import labels


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def problem(**kw) -> dict:
    base = {"pr": 7, "kind": "problem", "source": "posted", "commit_id": "c1", "path": "src/a.py", "side": "RIGHT",
            "line": 22, "text": "This drops the error"}
    return labels.new_label(**{**base, **kw})


# ---- the file -----------------------------------------------------------------------------------


def test_append_writes_one_private_line_per_label(tmp_path):
    folder = tmp_path / "o__r"
    first = labels.append(folder, pr=7, kind="problem", source="imported", commit_id="c1", merge_base_sha="b1",
                          path="src/a.py", side="RIGHT", line=22, text="This drops the error", author="me",
                          gh_comment_id=101, split="holdout")
    labels.append(folder, pr=7, kind="not-problem", source="verdict", origin_id="s-2", stop_kind="finding",
                  path="src/a.py", line=40)
    assert mode(folder / "labels.jsonl") == 0o600 and mode(folder / "labels.lock") == 0o600
    rows = labels.read(folder)
    assert [r["kind"] for r in rows] == ["problem", "not-problem"]
    assert rows[0] == first and set(first) == {
        "v", "id", "pr", "commit_id", "merge_base_sha", "path", "side", "line", "kind", "text", "author", "source",
        "origin_id", "gh_comment_id", "finding_id", "stop_kind", "split", "at"}
    assert (first["v"], first["split"], rows[1]["split"]) == (1, "holdout", "train")


def test_bad_values_are_refused():
    with pytest.raises(ValueError):
        labels.new_label(pr=1, kind="maybe", source="posted")
    with pytest.raises(ValueError):
        labels.new_label(pr=1, kind="problem", source="dismissed")      # dismissing is not a label source
    with pytest.raises(ValueError):
        labels.new_label(pr=1, kind="problem", source="posted", split="test")


def test_a_damaged_line_is_skipped(tmp_path):
    folder = tmp_path / "o__r"
    labels.write(folder, [problem()])
    with open(folder / "labels.jsonl", "a") as fh:
        fh.write('{"pr": 7, "kind": "probl')
    assert len(labels.read(folder)) == 1


def test_a_busy_lock_times_out(tmp_path):
    folder = tmp_path / "o__r"
    folder.mkdir()
    holder = os.open(folder / "labels.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)                         # another process is appending
    try:
        with pytest.raises(TimeoutError):
            labels.write(folder, [problem()], timeout=0.2)
        assert labels.LOCK_TIMEOUT_S == 5.0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    labels.write(folder, [problem()])                          # free again
    assert len(labels.read(folder)) == 1


def test_save_never_raises(tmp_path, monkeypatch, capsys):
    pr_dir = tmp_path / "o__r" / "pr-7"
    pr_dir.mkdir(parents=True)

    def full_disk(folder, rows, timeout=labels.LOCK_TIMEOUT_S):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(labels, "write", full_disk)
    assert labels.save(pr_dir, [problem()]) == "Couldn't save this answer for eval"
    assert "could not save 1 label" in capsys.readouterr().err


def test_only_pr_folders_in_the_cache_get_labels(tmp_path):
    assert labels.repo_dir_for(tmp_path / "o__r" / "pr-7") == tmp_path / "o__r"
    assert labels.repo_dir_for(tmp_path / "_eval" / "run" / "pr-7-abc") is None    # an eval copy
    assert labels.repo_dir_for(tmp_path / "test_x0") is None
    assert labels.save(tmp_path / "test_x0", [problem()]) is None
    assert not (tmp_path / "labels.jsonl").exists()


# ---- compaction: one label per issue --------------------------------------------------------------


def test_a_verdict_its_posted_comment_and_an_import_are_one_label():
    verdict = problem(source="verdict", origin_id="s-1", stop_kind="finding", finding_id="f-1", commit_id="c1")
    posted = problem(source="posted", origin_id="s-1", gh_comment_id=555)
    imported = problem(source="imported", gh_comment_id=555, author="me", split="holdout")
    [one] = labels.compact([verdict, posted, imported])
    assert one["source"] == "imported" and one["origin_id"] == "s-1" and one["finding_id"] == "f-1"


def test_an_import_of_a_comment_posted_without_its_id_is_the_same_label():
    posted = problem(source="posted", origin_id="s-1", gh_comment_id=None, line=22)
    imported = problem(source="imported", gh_comment_id=900, line=21)   # one line apart, same place otherwise
    elsewhere = problem(source="imported", gh_comment_id=901, line=30)
    other_side = problem(source="imported", gh_comment_id=902, line=22, side="LEFT")
    out = labels.compact([posted, imported, elsewhere, other_side])
    assert [x["gh_comment_id"] for x in out] == [900, 901, 902]
    assert out[0]["origin_id"] == "s-1"


def test_the_latest_answer_wins_and_retracted_removes_it():
    real = problem(source="verdict", origin_id="s-1")
    assert labels.compact([real, problem(kind="not-problem", source="verdict", origin_id="s-1")])[0]["kind"] == \
        "not-problem"
    retract = labels.new_label(pr=7, kind="retracted", source="verdict", origin_id="s-1")
    assert labels.compact([real, retract]) == []
    again = problem(source="verdict", origin_id="s-1", text="real after all")
    assert [x["text"] for x in labels.compact([real, retract, again])] == ["real after all"]


def test_retracting_a_deleted_draft(tmp_path):
    pr = {"number": 7}
    assert labels.retracted(pr, {"id": "d1", "origin_id": None}) == []
    [row] = labels.retracted(pr, {"id": "d1", "origin_id": "s-1", "path": "src/a.py", "side": "RIGHT", "line": 22,
                                  "commit_id": "c1"})
    assert (row["kind"], row["origin_id"], row["source"]) == ("retracted", "s-1", "verdict")


def test_load_compacts_what_is_on_disk(tmp_path):
    folder = tmp_path / "o__r"
    labels.write(folder, [problem(origin_id="s-1"), problem(origin_id="s-1", gh_comment_id=3)])
    labels.write(folder, [problem(source="imported", gh_comment_id=3)])
    assert len(labels.read(folder)) == 3 and len(labels.load(folder)) == 1


# ---- what counts toward recall, and matching -----------------------------------------------------


def test_the_recall_denominator_leaves_out_ci_and_look_for():
    assert labels.countable(problem())
    assert labels.countable(problem(source="verdict", origin_id="s", stop_kind="risk-note"))
    assert not labels.countable(problem(source="verdict", origin_id="s", stop_kind="ci", path=None, line=None))
    assert not labels.countable(problem(source="verdict", origin_id="s", stop_kind="look_for"))   # even with a line
    assert not labels.countable(problem(line=None))
    assert not labels.countable(problem(kind="not-problem"))


def test_match_label_uses_the_finding_title_and_impact():
    label = problem(line=22, text="The retry loop swallows the timeout error, so callers never see it")
    finding = {"path": "src/a.py", "line": 24, "title": "Timeout error swallowed", "impact": "Callers never see it"}
    assert labels.match_label(label, finding)
    assert not labels.match_label(label, {**finding, "line": 26})                     # more than 3 lines away
    assert not labels.match_label(label, {**finding, "path": "src/b.py"})
    assert not labels.match_label(label, {**finding, "title": "Unused import", "impact": "Lint noise"})
    assert not labels.match_label(problem(line=None), finding)                       # no line: never matched


def test_posted_drafts_get_their_github_ids():
    drafts = [
        {"id": "d1", "path": "src/a.py", "line": 22, "side": "RIGHT", "body": "first", "anchored": True,
         "origin_id": "s-1"},
        {"id": "d2", "path": "src/a.py", "line": 15, "side": "RIGHT", "body": "outside the diff", "anchored": False},
        {"id": "d3", "path": "src/b.py", "line": 3, "side": "LEFT", "body": "removed line", "anchored": True},
    ]
    gh = [{"id": 11, "path": "src/b.py", "line": 3, "body": "removed line"},
          {"id": 10, "path": "src/a.py", "line": 22, "body": "first\n"}]
    out = labels.posted_drafts(drafts, gh)
    assert [(c["path"], c["line"], c["side"], c["gh_comment_id"], c["origin_id"]) for c in out] == [
        ("src/a.py", 22, "RIGHT", 10, "s-1"), ("src/a.py", 15, "RIGHT", None, None), ("src/b.py", 3, "LEFT", 11, None)]
    rows = labels.for_posted({"number": 7, "head_sha": "h", "merge_base_sha": "b"}, out, commit_id="h", author="me")
    assert {(r["kind"], r["source"], r["commit_id"], r["merge_base_sha"], r["author"]) for r in rows} == {
        ("problem", "posted", "h", "b", "me")}
    moved = labels.for_posted({"number": 7, "head_sha": "h", "merge_base_sha": "b"}, out, commit_id="h2", author="me")
    assert {r["merge_base_sha"] for r in moved} == {None}              # posted on a newer head: eval works it out
