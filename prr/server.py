"""The local review server: SPA + JSON API + streamed chat with Claude Code. Binds to 127.0.0.1 only.

Auth: the browser opens /?t=<token>; we set an HttpOnly SameSite=Strict cookie and redirect to /.
Every request must carry the cookie and a Host of 127.0.0.1/localhost:<port>; state-changing requests
must also carry a same-origin Origin. That blocks other local pages, other sites, and DNS rebinding
from driving Claude or posting to GitHub through this server.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import mimetypes
import os
import shutil
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import anchors, github, merge
from .analyze import style_rules
from .claude import ChatProcess, ClaudeError, run_structured, stream_chat
from .config import PROMPTS_DIR, WEB_DIST, Config
from .gitgh import git, viewer_login
from .state import StateStore
from .util import now_iso, read_json, write_text

COOKIE = "prr_session"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_FILE_BYTES = 2_000_000
MAX_CONTEXT_CHARS = 20_000


class Guard:
    """Pure-ASGI auth + security headers (pure ASGI so streamed chat responses aren't buffered)."""

    def __init__(self, app: Any, token: str, port: int, on_activity: Any):
        self.app, self.token, self.on_activity = app, token, on_activity
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    async def _reply(self, send: Any, status: int, body: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"cache-control", b"no-store")] + (headers or [])})
        await send({"type": "http.response.body", "body": body.encode()})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("host") not in self.hosts:
            return await self._reply(send, 403, "Forbidden: unexpected Host header")
        if scope["method"] not in ("GET", "HEAD") and headers.get("origin") not in self.origins:
            return await self._reply(send, 403, "Forbidden: cross-origin request")
        jar = SimpleCookie()
        jar.load(headers.get("cookie", ""))
        cookie = jar[COOKIE].value if COOKIE in jar else ""
        if scope["path"] == "/" and scope["method"] == "GET":
            t = parse_qs(scope.get("query_string", b"").decode()).get("t", [""])[0]
            if t and hmac.compare_digest(t, self.token):
                cookie_hdr = f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/"
                return await self._reply(send, 303, "", [(b"location", b"/"), (b"set-cookie", cookie_hdr.encode())])
        if not hmac.compare_digest(cookie, self.token):
            return await self._reply(send, 401, "Unauthorized: open the link printed by /pr-prism (prr serve).")
        self.on_activity()

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                extra = [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
                         (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------------------


class ReviewedIn(BaseModel):
    path: str
    reviewed: bool


class DraftIn(BaseModel):
    path: str
    side: str
    line: int
    start_line: int | None = None
    body: str
    source: str = "user"
    suggestion_id: str | None = None


class DraftPatch(BaseModel):
    body: str


class ReviewIn(BaseModel):
    body: str = ""
    event: str = "COMMENT"
    commit: str = "analyzed"


class ThreadIn(BaseModel):
    title: str | None = None


class ChipIn(BaseModel):
    path: str
    side: str = "RIGHT"
    start: int = 0
    end: int = 0
    text: str = ""
    label: str = ""


class MessageIn(BaseModel):
    text: str
    context: list[ChipIn] = []


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------


def _sse(ev: dict) -> bytes:
    return f"data: {json.dumps(ev)}\n\n".encode()


def _compose_prompt(text: str, chips: list[ChipIn]) -> str:
    blocks = []
    for c in chips:
        if c.start <= 0:
            blocks.append(f"Context: the whole file `{c.path}` (read it as needed).")
            continue
        where = "PR head (new code)" if c.side == "RIGHT" else "merge-base (old code)"
        rng = f"{c.start}-{c.end}" if c.end and c.end != c.start else f"{c.start}"
        code = c.text[:MAX_CONTEXT_CHARS]
        blocks.append(f"Context: `{c.path}:{rng}` ({where}):\n```\n{code}\n```")
    return ("\n\n".join(blocks) + "\n\n" if blocks else "") + text


def create_app(pr_dir: Path, cfg: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = StateStore(pr_dir)
    running: dict[str, ChatProcess] = {}
    cache: dict[str, Any] = {}

    def ctx() -> dict:
        data = read_json(pr_dir / "context.json")
        if data is None:
            raise HTTPException(503, "context.json missing; run prr fetch")
        return data

    def file_ctx(path: str) -> dict | None:
        return next((f for f in ctx()["files"] if f["path"] == path), None)

    def patches() -> dict[str, str]:
        p = pr_dir / "diff.patch"
        mtime = p.stat().st_mtime
        if cache.get("patch_mtime") != mtime:
            from .diffparse import parse
            cache["patches"] = {f.path: f.patch for f in parse(p.read_bytes().decode("utf-8", errors="replace"))}
            cache["patch_mtime"] = mtime
        return cache["patches"]

    def viewer() -> str:
        if "viewer" not in cache:
            cache["viewer"] = viewer_login()
        return cache["viewer"]

    def codegraph_mcp() -> tuple[str | None, list[str]]:
        root = Path(ctx()["repo_root"])
        if (root / ".codegraph").is_dir() and shutil.which("codegraph"):
            conf = {"mcpServers": {"codegraph": {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]}}}
            return json.dumps(conf), ["mcp__codegraph"]
        return None, []

    def chat_system_file(analysis: dict, codegraph: bool) -> Path:
        pr = analysis["pr"]
        chapters = []
        for i, ch in enumerate(analysis["chapters"], 1):
            chapters.append(f"{i}. **{ch['title']}**: {ch['goal']}")
            chapters += [f"   - `{f['path']}`" for f in ch["files"][:25]]
        root = ctx()["repo_root"]
        values = {
            "HEAD_SHA": pr["head_sha"], "MERGE_BASE_SHA": pr["merge_base_sha"], "PR_DIR": str(pr_dir),
            "EXTRA_TOOLS": (f", plus codegraph tools to find callers and what a change affects (pass "
                            f"projectPath=\"{root}\"; its index matches the reviewer's own checkout, which is close to "
                            "the base branch)") if codegraph else "",
            "PR_TITLE": pr["title"], "PR_URL": pr["url"], "PR_AUTHOR": pr["author"], "BASE_REF": pr["base_ref"],
            "HEAD_REF": pr["head_ref"], "ADDITIONS": str(pr["additions"]), "DELETIONS": str(pr["deletions"]),
            "CHANGED_FILES": str(pr["changed_files"]),
            "TLDR": (analysis["summary"] or {}).get("tldr") or "(the analysis is still running)",
            "CHAPTERS": "\n".join(chapters[:200]) or "(not available yet)",
            "FOCUS": analysis["focus"] or "(none)",
        }
        text = (PROMPTS_DIR / "chat_system.md").read_text().replace("{{STYLE}}", style_rules())
        for k, v in values.items():
            text = text.replace("{{" + k + "}}", v)
        path = pr_dir / "chat_system.md"
        write_text(path, text)
        return path

    # ---- read-only data -------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "pr": ctx()["pr"]["number"]}

    @app.get("/api/analysis")
    def analysis() -> dict:
        return merge.build_analysis(pr_dir)

    @app.get("/api/state")
    def state() -> dict:
        return store.load()

    @app.get("/api/diff")
    def diff(path: str) -> dict:
        patch = patches().get(path)
        if patch is None:
            raise HTTPException(404, "file is not part of this PR")
        return {"path": path, "patch": patch}

    @app.get("/api/file")
    def file(path: str, at: str = "head") -> dict:
        c = ctx()
        fc = file_ctx(path)
        sha = c["pr"]["head_sha"] if at == "head" else c["pr"]["merge_base_sha"]
        real = ((fc or {}).get("old_path") or path) if at == "base" else path
        root = c["repo_root"]
        listed = git("ls-tree", "-r", "--name-only", sha, "--", real, cwd=root, check=False).strip().splitlines()
        if real not in listed:
            raise HTTPException(404, f"{real} does not exist at {at}")
        content = git("show", f"{sha}:{real}", cwd=root)
        if len(content) > MAX_FILE_BYTES:
            content = content[:MAX_FILE_BYTES] + "\n…[truncated]"
        return {"path": path, "at": at, "content": content}

    @app.get("/api/github/threads")
    def threads() -> dict:
        now = time.time()
        if now - cache.get("threads_at", 0) > 60:
            pr = ctx()["pr"]
            try:
                cache["threads"] = github.existing_comments(pr["owner"], pr["repo"], pr["number"])
            except Exception as e:  # noqa: BLE001 — GitHub being unreachable shouldn't break the page
                raise HTTPException(502, f"could not load existing comments: {e}") from e
            cache["threads_at"] = now
        return {"comments": cache["threads"]}

    # ---- reviewer state ------------------------------------------------------------

    @app.put("/api/reviewed")
    def set_reviewed(body: ReviewedIn) -> dict:
        fc = file_ctx(body.path)
        if fc is None:
            raise HTTPException(404, "file is not part of this PR")

        def apply(s: dict) -> dict:
            if body.reviewed:
                s["reviewed"][body.path] = fc["head_blob"] or fc["base_blob"] or ""
            else:
                s["reviewed"].pop(body.path, None)
            return s["reviewed"]

        return {"reviewed": store.update(apply)}

    @app.post("/api/drafts")
    def add_draft(body: DraftIn) -> dict:
        fc = file_ctx(body.path)
        if fc is None or body.side not in ("LEFT", "RIGHT") or not body.body.strip():
            raise HTTPException(400, "draft needs a PR file, a side (LEFT/RIGHT) and a body")
        start = body.start_line if body.start_line and body.start_line != body.line else None
        if start and start > body.line:
            start, line = body.line, start
        else:
            line = body.line
        draft = {
            "id": uuid.uuid4().hex[:12], "path": body.path, "side": body.side, "line": line, "start_line": start,
            "body": body.body, "source": body.source if body.source in ("user", "suggestion", "chat") else "user",
            "suggestion_id": body.suggestion_id, "anchored": anchors.check(fc, body.side, line, start) is None,
            "outdated": False, "commit_id": ctx()["pr"]["head_sha"], "created_at": now_iso(),
        }
        store.update(lambda s: s["drafts"].append(draft))
        return draft

    @app.patch("/api/drafts/{draft_id}")
    def edit_draft(draft_id: str, body: DraftPatch) -> dict:
        def apply(s: dict) -> dict | None:
            for d in s["drafts"]:
                if d["id"] == draft_id:
                    d["body"] = body.body
                    return d
            return None

        d = store.update(apply)
        if d is None:
            raise HTTPException(404, "no such draft")
        return d

    @app.delete("/api/drafts/{draft_id}")
    def delete_draft(draft_id: str) -> dict:
        store.update(lambda s: s.__setitem__("drafts", [d for d in s["drafts"] if d["id"] != draft_id]))
        return {"ok": True}

    @app.post("/api/suggestions/{sid}/dismiss")
    def dismiss(sid: str) -> dict:
        def apply(s: dict) -> list:
            if sid not in s["dismissed_suggestions"]:
                s["dismissed_suggestions"].append(sid)
            return s["dismissed_suggestions"]

        return {"dismissed_suggestions": store.update(apply)}

    @app.post("/api/suggestions/{sid}/restore")
    def restore(sid: str) -> dict:
        def apply(s: dict) -> list:
            s["dismissed_suggestions"] = [x for x in s["dismissed_suggestions"] if x != sid]
            return s["dismissed_suggestions"]

        return {"dismissed_suggestions": store.update(apply)}

    # ---- publishing ----------------------------------------------------------------

    def _current_head(pr: dict, fresh: bool) -> str:
        # The preview runs on every keystroke (debounced), so the GitHub lookup is cached; submit re-checks.
        if fresh or time.time() - cache.get("head_at", 0) > 30:
            try:
                cache["head"] = github.current_head(pr["owner"], pr["repo"], pr["number"])
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"could not reach GitHub: {e}") from e
            cache["head_at"] = time.time()
        return cache["head"]

    def _prepare(body: ReviewIn, fresh: bool = False) -> tuple[dict, list[dict], dict]:
        c = ctx()
        pr = c["pr"]
        current = _current_head(pr, fresh)
        me = viewer()
        info = {"analyzed_head": pr["head_sha"], "current_head": current, "head_changed": current != pr["head_sha"],
                "viewer": me, "is_author": me.lower() == pr["author"].lower()}
        commit = current if body.commit == "current" else pr["head_sha"]
        try:
            payload, unanchored = github.build_payload(c, store.load()["drafts"], body.body, body.event, commit)
        except github.ReviewError as e:
            raise HTTPException(400, str(e)) from e
        return payload, unanchored, info

    @app.post("/api/review/preview")
    def preview(body: ReviewIn) -> dict:
        payload, unanchored, info = _prepare(body)
        return {"payload": payload, "inline_count": len(payload["comments"]), "unanchored": unanchored, **info}

    @app.post("/api/review/submit")
    def submit(body: ReviewIn) -> dict:
        payload, unanchored, info = _prepare(body, fresh=True)
        if info["is_author"] and body.event in ("APPROVE", "REQUEST_CHANGES"):
            raise HTTPException(400, "GitHub doesn't let a PR author approve or request changes on their own PR")
        pr = ctx()["pr"]
        try:
            res = github.submit(pr["owner"], pr["repo"], pr["number"], payload)
        except github.ReviewError as e:
            raise HTTPException(422, str(e)) from e
        record = {"review_id": res.get("id"), "html_url": res.get("html_url", ""), "event": body.event,
                  "comments": len(payload["comments"]), "at": now_iso()}

        def apply(s: dict) -> None:
            s["drafts"] = []
            s["submitted"].append(record)

        store.update(apply)
        cache.pop("threads_at", None)
        return {"ok": True, "review_id": record["review_id"], "html_url": record["html_url"]}

    @app.post("/api/review/draft-summary")
    async def draft_summary() -> dict:
        a = merge.build_analysis(pr_dir)
        drafts = store.load()["drafts"]
        lines = [f"- {d['path']}:{d['line']} — {d['body'][:400]}" for d in drafts] or ["(no inline comments)"]
        prompt = (
            "Write the main text of a GitHub pull request review. Write it as the reviewer, talking to the PR author.\n"
            "Use 3 to 6 short sentences or bullets:\n"
            "- what you think of the PR overall;\n"
            "- the most important problems (point to the inline comments below; do not copy them);\n"
            "- any questions you have.\n"
            "Be friendly and direct. No greeting and no sign-off.\n\n"
            f"{style_rules()}\n\n"
            f"PR: {a['pr']['title']}\nSummary: {(a['summary'] or {}).get('tldr', '')}\n"
            f"Risk: {(a['summary'] or {}).get('risk', 'unknown')}\n\nMy inline comments:\n" + "\n".join(lines)
        )
        schema = {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}
        try:
            out, _ = await asyncio.to_thread(run_structured, prompt, schema, cwd=Path(ctx()["worktree"]),
                                             model=cfg.models.chat, tools=[], max_turns=3, timeout=180)
        except ClaudeError as e:
            raise HTTPException(502, str(e)) from e
        return {"body": out["body"]}

    # ---- chat ------------------------------------------------------------------------

    def _thread(s: dict, tid: str) -> dict:
        t = next((t for t in s["threads"] if t["id"] == tid), None)
        if t is None:
            raise HTTPException(404, "no such thread")
        return t

    @app.get("/api/chat/threads")
    def list_threads() -> list:
        return store.load()["threads"]

    @app.post("/api/chat/threads")
    def new_thread(body: ThreadIn) -> dict:
        t = {"id": uuid.uuid4().hex[:12], "title": body.title or "New thread", "session_id": str(uuid.uuid4()),
             "started": False, "messages": [], "created_at": now_iso()}
        store.update(lambda s: s["threads"].append(t))
        return t

    @app.delete("/api/chat/threads/{tid}")
    def delete_thread(tid: str) -> dict:
        if running.get(tid):
            running[tid].stop()
        store.update(lambda s: s.__setitem__("threads", [t for t in s["threads"] if t["id"] != tid]))
        return {"ok": True}

    @app.post("/api/chat/threads/{tid}/stop")
    def stop(tid: str) -> dict:
        if running.get(tid):
            running[tid].stop()
        return {"ok": True}

    @app.post("/api/chat/threads/{tid}/messages")
    async def send_message(tid: str, body: MessageIn) -> StreamingResponse:
        if tid in running:
            raise HTTPException(409, "a reply is already streaming in this thread")
        if not body.text.strip():
            raise HTTPException(400, "empty message")
        user_msg = {"role": "user", "text": body.text, "context": [c.model_dump() for c in body.context], "tools": [],
                    "cost_usd": None, "error": None, "ts": now_iso()}

        def add_user(s: dict) -> dict:
            t = _thread(s, tid)
            if not t["messages"] and t["title"] == "New thread":
                t["title"] = body.text.strip().splitlines()[0][:60]
            t["messages"].append(user_msg)
            return dict(t)

        thread = store.update(add_user)
        running[tid] = None  # type: ignore[assignment]  # reserve the slot; the process attaches via on_start
        analysis_now = await asyncio.to_thread(merge.build_analysis, pr_dir)
        mcp_config, mcp_tools = codegraph_mcp()
        system_file = chat_system_file(analysis_now, bool(mcp_config))
        prompt = _compose_prompt(body.text, body.context)
        c = ctx()

        async def events():
            reply = {"role": "assistant", "text": "", "context": [], "tools": [], "cost_usd": None, "error": None, "ts": now_iso()}
            started = thread.get("started", False)
            try:
                async for ev in stream_chat(
                    prompt, cwd=Path(c["worktree"]), session_id=thread["session_id"], resume=started,
                    model=cfg.models.chat, system_file=system_file, add_dirs=[pr_dir],
                    mcp_config=mcp_config, mcp_tools=mcp_tools, on_start=lambda p: running.__setitem__(tid, p),
                ):
                    if ev["type"] == "text":
                        reply["text"] += ev["text"]
                        started = True
                    elif ev["type"] == "tool":
                        reply["tools"].append({"name": ev["name"], "summary": ev["summary"]})
                        started = True
                    elif ev["type"] == "result":
                        reply["cost_usd"] = ev.get("cost_usd")
                        started = True
                    elif ev["type"] == "error":
                        reply["error"] = ev["message"]
                    yield _sse(ev)
                yield _sse({"type": "done"})
            finally:
                running.pop(tid, None)

                def save(s: dict) -> None:
                    t = next((t for t in s["threads"] if t["id"] == tid), None)
                    if t is not None:
                        t["messages"].append(reply)
                        t["started"] = started

                store.update(save)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"cache-control": "no-store", "x-accel-buffering": "no"})

    # ---- SPA -------------------------------------------------------------------------

    @app.get("/{rest:path}")
    def spa(rest: str) -> Response:
        if rest.startswith("api/"):
            raise HTTPException(404, "unknown API route")
        index = WEB_DIST / "index.html"
        if not index.exists():
            return Response("The web UI is not built. Run ./install.sh in the PR Prism folder.", status_code=503)
        target = (WEB_DIST / rest).resolve()
        if rest and target.is_file() and WEB_DIST.resolve() in target.parents:
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            cache_ctl = "public, max-age=31536000, immutable" if rest.startswith("assets/") else "no-cache"
            return Response(target.read_bytes(), media_type=mime, headers={"cache-control": cache_ctl})
        return Response(index.read_bytes(), media_type="text/html", headers={"cache-control": "no-cache"})

    app.state.running = running
    return app


# --------------------------------------------------------------------------------------
# Process entry point (`prr _serve`): bind, write server.json, idle shutdown
# --------------------------------------------------------------------------------------


def serve_forever(pr_dir: Path, cfg: Config, port: int = 0) -> None:
    import secrets
    import socket

    import uvicorn

    from .util import write_json

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    last = {"t": time.time()}

    inner = create_app(pr_dir, cfg)
    app = Guard(inner, token, port, on_activity=lambda: last.__setitem__("t", time.time()))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=3))

    async def idle_watch() -> None:
        while not server.should_exit:
            await asyncio.sleep(30)
            if time.time() - last["t"] > cfg.idle_shutdown_minutes * 60 and not inner.state.running:
                server.should_exit = True

    url = f"http://127.0.0.1:{port}/?t={token}"
    write_json(pr_dir / "server.json", {"pid": os.getpid(), "port": port, "token": token, "url": url, "started_at": now_iso()})

    async def main() -> None:
        watcher = asyncio.create_task(idle_watch())
        try:
            await server.serve(sockets=[sock])
        finally:
            watcher.cancel()

    try:
        asyncio.run(main())
    finally:
        info = read_json(pr_dir / "server.json")
        if info and info.get("pid") == os.getpid():
            (pr_dir / "server.json").unlink(missing_ok=True)
