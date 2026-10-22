import re

from prr.analyze import _template, style_rules
from prr.config import PROMPTS_DIR


def test_every_prompt_gets_the_plain_language_rules():
    for name in ("annotate.md", "synthesize.md"):
        text = _template(name, focus="the retry logic")
        assert style_rules() in text
        assert "10th-class" in text
        assert "the retry logic" in text
        left = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
        # the reviewer prompt gets its checklists and the repo's conventions per batch (annotate_prompt)
        assert left == ({"RUBRICS", "CONVENTIONS"} if name == "annotate.md" else set()), f"unfilled placeholder in {name}: {left}"
    assert "{{STYLE}}" in (PROMPTS_DIR / "chat_system.md").read_text()


def test_style_rules_ban_ai_phrasing():
    rules = style_rules()
    for banned in ("leverage", "seamless", "robust", "delve", "em dashes"):
        assert banned in rules


def test_checklists_follow_the_languages_in_a_batch():
    from prr.rubrics import rubric_names, rubrics_for
    assert rubric_names(["svc/api.go", "svc/api_test.go", "web/App.tsx", "db/migrations/001.sql"]) == ["go", "javascript", "sql"]
    assert rubric_names(["app/src/main/java/com/x/HomeScreen.kt"]) == ["kotlin"]
    assert rubric_names(["server/Billing.kt"]) == ["java"]
    text = rubrics_for(["a.py"])
    assert "### Every language" in text and "### Python" in text and "### Go" not in text
