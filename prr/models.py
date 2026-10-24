"""Pydantic models: the LLM output contracts and the analysis.json contract served to the web app.

The frontend mirrors these in web/src/types.ts — change both together.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Level = Literal["low", "medium", "high"]
Side = Literal["LEFT", "RIGHT"]
Category = Literal["schema", "types", "core", "api", "ui", "config", "infra", "test", "docs", "generated"]
NoteKind = Literal["explain", "risk", "flow", "test-gap"]
# blocker = blocks merge (only from the rule pass, confirmed in code); major = must fix; minor = should fix; nit = could improve
Severity = Literal["blocker", "major", "minor", "nit"]
FindingCategory = Literal["security", "correctness", "performance", "concurrency", "reliability", "data",
                          "observability", "design", "testing", "maintainability"]
FindingStatus = Literal["new", "still-open"]
# judge = scored by the second look; rule = a blocker the code check confirmed; carried = an earlier round's finding,
# keeping its earlier verdict; pending = waiting for its second look; none = the second look failed ("not second-checked")
SecondLook = Literal["judge", "rule", "carried", "pending", "none"]
FileStatus = Literal["added", "modified", "deleted", "renamed", "copied"]
ImpactKind = Literal["callers", "breaking", "migration", "config", "perf", "security", "tests", "sensitive", "other"]
FlowNodeKind = Literal["entry", "logic", "store", "external", "test"]
ProgressStatus = Literal["fetched", "annotating", "synthesizing", "complete", "failed"]


# --------------------------------------------------------------------------------------
# LLM output: per-cluster annotation (prompts/annotate.md)
# --------------------------------------------------------------------------------------


class NoteOut(BaseModel):
    side: Side = Field(description="RIGHT for added/context lines (new numbers), LEFT for deleted lines (old numbers)")
    start: int
    end: int
    kind: NoteKind
    text: str = Field(description="Markdown, 1-3 sentences")


class FindingOut(BaseModel):
    title: str = Field(description="The problem in 70 characters or fewer, plain words")
    category: FindingCategory
    severity: Literal["major", "minor", "nit"] = Field(description="major = must fix, minor = should fix, nit = could improve")
    side: Side = Field(description="RIGHT for added or unchanged lines (new numbers), LEFT for deleted lines (old numbers)")
    anchor: str = Field(description="ONE line copied exactly from the diff (without the R/L tag and +/-): the line the comment belongs on")
    line: int = Field(description="That line's number, as shown in the diff")
    start_line: int | None = Field(default=None, description="First line, only when the comment covers several lines")
    impact: str = Field(description="One sentence: who or what is affected, and how")
    fix: str = Field(description="One clear instruction: the better option, not a list of options")
    suggestion: str | None = Field(default=None, description="Drop-in replacement code for exactly the commented line(s), or null")
    why: str = Field(description="Short reasoning in Markdown, 1-4 sentences")
    evidence: str = Field(description="What you read to confirm it, as path:line plus a short quote; or 'diff only'")
    confidence: int = Field(description="0-100: how sure you are this is a real problem")


class FileAnnotation(BaseModel):
    path: str
    summary: str = Field(description="Markdown, 1-3 sentences: this file's role and what changed in it")
    risk: Level
    look_for: list[str] = Field(description="2-5 concrete things the reviewer should verify in this file")
    notes: list[NoteOut]
    changed_symbols: list[str] = Field(description="Functions/types/constants whose behavior or signature changed")
    findings: list[FindingOut]


class FlowEdgeOut(BaseModel):
    source: str = Field(description="'path#Symbol' of the caller / producer")
    target: str = Field(description="'path#Symbol' of the callee / consumer")
    label: str | None = None


class AnnotateOut(BaseModel):
    files: list[FileAnnotation]
    flow_edges: list[FlowEdgeOut]


# --------------------------------------------------------------------------------------
# LLM output: synthesis (prompts/synthesize.md)
# --------------------------------------------------------------------------------------


class Summary(BaseModel):
    tldr: str
    why: str
    how: str
    risk: Level
    risk_reasons: list[str]


class ChapterFile(BaseModel):
    path: str
    why_now: str = Field(description="One line: why read this file at this point in the review")


class Chapter(BaseModel):
    id: str
    title: str
    goal: str
    files: list[ChapterFile]


class FlowNode(BaseModel):
    id: str
    label: str
    kind: FlowNodeKind
    path: str | None = None
    line: int | None = None


class FlowEdge(BaseModel):
    source: str
    target: str
    label: str | None = None


class Flow(BaseModel):
    id: str
    title: str
    narrative: str
    nodes: list[FlowNode]
    edges: list[FlowEdge]


class Ref(BaseModel):
    path: str
    line: int | None = None
    at: Literal["head", "base"] = "head"


class ImpactItem(BaseModel):
    kind: ImpactKind
    severity: Level
    title: str
    detail: str
    refs: list[Ref]


class Question(BaseModel):
    text: str
    refs: list[Ref]


class SynthesisOut(BaseModel):
    summary: Summary
    chapters: list[Chapter]
    flows: list[Flow]
    impact: list[ImpactItem]
    questions: list[Question]
    test_files: list[str] = Field(description="Changed files whose main job is testing (tests, helpers, fixtures, mocks, evals)")
    docs_files: list[str] = Field(description="Changed files that are documentation")


# --------------------------------------------------------------------------------------
# analysis.json — what the web app renders
# --------------------------------------------------------------------------------------


class Commit(BaseModel):
    sha: str
    headline: str
    author: str


class PRInfo(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    url: str
    author: str
    body: str
    state: str
    is_draft: bool
    base_ref: str
    head_ref: str
    base_sha: str
    merge_base_sha: str
    head_sha: str
    additions: int
    deletions: int
    changed_files: int
    labels: list[str]
    commits: list[Commit]


class HunkInfo(BaseModel):
    id: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str


class Note(NoteOut):
    pass


class FileEntry(BaseModel):
    path: str
    old_path: str | None
    status: FileStatus
    additions: int
    deletions: int
    binary: bool
    category: Category
    generated: bool
    sensitive: bool
    minutes: float
    base_blob: str | None
    head_blob: str | None
    tests_for: str | None
    tested_by: list[str]
    is_test: bool
    is_docs: bool
    kind_reason: str | None  # why it counts as a test or docs file
    hunks: list[HunkInfo]
    annotated: bool
    risk: Level | None
    summary: str | None
    look_for: list[str]
    notes: list[Note]
    changed_symbols: list[str]


class Finding(BaseModel):
    id: str
    path: str
    side: Side
    line: int
    start_line: int | None
    anchored: bool                 # on a diff line; False = shown at file level, posted inside the review summary
    anchor: str                    # the exact code line the finding is about
    severity: Severity
    category: str
    title: str
    impact: str
    fix: str
    suggestion: str | None         # replacement code
    one_click: bool                # the code is safe to offer as a GitHub "suggestion" (applies with one click)
    why: str
    evidence: str
    confidence: int | None         # the reviewer's own 0-100
    judge_score: int | None        # the second-look score, 1-10
    judge_reason: str | None
    low_confidence: bool           # hidden by default: only the second look hides (drop or a score under 5)
    second_look: SecondLook        # how the finding was checked a second time
    source: str                    # "claude" or "rule:<id>"
    note: str | None               # e.g. why a blocker was shown as "must fix" instead
    status: FindingStatus          # "still-open": also found in the previous review round
    also_flagged_by: list[str]     # other review tools that flagged the same spot
    comment_body: str              # ready-to-post Markdown


class BotFinding(BaseModel):
    """A finding another review tool (a bot account) already posted on the PR."""
    comment_id: int
    bot: str
    path: str
    line: int | None
    side: Side | None
    severity: Severity | None
    title: str
    body: str
    html_url: str
    verdict: Literal["agree", "disagree", "unsure"] | None   # Claude's second look
    verdict_reason: str | None


class CICheck(BaseModel):
    name: str
    kind: Literal["check", "status"]   # a GitHub check run, or a commit status (e.g. Jenkins)
    state: Literal["pass", "fail", "pending", "skipped", "neutral"]
    url: str | None
    app: str | None
    summary: str | None
    log_tail: str | None
    explanation: dict | None           # Claude's {summary, cause, caused_by_pr, related, fix}


class CIAnnotation(BaseModel):
    check: str
    path: str
    line: int
    end_line: int | None
    level: str
    message: str


class CIInfo(BaseModel):
    fetched_at: str | None
    head_sha: str | None
    checks: list[CICheck]
    annotations: list[CIAnnotation]


class Resolved(BaseModel):
    """An earlier finding or comment that the re-check found fixed or still open."""
    kind: Literal["finding", "comment"]
    title: str
    path: str
    line: int | None
    status: Literal["fixed", "still-open"]
    how: str                           # plain words: "the file was removed", "the line is still there", …
    author: str | None
    html_url: str | None


class Progress(BaseModel):
    status: ProgressStatus
    clusters_total: int = 0
    clusters_done: int = 0
    clusters_failed: int = 0
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    log: list[str] = []
    cost_usd: float = 0.0          # this analysis run's spend so far, from calls.jsonl


class Analysis(BaseModel):
    schema_version: int = SCHEMA_VERSION
    status: ProgressStatus
    focus: str | None
    pr: PRInfo
    summary: Summary | None
    files: list[FileEntry]
    chapters: list[Chapter]
    flows: list[Flow]
    impact: list[ImpactItem]
    questions: list[Question]
    findings: list[Finding]
    bot_findings: list[BotFinding]
    ci: CIInfo | None
    rechecked: list[Resolved]
    progress: Progress
    repair_log: list[str]


# --------------------------------------------------------------------------------------
# JSON Schema for `claude -p --json-schema`
# --------------------------------------------------------------------------------------


def llm_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema with $refs inlined and titles stripped, which keeps the CLI's validator happy."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)
