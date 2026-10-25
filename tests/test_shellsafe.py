import os

import pytest

from prr import shellsafe


@pytest.fixture()
def where(tmp_path):
    """A worktree and a PR folder side by side, like the chat's roots. Returns (scope, cwd, pr folder)."""
    wt, pr = tmp_path / "wt", tmp_path / "pr"
    (wt / "src" / "services" / "gbp").mkdir(parents=True)
    (wt / "src" / "a.py").write_text("x\n")
    pr.mkdir()
    for name in ("server.json", "server.pid", "diff.patch"):
        (pr / name).write_text("{}")
    scope = shellsafe.Scope.of([wt, pr], [pr / "server.json", pr / "server.pid"], "o/r")
    return scope, str(wt), pr


def read_only(cmd, where) -> bool:
    scope, cwd, _ = where
    return shellsafe.is_read_only(cmd, scope, cwd)


# Commands the chat really asked about, plus everyday read-only ones.
READ_ONLY = [
    'f=$(grep -rln "async def read_open" src/services/gbp | head -1); echo $f; grep -n "async def read_open" -A 25 $f | head -35',
    "awk 'NR<=1931 && /^(async )?def |^    (async )?def |^class /{l=NR\": \"$0} END{}; NR==1931{print l}' src/tools/a.py",
    "awk 'NR<=432 && /^    (async )?def /{l=NR\": \"$0} NR==432{print l}' a.py; awk -F: '{print $1}' b.txt",
    "sed -n 10,32p src/a.py; echo ---; sed -n '/def x/,/^$/p' b.py",
    "find src -name '*.py' | xargs grep -n settle_readiness | head -20",
    "git log --oneline -5 -- src/a.py && git -C src blame -L 10,20 a.py",
    "for f in $(git ls-files 'src/*.py'); do grep -c TODO $f; done",
    "grep -rn x src 2>/dev/null | sort | uniq -c | sort -rn | head",
    "(cd src && ls -la) | wc -l",
    "gh pr view 965 --json title,body",
    "gh pr view 3 -R o/r",
    "rg -n 'def \\w+' src | cut -d: -f1 | sort -u",
    # spec: must be allowed
    "find src -name '*.py' | xargs grep x",
    "git log -- src/a.py",
    "sed -n 1,20p src/a.py",
    # patterns and separators that look like paths are not files
    "grep -rn '/api/v1' src",
    "grep -A 3 -e '/etc/passwd' src/a.py",
    "awk -F/ '{print $1}' src/a.py",
    "cut -d/ -f1 src/a.py",
    "git diff --no-index /dev/null src/a.py",
    "for i in 1 2 3; do echo $i; done",
    "git ls-files | grep test | xargs wc -l",
]

ASKS = [
    "rm -rf build",
    "npm test",
    "sed -i '' 's/a/b/' x.py",
    "sed -n 's/a/b/w out.txt' x.py",
    "awk '{print > \"out\"}' x",
    "awk 'BEGIN{system(\"rm -rf x\")}'",
    "find . -name '*.pyc' -delete",
    "find . -exec rm {} +",
    "grep x a > out.txt",
    "echo \"$(rm x)\"",
    "echo `rm x`",
    "diff <(rm -rf x) y",
    "./grep x a",
    "PATH=./bin:$PATH; grep x a",
    "path=(./bin $path); grep x a",
    "GIT_EXTERNAL_DIFF=./x git diff",
    "git -c core.pager=./x log",
    "git checkout main",
    "git diff --output=x",
    "xargs rm < list.txt",
    "sort -o x.txt y.txt",
    "rg --pre ./x foo",
    "uniq in.txt out.txt",
    "cat <<EOF > x\nhi\nEOF",
    "grep x a\nrm -rf y",
    "gh pr merge 1",
    "python3 -c 'print(1)'",
    "echo 'unbalanced",
    # spec E1: the main clone's remote and config
    "git config --get remote.origin.url",
    "git remote -v",
    "cat \"$(sed 's/gitdir: //' .git)/../../config\"",
]

# Spec E1: reads outside the PR, environment dumps and cwd tricks must show a card.
OUTSIDE = [
    "cat ~/.config/gh/hosts.yml",
    "cat /etc/passwd",
    "cat ../../x",
    "cat $HOME/x",
    "grep -r . ~/.config/gh",
    "rg x /",
    "git diff --no-index /dev/null ~/.config/gh/hosts.yml",
    "git -C ../other log",
    "gh pr view 1 -R other/repo",
    "gh pr view https://github.com/other/repo/pull/1",
    "gh repo view other/repo",
    "GH_REPO=other/repo gh pr view 1",
    "f=$(cat list); cat $f",
    "cat list | xargs cat",
    "xargs -a list cat",
    "cat $(cat list)",
    "cat $(cut -d' ' -f2 .git)/../../config",           # the main clone's config, through the worktree's .git file
    "cat /Users/me/code/repo/.git/config",
    "cd && cat .aws/credentials",
    "cd - && cat x",
    "cd .. && cat x",
    "pushd /tmp",
    "popd",
    "diff --from-file=/etc/passwd a",
    "grep -f/etc/passwd x",
    "grep -ie x /etc/passwd",
    "grep /etc/passwd -e x",
    "cat < /etc/passwd",
    "cat {/etc/passwd,a}",
    "sort --files0-from=list",
    "sed '1r /etc/passwd' src/a.py",
    "echo ${HOME}",
    "find / -name id_rsa",
    "git ls-files --format='/etc/%(path)' | xargs cat",
    "find src -printf '/etc/passwd\\n' | xargs cat",
    "grep -rh x src | xargs cat",
    "python3 -c \"print(open('/Users/me/.ssh/id_rsa').read())\"",
]

ENV_DUMPS = [
    "jq -n env",
    "jq -n '$ENV'",
    "awk 'BEGIN{for(k in ENVIRON)print k}'",
    "printenv",
    "env",
    "set",
    "export -p",
    "echo $GITHUB_TOKEN",
    "ps eww",
]


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only_commands_run_without_asking(cmd, where, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert read_only(cmd, where), shellsafe.analyze(cmd, *where[:2])


@pytest.mark.parametrize("cmd", ASKS)
def test_commands_that_write_or_run_programs_ask(cmd, where, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert not read_only(cmd, where)


@pytest.mark.parametrize("cmd", OUTSIDE)
def test_reads_outside_the_pr_ask(cmd, where, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    scope, cwd, _ = where
    assert not shellsafe.is_read_only(cmd, scope, cwd)
    assert shellsafe.analyze(cmd, scope, cwd).outside, cmd


@pytest.mark.parametrize("cmd", ENV_DUMPS)
def test_environment_dumps_ask(cmd, where, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    scope, cwd, _ = where
    rep = shellsafe.analyze(cmd, scope, cwd)
    assert not shellsafe.is_read_only(cmd, scope, cwd) and (rep.env or rep.outside), cmd


def test_the_server_files_are_outside(where):
    scope, cwd, pr = where
    assert not read_only(f"cat {pr}/server.json", where)
    assert not read_only(f"head {pr}/server.pid", where)
    assert read_only(f"grep -n x {pr}/diff.patch", where)
    assert not read_only(f"grep -r token {pr}", where)        # a folder that holds the token is outside too
    assert not scope.contains(str(pr)) and scope.contains(str(pr / "diff.patch"))


def test_relative_paths_follow_cd_and_the_shells_folder(where, tmp_path):
    scope, cwd, _ = where
    assert shellsafe.is_read_only("cd src && cat a.py", scope, cwd)
    assert not shellsafe.is_read_only("cd src && cat ../../x", scope, cwd)
    assert not shellsafe.is_read_only("cat a.py", scope, str(tmp_path))       # the shell is already outside
    assert not shellsafe.is_read_only("git log", scope, "/")


def test_nested_pr_folder(tmp_path):
    """The real layout: the worktree lives inside the PR folder, which holds the token."""
    pr = tmp_path / "pr"
    (pr / "worktree").mkdir(parents=True)
    (pr / "server.json").write_text("{}")
    scope = shellsafe.Scope.of([pr / "worktree", pr], [pr / "server.json", pr / "server.pid"], "o/r")
    wt = str(pr / "worktree")
    assert shellsafe.is_read_only("cat ../diff.patch", scope, wt)
    assert not shellsafe.is_read_only("cat ../server.json", scope, wt)
    assert not shellsafe.is_read_only("cd .. && grep -r token .", scope, wt)
    assert not shellsafe.is_read_only("rg token ..", scope, wt)


def test_bypass_mode_only_cares_about_reads(where):
    """Programs that aren't plain readers may name outside paths; only secrets and reads count as outside."""
    scope, cwd, _ = where
    rep = shellsafe.analyze("rm -rf /tmp/prr-build", scope, cwd)
    assert not rep.outside and rep.elsewhere == [os.path.realpath("/tmp/prr-build")]
    assert shellsafe.analyze("cp ~/.ssh/id_rsa x", scope, cwd).outside
    assert not shellsafe.analyze("npm test", scope, cwd).outside


def test_relative_path_entries_turn_auto_allow_off(where, monkeypatch):
    monkeypatch.setenv("PATH", "node_modules/.bin:/usr/bin")
    assert not read_only("grep x a", where)


def test_always_allow_keys_are_per_program(where):
    scope, cwd, _ = where
    assert shellsafe.approval_keys("npm test -- --watch") == ["npm test"]
    assert shellsafe.approval_keys("python3 -c 'x' | head; npm run build") == ["python3", "npm run"]
    assert shellsafe.approval_keys("grep x a; rm -rf build", scope, cwd) == ["rm"]
    # a command we can't follow is only ever allowed again exactly as it was
    assert shellsafe.approval_keys('echo "$(rm x)"') == ['echo "$(rm x)"']
    # so is one that reads outside the PR
    assert shellsafe.approval_keys("cat  /etc/hosts", scope, cwd) == ["cat /etc/hosts"]
    assert shellsafe.approval_keys("rm -rf /tmp/x", scope, cwd) == ["rm -rf /tmp/x"]
