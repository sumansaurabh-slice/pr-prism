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
Severity = Literal["blocker", "major", "minor", "nit"]
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


class SuggestionOut(BaseModel):
    side: Side
    line: int = Field(description="Last line of the commented range")
    start_line: int | None = Field(default=None, description="First line for a multi-line range, else null")
    severity: Severity
    body: str = Field(description="Markdown review comment, written to the PR author")


class FileAnnotation(BaseModel):
    path: str
    summary: str = Field(description="Markdown, 1-3 sentences: this file's role and what changed in it")
    risk: Level
    look_for: list[str] = Field(description="2-5 concrete things the reviewer should verify in this file")
    notes: list[NoteOut]
    changed_symbols: list[str] = Field(description="Functions/types/constants whose behavior or signature changed")
    suggestions: list[SuggestionOut]


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


class Suggestion(BaseModel):
    id: str
    path: str
    side: Side
    line: int
    start_line: int | None
    severity: Severity
    body: str


class Progress(BaseModel):
    status: ProgressStatus
    clusters_total: int = 0
    clusters_done: int = 0
    clusters_failed: int = 0
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    log: list[str] = []


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
    suggestions: list[Suggestion]
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
