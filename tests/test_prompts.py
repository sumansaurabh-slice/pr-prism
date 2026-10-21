import re

from prr.analyze import _template, style_rules
from prr.config import PROMPTS_DIR


def test_every_prompt_gets_the_plain_language_rules():
    for name in ("annotate.md", "synthesize.md"):
        text = _template(name, focus="the retry logic")
        assert style_rules() in text
        assert "10th-class" in text
        assert "the retry logic" in text
        assert not re.search(r"\{\{[A-Z_]+\}\}", text), f"unfilled placeholder in {name}"
    assert "{{STYLE}}" in (PROMPTS_DIR / "chat_system.md").read_text()


def test_style_rules_ban_ai_phrasing():
    rules = style_rules()
    for banned in ("leverage", "seamless", "robust", "delve", "em dashes"):
        assert banned in rules
