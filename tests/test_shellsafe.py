import pytest

from prr import shellsafe

# Commands the chat really asked about, plus everyday read-only ones.
READ_ONLY = [
    'f=$(grep -rln "async def read_open" src/services/gbp | head -1); echo $f; grep -n "async def read_open" -A 25 $f | head -35',
    "awk 'NR<=1931 && /^(async )?def |^    (async )?def |^class /{l=NR\": \"$0} END{}; NR==1931{print l}' src/tools/a.py",
    "awk 'NR<=432 && /^    (async )?def /{l=NR\": \"$0} NR==432{print l}' a.py; awk -F: '{print $1}' b.txt",
    "sed -n 10,32p src/a.py; echo ---; sed -n '/def x/,/^$/p' b.py",
    "find src -name '*.py' | xargs grep -n settle_readiness | head -20",
    "git log --oneline -5 -- src/a.py && git -C ../repo blame -L 10,20 a.py",
    "for f in $(git ls-files 'src/*.py'); do grep -c TODO $f; done",
    "grep -rn x src 2>/dev/null | sort | uniq -c | sort -rn | head",
    "(cd src && ls -la) | wc -l",
    "gh pr view 965 --json title,body",
    "rg -n 'def \\w+' src | cut -d: -f1 | sort -u",
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
]


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only_commands_run_without_asking(cmd, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert shellsafe.is_read_only(cmd)


@pytest.mark.parametrize("cmd", ASKS)
def test_commands_that_write_or_run_programs_ask(cmd, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert not shellsafe.is_read_only(cmd)


def test_relative_path_entries_turn_auto_allow_off(monkeypatch):
    monkeypatch.setenv("PATH", "node_modules/.bin:/usr/bin")
    assert not shellsafe.is_read_only("grep x a")


def test_always_allow_keys_are_per_program():
    assert shellsafe.approval_keys("npm test -- --watch") == ["npm test"]
    assert shellsafe.approval_keys("python3 -c 'x' | head; npm run build") == ["python3", "npm run"]
    assert shellsafe.approval_keys("grep x a; rm -rf build") == ["rm"]
    # a command we can't follow is only ever allowed again exactly as it was
    assert shellsafe.approval_keys('echo "$(rm x)"') == ['echo "$(rm x)"']
