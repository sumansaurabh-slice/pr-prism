"""Is a changed file a test file or a docs file? Uses each language's own naming rules, plus the repo's own
test settings (pytest `testpaths`). The analysis can add more (Claude lists test and docs files it finds);
see merge.py. The web app uses these flags for its "Hide test files" and "Hide docs files" checkboxes.
"""

from __future__ import annotations

import re
import tomllib
from configparser import ConfigParser
from pathlib import Path

# (reason shown to the reviewer, pattern on the repo-relative path). First match wins.
TEST_RULES: list[tuple[str, re.Pattern[str]]] = [(reason, re.compile(rx, re.I if ci else 0)) for reason, rx, ci in [
    ("Go test file (*_test.go)", r"_test\.go$", False),
    ("Python test file (test_*.py / *_test.py)", r"(^|/)test_[^/]*\.py$|_test\.py$", False),
    ("pytest shared fixtures (conftest.py)", r"(^|/)conftest\.py$", False),
    ("JavaScript/TypeScript test file (*.test.* / *.spec.*)", r"\.(test|spec)\.(m?[jt]sx?|c[jt]s|vue|svelte)$", False),
    ("Jest test or mock folder (__tests__/, __mocks__/)", r"(^|/)__(tests|mocks)__/", False),
    ("Java/Kotlin/Scala test source (src/test/)", r"(^|/)src/(test|androidTest|testFixtures|integrationTest|it|jmh)/", False),
    ("Java/Kotlin/C#/Groovy test class (*Test, *Tests, *IT, *Spec)", r"(Test|Tests|IT|Spec)\.(java|kt|kts|scala|cs|groovy|fs)$", False),
    ("C# test project (*.Tests/)", r"(^|/)[^/]+\.(Tests?|UnitTests|IntegrationTests)/", False),
    ("Ruby test (*_spec.rb / *_test.rb)", r"_(spec|test)\.rb$", False),
    ("Rust integration test or benchmark (tests/, benches/)", r"(^|/)(tests|benches)/[^/]+\.rs$", False),
    ("Swift test (*Tests.swift)", r"Tests?\.swift$", False),
    ("PHP test (*Test.php)", r"Test\.php$", False),
    ("Elixir test (*_test.exs)", r"_test\.exs$", False),
    ("Dart/Flutter test (*_test.dart)", r"_test\.dart$|(^|/)integration_test/", False),
    ("C/C++ test (*_test.cc, *_unittest.cc)", r"(_test|_tests|_unittest)\.(c|cc|cpp|cxx|h|hpp)$", False),
    ("Test data, fixtures or snapshots", r"(^|/)(testdata|test-data|test_data|fixtures?|__snapshots__|snapshots|cassettes|golden)/|\.(snap|golden)$", False),
    # "evals/" anywhere, but "eval/" only at the top: a package named eval/ can be ordinary code.
    ("Eval or benchmark harness (evals/, benchmarks/)", r"(^|/)evals([_-][^/]+)?/|^eval([_-][^/]+)?/|(^|/)benchmarks?/", True),
    ("Test folder (tests/, e2e/, …)", r"(^|/)(tests?|testing|e2e|integration[_-]?tests?|cypress|playwright)/", False),
    ("Test runner config", r"(^|/)(jest|vitest|karma|playwright|cypress|phpunit|\.mocharc)[^/]*\.(js|ts|cjs|mjs|json|ya?ml|xml)$|(^|/)(pytest|tox)\.ini$", False),
]]

DOC_RULES: list[tuple[str, re.Pattern[str]]] = [(reason, re.compile(rx, re.I)) for reason, rx in [
    ("Project doc (README, CHANGELOG, LICENSE, …)",
     r"(^|/)(README|CHANGELOG|CHANGES|HISTORY|CONTRIBUTING|CODE_OF_CONDUCT|LICEN[CS]E|NOTICE|AUTHORS|SECURITY|SUPPORT|UPGRADING|MIGRATION)([.-][^/]*)?$"),
    ("Docs folder (docs/, doc/, wiki/, adr/, …)", r"(^|/)(docs?|documentation|wiki|adrs?|rfcs?|design[_-]docs?)/"),
    ("Markdown or reStructuredText file", r"\.(md|mdx|markdown|rst|adoc|asciidoc)$"),
]]


def test_roots(repo_root: Path) -> list[str]:
    """Test folders the repo declares itself (pytest `testpaths` in pyproject.toml, pytest.ini, tox.ini, setup.cfg)."""
    roots: list[str] = []
    try:
        data = tomllib.loads((repo_root / "pyproject.toml").read_text())
        roots += list(data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", []))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    for name, section in (("pytest.ini", "pytest"), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        cp = ConfigParser()
        try:
            cp.read(repo_root / name)
        except Exception:  # noqa: BLE001 — a broken config file must not break the review
            continue
        if cp.has_option(section, "testpaths"):
            roots += cp.get(section, "testpaths").split()
    return sorted({r.strip("./").rstrip("/") for r in roots if r.strip("./")})


def test_reason(path: str, roots: list[str] | None = None) -> str | None:
    for root in roots or []:
        if path == root or path.startswith(root + "/"):
            return f"In a test folder this repo's pytest config names ({root}/)"
    return next((reason for reason, rx in TEST_RULES if rx.search(path)), None)


def docs_reason(path: str) -> str | None:
    return next((reason for reason, rx in DOC_RULES if rx.search(path)), None)


# Instructions for AI coding agents look like docs but change how agents behave in the repo, so they are never
# hidden as docs or tests.
AGENT_FILES = re.compile(
    r"(^|/)(CLAUDE|AGENTS|GEMINI|CODEX)(\.local)?\.md$|(^|/)\.claude/|(^|/)\.cursor(rules|/)|(^|/)\.windsurfrules$"
    r"|(^|/)\.github/copilot-instructions\.md$|(^|/)\.github/instructions/",
    re.I,
)


def kind(path: str, roots: list[str] | None = None) -> tuple[str | None, str | None]:
    """("test" | "docs" | None, why). A file gets one kind: sample files inside test data stay tests; otherwise
    docs win (docs/specs/plan.md, tests/README.md are docs). AI-agent instruction files get none."""
    if AGENT_FILES.search(path):
        return None, None
    t, d = test_reason(path, roots), docs_reason(path)
    if t and (not d or t.startswith("Test data")):
        return "test", t
    if d:
        return "docs", d
    return None, None
