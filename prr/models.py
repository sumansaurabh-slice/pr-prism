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
# intent = a question for the author ("Why was this removed?"), not a problem report; always minor (findings.INTENT)
FindingCategory = Literal["security", "correctness", "performance", "concurrency", "reliability", "data",
                          "observability", "design", "testing", "maintainability", "intent"]
FindingStatus = Literal["new", "still-open"]
# judge = scored by the second look; rule = a blocker the code check confirmed; carried = an earlier round's finding,
# keeping its earlier verdict; pending = waiting for its second look; none = the second look failed ("not second-checked")
SecondLook = Literal["judge", "rule", "carried", "pending", "none"]
FileStatus = Literal["added", "modified", "deleted", "renamed", "copied"]
ImpactKind = Literal["callers", "breaking", "migration", "config", "perf", "security", "tests", "sensitive", "other"]
FlowNodeKind = Literal["entry", "logic", "store", "external", "test"]
# deepchecking: the deep check ("what could break") runs after the summary; its findings arrive one area at a time
ProgressStatus = Literal["fetched", "annotating", "synthesizing", "deepchecking", "complete", "failed"]
# guided review: where a stop came from, and the reviewer's answer to it
StopKind = Literal["finding", "risk_note", "look_for", "ci", "bot"]
Verdict = Literal["real", "not", "unsure"]


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
    title: str = Field(description="The problem in 70 characters or fewer, plain words (for intent: the question)")
    category: FindingCategory
    severity: Literal["major", "minor", "nit"] = Field(description="major = must fix, minor = should fix, nit = could improve")
    side: Side = Field(description="RIGHT for added or unchanged lines (new numbers), LEFT for deleted lines (old numbers)")
    anchor: str = Field(description="ONE line copied exactly from the diff (without the R/L tag and +/-): the line the comment belongs on")
    line: int = Field(description="That line's number, as shown in the diff")
    start_line: int | None = Field(default=None, description="First line, only when the comment covers several lines")
    impact: str = Field(description="One sentence: who or what is affected, and how (for intent: the question to "
                                    "the author, 1-3 short polite sentences)")
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
# LLM output: deep check, "what could break" (prompts/deepcheck.md)
# --------------------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    path: str
    line: int


class DeepFinding(FindingOut):
    path: str = Field(description="The changed file the comment belongs on, copied exactly from a ### heading")
    scenario: list[str] = Field(description="2-6 short steps, with example values, from a starting state to the wrong result")
    question: str = Field(description="One yes/no question for the reviewer that settles whether this is real")
    evidence_refs: list[EvidenceRef] = Field(description="Up to 4 spots you read that show it: path and line in the current code")


class DeepCheckOut(BaseModel):
    findings: list[DeepFinding]


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
    author_association: str | None = None  # OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, …; None: the check failed
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
    scenario: list[str] = []       # deep check: "How it could happen", 2-6 steps with example values
    question: str | None = None    # deep check: one yes/no question for the reviewer
    evidence_refs: list[EvidenceRef] = []  # deep check: up to 4 spots it read ({path, line} in the head code)
    sources: list[str] = []        # which passes found it: "annotate", "blockers", "deep-check" (twins list both)


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
    deepcheck_total: int = 0       # areas the deep check is checking in this run
    deepcheck_done: int = 0        # … of which finished (checked, nothing found, failed or stopped)
    deepcheck_cost_usd: float = 0.0  # the deep check's share of cost_usd


class Stop(BaseModel):
    """Guided review: one place that needs a human, with one question (prr/stops.py)."""
    id: str                        # "stop-" + 40 hex; stable across re-runs (never built from a finding's title)
    kind: StopKind
    path: str | None               # None for a CI check
    side: Side | None
    line: int | None
    start_line: int | None
    question: str                  # the heading: one plain question
    scenario: list[str] = []       # "How it could happen" (deep-check findings)
    claude_view: str | None        # what Claude thinks, shown muted after the code (Markdown)
    confidence: int | None         # the finding's "% sure"
    judge_score: int | None        # the second look's 1-10
    second_look: SecondLook | None
    severity: str | None
    category: str | None
    finding_id: str | None
    bot_comment_id: int | None
    check_name: str | None
    comment_body: str | None       # what "Real problem" pre-fills (for a CI check: what it adds to the review summary)
    chapter: str | None            # the reading-order chapter's id (analysis.chapters[].id)
    order: int                     # position in reading order: chapter, file, line
    more: bool                     # past the cap: listed under "N more places"
    low_confidence: bool           # a finding the second look hides: listed only with "Include low-confidence findings"
    sources: list[str] = []        # what found it: annotate, blockers, deep-check, ci, bots
    changed_lines: int = 0         # changed lines in the stop's hunk (the whole file for a look_for item)
    dropped: bool = False          # an answer whose stop is gone or hidden by the second look ("earlier answer")


class ReviewSummary(BaseModel):
    """The Overview's plan box: "N places need a human · M files have nothing flagged · ~K min"."""
    places: int = 0                # stops listed (not "more", not low confidence, not dropped)
    more_places: int = 0           # stops past the cap
    quiet_files: int = 0           # files with no stop at all
    minutes_left: int = 0          # Σ over unanswered listed stops of 2 + min(5, changed_lines // 20)


class SkippedArea(BaseModel):
    """A deep-check area this run didn't check (failed, stopped, or skipped); counted as not checked (E5)."""
    title: str
    reason: str


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
    warnings: list[str] = []       # what didn't run fully, e.g. a deep-check area that failed or was skipped
    skipped_areas: list[SkippedArea] = []  # deep-check areas this run didn't check
    stops: list[Stop] = []         # guided review, in reading order (prr/stops.py)
    review_summary: ReviewSummary = Field(default_factory=ReviewSummary)


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
