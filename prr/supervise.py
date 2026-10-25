"""Keeps one PR's review server running until someone stops it on purpose.

`prr serve` starts a small supervisor process in the background. The supervisor owns the PR's port and
login token, which are saved in server.json and reused forever, so the link never changes. It runs the real
server as a child process. If the child dies for any reason other than `prr stop`, the supervisor starts it
again on the same port, waiting a little longer after each quick crash (up to 30 s).

Files in the PR folder:
- server.json: {port, token, url}. The PR's fixed address; kept when the server stops, removed by `prr clean`.
- server.pid: {pid, child_pid, port, started_at}. Only present while the supervisor runs.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

from .util import now_iso, read_json, session_cookie, write_json

PORT_BASE, PORT_SPAN = 47100, 800  # each PR gets a preferred port from its name, in 47100-47899
MAX_BACKOFF_S = 30
STABLE_RUN_S = 60  # a child that ran at least this long resets the backoff
EXIT_PORT_BUSY = 98


def _log(msg: str) -> None:
    print(f"[prr {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_prr_process(pid: int) -> bool:
    """Guards against signalling an unrelated process that reused an old pid."""
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return "prr" in out and "_serve" in out


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def preferred_port(pr_dir: Path) -> int:
    key = f"{pr_dir.parent.name}/{pr_dir.name}"
    return PORT_BASE + zlib.crc32(key.encode()) % PORT_SPAN


def _first_free_port(pr_dir: Path, avoid: int | None = None) -> int:
    start = preferred_port(pr_dir)
    for p in range(start, start + 50):
        if p != avoid and port_free(p):
            return p
    with socket.socket() as s:  # every nearby port is taken: let the OS choose
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _save_identity(pr_dir: Path, port: int, token: str) -> dict:
    info = {"port": port, "token": token, "url": f"http://127.0.0.1:{port}/?t={token}"}
    path = pr_dir / "server.json"
    write_json(path, info)
    os.chmod(path, 0o600)
    return info


def identity(pr_dir: Path) -> dict:
    """The PR's fixed port and token, created on first use. An older server.json (with a pid) is reused as-is."""
    info = read_json(pr_dir / "server.json") or {}
    port = info.get("port") or _first_free_port(pr_dir)
    token = info.get("token") or secrets.token_urlsafe(32)
    if info.get("port") != port or info.get("token") != token or "pid" in info:
        info = _save_identity(pr_dir, port, token)
    return info


def runtime(pr_dir: Path) -> dict | None:
    rt = read_json(pr_dir / "server.pid")
    return rt if rt and pid_alive(rt.get("pid")) else None


def running(pr_dir: Path) -> bool:
    """True while the PR's review page is up: its supervisor runs (even mid-restart), or an older server does."""
    rt = read_json(pr_dir / "server.pid") or {}
    info = read_json(pr_dir / "server.json") or {}
    return any(pid_alive(p) and _is_prr_process(p) for p in (rt.get("pid"), info.get("pid")))


def healthy(pr_dir: Path, timeout: float = 2) -> bool:
    info = read_json(pr_dir / "server.json")
    alive = runtime(pr_dir) or (info and pid_alive(info.get("pid")))  # older servers kept their pid in server.json
    if not info or not alive:
        return False
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/health",
                                 headers={"Cookie": f"{session_cookie(info['port'])}={info['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def _wait_dead(pids: list[int], seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.1)


def stop(pr_dir: Path) -> bool:
    """Stop the supervisor (and so the server). Keeps server.json, so the next start uses the same link."""
    rt = read_json(pr_dir / "server.pid") or {}
    info = read_json(pr_dir / "server.json") or {}
    pids = [p for p in (rt.get("pid"), info.get("pid")) if pid_alive(p) and _is_prr_process(p)]
    for p in pids:
        os.kill(p, signal.SIGTERM)
    _wait_dead(pids, 8)
    (pr_dir / "server.pid").unlink(missing_ok=True)
    if "pid" in info:  # an older server.json: keep only the address
        _save_identity(pr_dir, info["port"], info["token"])
    return bool(pids)


def ensure_running(pr_dir: Path, wait_s: float = 25) -> str:
    """Start the supervisor unless it already runs, wait until the server answers, and return its URL."""
    if not runtime(pr_dir):
        info = read_json(pr_dir / "server.json") or {}
        legacy = info.get("pid")
        if pid_alive(legacy) and _is_prr_process(legacy):  # a server from before supervision: replace it
            os.kill(legacy, signal.SIGTERM)
            _wait_dead([legacy], 8)
        log = open(pr_dir / "server.log", "a")
        subprocess.Popen([sys.executable, "-P", "-m", "prr", "_serve", str(pr_dir)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, cwd=pr_dir)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if healthy(pr_dir):
            return read_json(pr_dir / "server.json")["url"]
        time.sleep(0.3)
    raise SystemExit(f"the review server did not start; see {pr_dir / 'server.log'}")


def supervise(pr_dir: Path) -> None:
    """Run the server as a child process and restart it whenever it stops without being asked to."""
    info = identity(pr_dir)
    port = info["port"]
    deadline = time.time() + 10
    while not port_free(port) and time.time() < deadline:  # e.g. the previous server is still shutting down
        time.sleep(0.5)
    if not port_free(port):
        new_port = _first_free_port(pr_dir, avoid=port)
        _log(f"port {port} is used by another program, so this PR moves to port {new_port}")
        info = _save_identity(pr_dir, new_port, info["token"])
        port = new_port

    state: dict = {"stopping": False, "child": None}

    def on_stop(signum: int, _frame: object) -> None:
        state["stopping"] = True
        child = state["child"]
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # closing the terminal must not stop the server

    rt = {"pid": os.getpid(), "child_pid": None, "port": port, "started_at": now_iso()}
    backoff = 1
    _log(f"review server for {pr_dir.name} on {info['url'].split('?')[0]}")
    try:
        while not state["stopping"]:
            started = time.time()
            child = subprocess.Popen([sys.executable, "-P", "-m", "prr", "_serve-child", str(pr_dir), "--port", str(port)],
                                     stdin=subprocess.DEVNULL, env={**os.environ, "PRR_TOKEN": info["token"]})
            state["child"] = child
            write_json(pr_dir / "server.pid", {**rt, "child_pid": child.pid})
            code = child.wait()
            if state["stopping"]:
                break
            if time.time() - started >= STABLE_RUN_S:
                backoff = 1
            reason = "the port was busy" if code == EXIT_PORT_BUSY else f"exit code {code}"
            _log(f"the server stopped unexpectedly ({reason}); starting it again in {backoff}s")
            end = time.time() + backoff
            while time.time() < end and not state["stopping"]:
                time.sleep(0.2)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
    finally:
        child = state["child"]
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
        current = read_json(pr_dir / "server.pid")
        if current and current.get("pid") == os.getpid():
            (pr_dir / "server.pid").unlink(missing_ok=True)
        _log("review server stopped on request")
