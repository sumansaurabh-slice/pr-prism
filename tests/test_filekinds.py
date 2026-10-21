import pytest

from prr import filekinds, merge
from prr.util import write_json
from tests.test_merge import make_pr_dir


@pytest.mark.parametrize("path,expected", [
    ("pkg/cmd/pr/view_test.go", "test"),
    ("tests/unit/gbp/test_budget.py", "test"),
    ("src/app/conftest.py", "test"),
    ("web/src/App.test.tsx", "test"),
    ("web/src/components/Button.spec.ts", "test"),
    ("src/__tests__/util.ts", "test"),
    ("app/src/test/java/com/acme/FooTest.java", "test"),
    ("Acme.Tests/FooTests.cs", "test"),
    ("spec/models/user_spec.rb", "test"),
    ("tests/integration.rs", "test"),
    ("Tests/AppTests/AppTests.swift", "test"),
    ("evals_e2e/journey_steps/step.py", "test"),
    ("testdata/input.json", "test"),
    ("tests/fixtures/sample.md", "test"),           # sample data in a test folder stays a test file
    ("jest.config.ts", "test"),
    ("README.md", "docs"),
    ("CHANGELOG.md", "docs"),
    ("docs/superpowers/specs/design.md", "docs"),    # a design spec is docs, not a test
    ("plans/q4.md", "docs"),
    ("tests/README.md", "docs"),
    ("docs/examples/main.go", "docs"),
    ("src/main/java/com/acme/Foo.java", None),
    ("pkg/eval/expr.go", None),                      # a package named eval is ordinary code
    ("locales/it/strings.json", None),
    ("config/settings.py", None),
    ("requirements.txt", None),
    ("CLAUDE.md", None),                             # AI-agent instructions are never hidden
    ("services/api/AGENTS.md", None),
    (".claude/skills/deploy/SKILL.md", None),
    (".github/copilot-instructions.md", None),
])
def test_language_rules(path, expected):
    assert filekinds.kind(path)[0] == expected


def test_repo_pytest_testpaths(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["checks", "./qa/"]\n')
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = integration\n")
    roots = filekinds.test_roots(tmp_path)
    assert roots == ["checks", "integration", "qa"]
    assert filekinds.kind("checks/budget.py", roots)[0] == "test"
    assert "pytest config" in filekinds.kind("qa/smoke.py", roots)[1]


def test_claude_lists_fill_in_what_the_rules_miss(tmp_path, tricky_repo):
    make_pr_dir(tmp_path, tricky_repo["diff"])
    write_json(tmp_path / "synthesis.json", {
        "summary": {"tldr": "t", "why": "w", "how": "h", "risk": "low", "risk_reasons": []},
        "chapters": [], "flows": [], "impact": [], "questions": [],
        "test_files": ["src/new_name.py", "with space.md"], "docs_files": ["noeol.txt"],
    })
    files = {f["path"]: f for f in merge.build_analysis(tmp_path)["files"]}
    assert files["src/new_name.py"]["is_test"] and files["src/new_name.py"]["kind_reason"].startswith("Claude")
    assert files["with space.md"]["is_docs"] and not files["with space.md"]["is_test"]  # the rule wins
    assert files["noeol.txt"]["is_docs"]
    assert not files["src/core.py"]["is_test"] and not files["src/core.py"]["is_docs"]
