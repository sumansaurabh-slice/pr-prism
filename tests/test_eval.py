"""E3 + D20 + D21: `prr eval` replays labelled PRs and scores four views per run. Nothing here reaches GitHub or
Claude: `gh pr view` is a fake, git fetches go to a local repo, and the analysis is a fake (or FakeClaude)."""

import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prr import analyze, bots, ci, cli, config, evalrun, fetch, github, labels, ledger, recheck
from prr.config import Config
from prr.gitgh import PRRef
from prr.util import read_json, write_json
from tests.conftest import _git
from tests.test_cost import FakeClaude
from tests.test_merge import make_pr_dir

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def finding(fid: str, line: int, title: str, *, conf: int = 80, sources=("annotate",), severity: str = "minor") -> dict:
    """A finding in analysis.json's shape (only what eval reads)."""
    return {"id": fid, "path": "src/core.py", "side": "RIGHT", "line": line, "title": title, "impact": "",
            "severity": severity, "confidence": conf, "low_confidence": False, "sources": list(sources)}


def label(line: int | None, text: str, kind: str = "problem", **kw) -> dict:
    base = {"pr": 1, "kind": kind, "source": "imported", "commit_id": "c" * 40, "path": "src/core.py",
            "side": "RIGHT", "line": line, "text": text}
    return labels.new_label(**{**base, **kw})


def call(purpose: str, usd: float, end_s: int = 0, duration_s: int = 0) -> dict:
    return {"at": (T0 + timedelta(seconds=end_s)).isoformat(timespec="seconds"), "run_id": "r", "purpose": purpose,
            "model": "m", "target": "", "duration_ms": duration_s * 1000, "cost_usd": usd, "outcome": "ok", "error": None}


# ---- four views from one run ---------------------------------------------------------------------

F1 = finding("f1", 5, "Changed value breaks parsing")
F2 = finding("f2", 22, "Inserted lines skip validation", sources=("deep-check",))
F3 = finding("f3", 30, "Off by one in the loop")
F4 = finding("f4", 10, "Missing null check", conf=30)
F5 = finding("f5", 40, "Naming nit here")
F6 = finding("f6", 60, "Something else entirely")
LABELS = [
    label(5, "Changed value breaks parsing of old files"),            # found by an annotate finding
    label(21, "Inserted lines skip validation"),                       # found only by the deep check
    label(30, "Off by one in the loop"),                              # found, but the second look dropped it
    label(10, "Missing null check before use"),                       # found, the reviewer was only 30% sure
    label(None, "CI fails on lint", source="verdict", origin_id="s-ci", stop_kind="ci", path=None),  # not countable
    label(40, "Naming nit", kind="not-problem", source="verdict", origin_id="s-n"),
]


def write_run(run_dir, final: list[dict], before: list[dict] | None, calls: list[dict], verdicts: dict | None = None):
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "analysis.json", {"status": "complete", "findings": final})
    if before is not None:
        write_json(run_dir / "placed_pre_deepcheck.json", before)
    write_json(run_dir / "judge.json", verdicts if verdicts is not None else {
        **{f["id"]: {"score": 8, "verdict": "keep", "reason": "ok"} for f in final},
        "f3": {"score": 2, "verdict": "drop", "reason": "not a bug"}})
    for c in calls:
        ledger.append(run_dir, c)


def test_four_views_from_one_run(tmp_path):
    calls = [call("annotate", 1.0, 10, 10), call("judge", 0.1, 20, 5), call("synthesis", 0.5, 40, 20),
             call("deepcheck", 0.4, 100, 50), call("deepcheck", 0.6, 120, 30)]
    write_run(tmp_path, [F1, F2, F3, F4, F5, F6], [F1, F3, F4, F5, F6], calls)
    run = evalrun.score_run(tmp_path, LABELS, elapsed_s=300)
    v = run["views"]
    got = {vid: (v[vid]["found"], v[vid]["hidden"], v[vid]["missed"], v[vid]["recall"]) for vid in v}
    assert got == {"on": (3, 1, 0, 0.75), "on+floor": (2, 2, 0, 0.5), "off": (2, 1, 1, 0.5),
                   "off+floor": (1, 2, 1, 0.25)}
    assert {vid: v[vid]["countable"] for vid in v} == dict.fromkeys(v, 4)          # CI isn't counted
    on = v["on"]
    assert on["not_problem"] == 1 and on["not_problem_findings"][0]["id"] == "f5"
    assert on["unlabelled"] == 1 and on["unlabelled_findings"] == [
        {"id": "f6", "path": "src/core.py", "line": 60, "severity": "minor", "title": "Something else entirely",
         "sources": ["annotate"]}]
    assert (on["judge_hidden"], v["on+floor"]["judge_hidden"], on["findings"]) == (1, 2, 6)
    labelled = {x["text"]: on["labels"][x["id"]] for x in LABELS if labels.countable(x)}
    assert labelled == {"Changed value breaks parsing of old files": "found", "Inserted lines skip validation": "found",
                        "Off by one in the loop": "hidden", "Missing null check before use": "found"}
    # cost and time: "off" leaves out the deep check's spend and its wall time (50 s to 120 s)
    assert (run["cost_usd"], run["deepcheck_usd"], run["deepcheck_time_s"], run["deepcheck_ran"]) == (2.6, 1.0, 70.0, True)
    assert (on["cost_usd"], on["time_s"], on["deepcheck_usd"]) == (2.6, 300, 1.0)
    assert (v["off"]["cost_usd"], v["off"]["time_s"], v["off"]["deepcheck_usd"]) == (1.6, 230, 0.0)


def test_without_the_deep_check_off_is_the_final_set(tmp_path):
    write_run(tmp_path, [F1, F3, F4, F5, F6], None, [call("annotate", 1.0)])
    run = evalrun.score_run(tmp_path, LABELS, elapsed_s=60)
    assert not run["deepcheck_ran"]
    assert run["views"]["on"]["recall"] == run["views"]["off"]["recall"] == 0.5


def test_a_finding_the_second_look_never_scored_keeps_its_own_flag():
    unscored = {**F1, "low_confidence": True}
    assert evalrun.hidden(unscored, {}, None) and not evalrun.hidden(F1, {}, None)
    assert evalrun.hidden(F4, {}, 50) and not evalrun.hidden(F4, {}, None)
    assert not evalrun.hidden({**F4, "severity": "blocker"}, {}, 50)              # blockers are never hidden


# ---- which pairs, and the estimate -----------------------------------------------------------------


def test_pairs_are_distinct_pr_commits_and_a_pr_is_held_out_as_a_whole():
    rows = [label(5, "a", pr=1, commit_id="a" * 40, merge_base_sha="b" * 40),
            label(9, "b", pr=1, commit_id="a" * 40, gh_comment_id=3),
            label(7, "c", pr=1, commit_id="d" * 40, split="holdout"),
            label(7, "d", pr=2, commit_id="e" * 40),
            label(7, "no commit", pr=3, commit_id=None)]
    pairs = evalrun.pairs_from(rows)
    assert [(p["pr"], p["commit_id"][0], p["split"], len(p["labels"])) for p in pairs] == [
        (1, "a", "holdout", 2), (1, "d", "holdout", 1), (2, "e", "train", 1)]
    assert pairs[0]["merge_base_sha"] == "b" * 40 and pairs[1]["merge_base_sha"] is None
    assert [p["pr"] for p in evalrun.pairs_from(rows, split="train")] == [2]
    assert [p["pr"] for p in evalrun.pairs_from(rows, prs=[2])] == [2]


# ---- the whole eval, with fakes ----------------------------------------------------------------------


class FakeAnalysis:
    """Stands in for one eval run's analysis process (evalrun.run_analysis). Run 2 of each pair misses the deep-check
    finding; the deep check costs more on each run."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        monkeypatch.setattr(evalrun, "run_analysis", self)

    def __call__(self, run_dir, tag):
        i = int(run_dir.name.split("-")[1])
        self.calls.append((run_dir, read_json(run_dir / "context.json")["pr"]["head_sha"], tag))
        assert (run_dir / "diff.patch").exists()
        final = [F1, F2] if i != 2 else [F1]
        write_run(run_dir, final, [F1], [call("annotate", 1.0, 10, 10), call("deepcheck", 0.5 * i, 20, 5)])


@pytest.fixture()
def repo(tmp_path, tricky_repo, monkeypatch):
    """Labels for #1 at the head commit (held out) and #2 at a commit nobody can fetch, with GitHub faked."""
    root, head, base = tricky_repo["root"], tricky_repo["head"], tricky_repo["base"]
    cfg = Config(cache_dir=tmp_path / "cache")
    folder = labels.repo_dir(cfg.cache_dir, "o", "r")
    labels.write(folder, [
        label(5, "Changed value breaks parsing", commit_id=head, merge_base_sha=base, split="holdout"),
        label(21, "Inserted lines skip validation", commit_id=head, merge_base_sha=base, split="holdout"),
        label(5, "Gone", pr=2, commit_id="0" * 40, merge_base_sha=base)])
    meta = {"number": 1, "title": "t", "url": "u", "author": {"login": "a"}, "body": "", "state": "MERGED",
            "isDraft": False, "baseRefName": "main", "headRefName": "f", "baseRefOid": base, "headRefOid": "f" * 40,
            "additions": 9, "deletions": 9, "changedFiles": 9, "labels": [], "commits": []}
    asked = []

    def read_meta(ref):
        asked.append(ref.number)
        return meta

    monkeypatch.setattr(fetch, "read_meta", read_meta)
    monkeypatch.setattr(evalrun, "find_remote", lambda root, ref: str(root))
    return type("Repo", (), {"root": root, "head": head, "base": base, "cfg": cfg, "folder": folder, "asked": asked})


def eval_now(repo, **kw):
    lines: list[str] = []
    report = evalrun.run_eval(repo.cfg, repo.root, "o", "r", confirm=lambda text: lines.append(text) or True,
                              out=lines.append, **kw)
    return report, lines


def eval_folders(repo) -> list:
    base = repo.cfg.cache_dir / "_eval"
    return sorted(p.relative_to(base) for p in base.glob("*/*")) if base.exists() else []


def test_eval_replays_each_pair_three_times_and_reports_the_spread(repo, monkeypatch):
    fake = FakeAnalysis(monkeypatch)
    report, lines = eval_now(repo)
    assert report["status"] == "complete" and report["runs_per_pair"] == 3
    # three fresh runs of the pinned commit, each in its own folder
    assert [(c[0].name, c[1], c[2]) for c in fake.calls] == [
        (f"run-{i}", repo.head, f"#1 {repo.head[:7]} run {i}/3") for i in (1, 2, 3)]
    [one, gone] = report["pairs"]
    assert (one["status"], one["labels"]) == ("complete", {"problems": 2, "countable": 2, "not_problem": 0})
    assert gone["status"] == "skipped" and "couldn't fetch commit 0000000000" in gone["reason"]
    assert report["skipped"] == [{"pr": 2, "commit_id": "0" * 40, "reason": gone["reason"]}]
    on, off = report["total"]["all"]["on"], report["total"]["all"]["off"]
    assert on["recall"] == {"mean": pytest.approx(0.8333, abs=1e-4), "min": 0.5, "max": 1.0, "runs": 3}
    assert off["recall"] == {"mean": 0.5, "min": 0.5, "max": 0.5, "runs": 3}
    assert on["deepcheck_usd_per_pr"] == {"mean": 1.0, "min": 0.5, "max": 1.5, "runs": 3}
    assert on["cost_usd_per_pr"]["mean"] == 2.0 and off["cost_usd_per_pr"]["mean"] == 1.0
    assert report["spent_usd"] == 6.0 and report["estimate_usd"] == 2 * 3 * evalrun.DEFAULT_RUN_USD
    assert [p["pr"] for p in report["prs"]] == [1, 2] and set(report["total"]) == {"all", "holdout"}  # no train runs
    assert repo.asked == [1, 2]                                             # `gh pr view` once per PR
    # the gate, on the held-out PR: +33 points, 83%, $1.00 added, no unlabelled findings
    g = report["gate"]
    assert g["pass"] is True and g["prs"] == 1
    assert [(b["shown"], b["pass"]) for b in g["bars"]] == [("+33 points", True), ("83%", True), ("$1.00", True),
                                                           ("0.0", True)]
    text = "\n".join(lines)
    assert "about $30.00" in text and "Wave 2 gate, held-out PRs (1), mean of 3 runs: PASS" in text
    assert "#2 0000000: couldn't fetch commit" in text and "Deep check on, no floor (now): found 1.7 of 2" in text


def test_the_report_is_private_and_the_eval_folders_are_cleaned_up(repo, monkeypatch):
    FakeAnalysis(monkeypatch)
    report, _ = eval_now(repo)
    path = repo.folder / f"eval-{datetime.now(timezone.utc):%Y-%m-%d}.json"
    assert report["path"] == str(path) and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    saved = read_json(path)
    assert saved["gate"]["pass"] is True and saved["pairs"][0]["runs"][0]["views"]["on"]["found"] == 2
    assert eval_folders(repo) == []                                         # worktree removed, folder deleted
    assert "_eval" not in _git(repo.root, "worktree", "list")
    assert _git(repo.root, "rev-parse", f"refs/pr-prism/eval/{repo.head}").strip() == repo.head   # the pin stays
    second, _ = eval_now(repo)
    assert second["path"].endswith("-2.json")                              # a second eval the same day


def test_keep_leaves_the_eval_folders(repo, monkeypatch):
    FakeAnalysis(monkeypatch)
    report, lines = eval_now(repo, keep=True, runs=1)
    folder = repo.cfg.cache_dir / "_eval" / report["eval_id"] / f"pr-1-{repo.head}"
    assert report["folder"] == str(folder.parent) and (folder / "worktree" / "src" / "core.py").exists()
    assert (folder / "run-1" / "analysis.json").exists() and any("Eval folders kept" in x for x in lines)


def test_eval_prepares_the_pinned_commit_with_fetch_prepare(repo, monkeypatch):
    FakeAnalysis(monkeypatch)
    seen = []
    real = fetch.prepare

    def spy(ref, head_sha, merge_base_sha, meta, **kw):
        seen.append((ref, head_sha, merge_base_sha, meta["additions"], kw["d"].name))
        return real(ref, head_sha, merge_base_sha, meta, **kw)

    monkeypatch.setattr(fetch, "prepare", spy)
    eval_now(repo, runs=1)
    # GitHub's +/- counts are for a later head, so they are not compared with this commit's diff
    assert seen == [(PRRef("o", "r", 1), repo.head, repo.base, None, f"pr-1-{repo.head}")]


def test_max_usd_stops_new_runs(repo, monkeypatch):
    fake = FakeAnalysis(monkeypatch)
    report, lines = eval_now(repo, max_usd=2.0, prs=[1])
    assert len(fake.calls) == 2                                             # $1.50, then $3.50: no third run
    assert report["status"] == "stopped" and "reached --max-usd $2.00 after spending $3.50" in report["stop_reason"]
    assert report["pairs"][0]["status"] == "partial" and len(report["pairs"][0]["runs"]) == 2
    assert report["total"]["all"]["on"]["recall"]["runs"] == 2


def test_nothing_runs_without_a_yes(repo, monkeypatch):
    fake = FakeAnalysis(monkeypatch)
    shown: list[str] = []
    out: list[str] = []
    assert evalrun.run_eval(repo.cfg, repo.root, "o", "r", confirm=lambda t: shown.append(t) or False,
                            out=out.append) is None
    assert fake.calls == [] and eval_folders(repo) == [] and out == ["Not run."]
    assert shown == ["This replays 2 labelled PR commits × 3 runs: about $30.00 (no earlier analysis in this repo, so "
                     "$5.00 each is assumed)."]


def test_the_estimate_uses_each_prs_full_analysis(repo):
    for n, runs in ((1, {"r1": [3.0, 1.0], "r2": [0.2]}), (9, {"r3": [2.5]})):  # r2 reused cached file notes
        (repo.folder / f"pr-{n}").mkdir()
        for rid, costs in runs.items():
            for c in costs:
                ledger.append(repo.folder / f"pr-{n}", {**call("annotate", c), "run_id": rid})
    write_json(repo.folder / "pr-1" / "progress.json", {"status": "complete", "cost_usd": 0.2, "run_id": "r2"})
    pairs = evalrun.pairs_from(labels.load(repo.folder))
    assert evalrun.full_run_usd(repo.folder) == {1: 4.0, 9: 2.5}
    usd, basis = evalrun.estimate(pairs, 3, repo.folder)
    assert usd == 3 * (4.0 + 3.25)                  # #2 was never analyzed here: the others' average
    assert basis == "each PR's most expensive earlier analysis, and $3.25 for 1 PR commit never analyzed here"


def test_a_failed_run_is_a_row_not_the_end(repo, monkeypatch):
    fake = FakeAnalysis(monkeypatch)

    def flaky(run_dir, tag):
        if run_dir.name == "run-2":
            ledger.append(run_dir, call("annotate", 0.3))
            raise evalrun.RunFailed("the analysis stopped with exit code 1 (its log is above)")
        return fake(run_dir, tag)

    monkeypatch.setattr(evalrun, "run_analysis", flaky)
    report, _ = eval_now(repo, prs=[1])
    runs = report["pairs"][0]["runs"]
    assert [r["status"] for r in runs] == ["complete", "failed", "complete"] and "exit code 1" in runs[1]["reason"]
    assert runs[1]["cost_usd"] == 0.3 and report["spent_usd"] == 1.5 + 0.3 + 2.5
    assert report["total"]["all"]["on"]["recall"]["runs"] == 2


def test_a_broken_run_folder_is_a_failed_run_too(repo, monkeypatch):
    fake = FakeAnalysis(monkeypatch)

    def no_findings_file(run_dir, tag):
        fake(run_dir, tag)
        (run_dir / "judge.json").write_text("{not json")                  # read_json treats it as missing
        (run_dir / "analysis.json").write_text('{"findings": [{"id": "x"}]}')  # a finding without a path

    monkeypatch.setattr(evalrun, "run_analysis", no_findings_file)
    report, _ = eval_now(repo, prs=[1], runs=1)
    [run] = report["pairs"][0]["runs"]
    assert run["status"] == "failed" and run["reason"].startswith("KeyError") and report["status"] == "complete"


def test_ctrl_c_writes_a_partial_report_and_cleans_up(repo, monkeypatch):
    fake = FakeAnalysis(monkeypatch)

    def interrupted(run_dir, tag):
        if run_dir.name == "run-2":
            ledger.append(run_dir, call("annotate", 0.7))                     # spent before the stop
            raise KeyboardInterrupt
        return fake(run_dir, tag)

    monkeypatch.setattr(evalrun, "run_analysis", interrupted)
    report, lines = eval_now(repo)
    assert report["status"] == "aborted" and report["stop_reason"] == "stopped with Ctrl-C"
    [one, two] = report["pairs"]
    assert one["status"] == "partial" and len(one["runs"]) == 1
    assert (two["status"], two["reason"]) == ("skipped", "stopped with Ctrl-C")   # never reached, still listed
    assert report["spent_usd"] == 2.2 and read_json(Path(report["path"]))["status"] == "aborted"
    assert eval_folders(repo) == [] and any("aborted: stopped with Ctrl-C" in x for x in lines)


# ---- each run in its own process group ----------------------------------------------------------


def test_ctrl_c_stops_the_run_and_everything_it_started(monkeypatch, tmp_path):
    started, stopped = [], []

    class Proc:
        pid = 4242

        def wait(self, timeout=None):
            raise KeyboardInterrupt

    def popen(argv, **kw):
        started.append((argv[-3:], kw["start_new_session"]))
        return Proc()

    monkeypatch.setattr(evalrun.subprocess, "Popen", popen)
    monkeypatch.setattr(evalrun, "stop_process_group", lambda proc: stopped.append(proc.pid))
    with pytest.raises(KeyboardInterrupt):
        evalrun.run_analysis(tmp_path, "#1 abc run 1/3")
    assert started == [(["_eval-run", str(tmp_path), "#1 abc run 1/3"], True)] and stopped == [4242]


def test_stopping_a_process_group_stops_its_children(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = ("import subprocess, sys, time\n"
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
              "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    deadline = time.monotonic() + 20
    while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(marker.read_text())
    evalrun.stop_process_group(proc)
    assert proc.poll() is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the run's own child process is still running")


def test_a_run_process_reports_failure(tmp_path):
    with pytest.raises(evalrun.RunFailed, match="exit code 1"):
        evalrun.run_analysis(tmp_path, "#1 abc run 1/1")                  # no context.json: the child says so


def test_the_run_process_analyzes_in_eval_mode(tmp_path, monkeypatch, capsys):
    seen = []

    def fake(run_dir, ctx, cfg, focus, force, echo, mode="review"):
        seen.append((run_dir, ctx["pr"]["number"], focus, force, mode))
        echo("Reading 3 files")
        return {"status": "complete"}

    monkeypatch.setattr(analyze, "_analyze", fake)
    write_json(tmp_path / "context.json", {"pr": {"number": 7}})
    assert evalrun.child_run(tmp_path, Config(), "#7 abc run 1/3") == 0
    assert seen == [(tmp_path, 7, None, True, "eval")] and "#7 abc run 1/3: Reading 3 files" in capsys.readouterr().err
    monkeypatch.setattr(analyze, "_analyze", lambda *a, **k: {"status": "failed"})
    assert evalrun.child_run(tmp_path, Config(), "t") == 1


def test_no_labels_means_nothing_to_run(tmp_path):
    out: list[str] = []
    assert evalrun.run_eval(Config(cache_dir=tmp_path), tmp_path, "o", "r", confirm=lambda t: True,
                            out=out.append) is None
    assert "Import your comments with `prr labels import <pr…>`" in out[0]


def test_no_held_out_prs_means_no_gate(repo, monkeypatch):
    FakeAnalysis(monkeypatch)
    report, lines = eval_now(repo, split="train")
    assert report["gate"]["pass"] is None and report["gate"]["bars"] == []
    assert any("No held-out PRs were scored" in x for x in lines)


def test_the_command_needs_yes_without_a_terminal(repo, monkeypatch, capsys):
    fake = FakeAnalysis(monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda: repo.cfg)
    monkeypatch.setattr(config, "REPO_DIR", repo.root)
    monkeypatch.setattr(cli, "_has_terminal", lambda: False)
    import prr.gitgh
    monkeypatch.setattr(prr.gitgh, "default_repo", lambda cwd: ("o", "r"))
    with pytest.raises(SystemExit) as done:
        cli.main(["--repo", str(repo.root), "eval", "--prs", "1"])
    assert done.value.code == 1 and fake.calls == []
    assert "runs only with --yes" in capsys.readouterr().out
    with pytest.raises(SystemExit) as done:
        cli.main(["--repo", str(repo.root), "eval", "--prs", "#1", "--runs", "1", "--yes"])
    assert done.value.code == 0 and len(fake.calls) == 1
    assert "Report:" in capsys.readouterr().out


# ---- eval mode in the real analysis ----------------------------------------------------------------


def test_eval_mode_skips_ci_bots_rechecks_and_history(tmp_path, tricky_repo, monkeypatch):
    FakeClaude(monkeypatch)
    root = tricky_repo["root"]
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    ctx = make_pr_dir(run_dir, tricky_repo["diff"])
    ctx["pr"].update(merge_base_sha=tricky_repo["base"], head_sha=tricky_repo["head"])
    ctx = {**ctx, "repo_root": str(root), "worktree": str(root)}
    write_json(run_dir / "context.json", ctx)
    (run_dir / "diff.patch").write_text(tricky_repo["diff"])
    earlier = {"head_sha": "an-older-head", "items": [{"id": "f-0", "kind": "finding", "title": "t", "path": "src/core.py",
                                                       "side": "RIGHT", "line": 5, "anchor": "x", "old_window": ""}]}
    write_json(run_dir / "history.json", earlier)                             # would be re-checked in a review
    touched: list[str] = []
    monkeypatch.setattr(ci, "fetch", lambda *a, **k: touched.append("ci"))
    monkeypatch.setattr(github, "existing_comments", lambda *a, **k: touched.append("comments") or [])
    monkeypatch.setattr(bots, "check", lambda *a, **k: touched.append("bots") or {})
    monkeypatch.setattr(recheck, "run", lambda *a, **k: touched.append("recheck") or {})

    a = analyze._analyze(run_dir, ctx, Config(), None, True, lambda m: None, mode="eval")
    assert a["status"] == "complete" and touched == []
    purposes = {x["purpose"] for x in ledger.read(run_dir)}
    assert "annotate" in purposes and not purposes & {"ci", "bots", "recheck"}
    assert not any((run_dir / n).exists() for n in ("ci.json", "bots.json", "recheck.json"))
    assert read_json(run_dir / "history.json") == earlier                    # not replaced by this run

    analyze._analyze(run_dir, ctx, Config(), None, True, lambda m: None)      # a normal review does all of it
    assert {"ci", "comments", "recheck"} <= set(touched)
    assert read_json(run_dir / "history.json")["head_sha"] == tricky_repo["head"]
