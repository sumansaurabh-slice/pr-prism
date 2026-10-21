import os
import signal
import time

from prr import supervise
from prr.util import read_json, write_json
from tests.test_merge import make_pr_dir


def wait_for(cond, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_preferred_port_is_stable_per_pr(tmp_path):
    a = tmp_path / "org__repo" / "pr-1"
    b = tmp_path / "org__repo" / "pr-2"
    assert supervise.preferred_port(a) == supervise.preferred_port(a)
    assert supervise.PORT_BASE <= supervise.preferred_port(a) < supervise.PORT_BASE + supervise.PORT_SPAN
    assert supervise.preferred_port(a) != supervise.preferred_port(b)


def test_identity_is_kept_and_old_server_json_is_reused(tmp_path):
    first = supervise.identity(tmp_path)
    assert supervise.identity(tmp_path) == first
    write_json(tmp_path / "server.json", {"pid": 1, "port": 47999, "token": "old-token", "url": "x"})
    again = supervise.identity(tmp_path)
    assert (again["port"], again["token"]) == (47999, "old-token") and "pid" not in again


def test_server_restarts_after_a_crash_and_stops_only_when_asked(tmp_path, tricky_repo):
    d = tmp_path / "org__repo" / "pr-7"
    d.mkdir(parents=True)
    make_pr_dir(d, tricky_repo["diff"])
    (d / "diff.patch").write_text(tricky_repo["diff"])
    try:
        url = supervise.ensure_running(d)
        info = read_json(d / "server.json")
        first_child = read_json(d / "server.pid")["child_pid"]

        os.kill(first_child, signal.SIGKILL)  # a crash, not a stop
        assert wait_for(lambda: (read_json(d / "server.pid") or {}).get("child_pid") not in (None, first_child))
        assert wait_for(lambda: supervise.healthy(d)), "the server did not come back after the crash"
        assert read_json(d / "server.json") == info  # same port and token, so the same link

        assert supervise.stop(d)
        time.sleep(2)  # longer than the first restart delay: nothing may bring it back
        assert not supervise.healthy(d) and supervise.runtime(d) is None
        assert read_json(d / "server.json") == info

        assert supervise.ensure_running(d) == url  # started again later: still the same link
    finally:
        supervise.stop(d)
