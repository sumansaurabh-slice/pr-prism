"""bin/prr and bin/bootstrap.py: the first-run setup of a plugin install, and the dev checkout path.

Every test uses a copy of the launcher in a temporary folder, a temporary HOME (so ~/.cache/pr-prism is temporary
too), and fake `gh`, `npm` and venv pythons, so nothing touches your real cache, network or GitHub.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "pr-prism"
VERSION = "9.9.9"
RELEASE = "sumansaurabh-slice/pr-prism"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release_files = _load("release_files", REPO / "scripts" / "release_files.py")


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _tree(tmp: Path, version: str = VERSION) -> Path:
    """A plugin folder as Claude Code installs it: the launcher, SKILL.md, pyproject.toml, web sources."""
    root = (tmp / "plugin").resolve()
    (root / "skills" / "pr-prism").mkdir(parents=True)
    shutil.copytree(SKILL / "bin", root / "skills" / "pr-prism" / "bin")
    shutil.copy2(SKILL / "SKILL.md", root / "skills" / "pr-prism" / "SKILL.md")
    (root / ".claude-plugin").mkdir()
    shutil.copy2(REPO / ".claude-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
    (root / "pyproject.toml").write_text(f'[project]\nname = "prr"\nversion = "{version}"\n')
    (root / "web").mkdir()
    (root / "web" / "package.json").write_text('{"name": "prr-web"}\n')
    (root / "web" / "package-lock.json").write_text("{}\n")
    return root


def _web_release(tmp: Path, text: str = "<h1>release</h1>") -> tuple[Path, str]:
    """A web-dist.tar.gz as scripts/release.sh makes it, and its sha256."""
    dist = tmp / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(text)
    (dist / "assets" / "app.js").write_text("console.log(1)")
    tarball = tmp / "out" / "web-dist.tar.gz"
    return tarball, release_files.pack(dist, tarball)


def _fake_venv(cache: Path, root: Path, version: str = VERSION) -> Path:
    """A finished venv-<version> whose python only says how it was called."""
    venv = cache / f"venv-{version}"
    _script(venv / "bin" / "python", 'echo "venv-python $*"\n')
    (venv / "pyvenv.cfg").write_text("home = /x\n")
    (venv / ".prr-ready").write_text(f"{root}\n")
    return venv


@pytest.fixture()
def env(tmp_path):
    """HOME, a fake gh that hands out $FAKE_ASSET and logs its calls, and the python that runs bootstrap.py."""
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    log = tmp_path / "calls.log"
    _script(fakebin / "gh", 'echo "gh $*" >> "$FAKE_LOG"\n'
                            'while [ $# -gt 0 ]; do [ "$1" = "-D" ] && dest="$2"; shift; done\n'
                            'cp "$FAKE_ASSET" "$dest/web-dist.tar.gz"\n')
    e = {k: v for k, v in os.environ.items() if k not in ("PRR_WEB_DIST", "CLAUDE_CONFIG_DIR", "PRR_PYTHON")}
    e.update(HOME=str(home), PATH=f"{fakebin}:{os.environ['PATH']}", PRR_PYTHON=sys.executable, FAKE_LOG=str(log))
    return {"env": e, "home": home, "cache": home / ".cache" / "pr-prism", "fakebin": fakebin, "log": log}


def _prr(root: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(root / "skills" / "pr-prism" / "bin" / "prr"), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _calls(env: dict) -> str:
    return env["log"].read_text() if env["log"].exists() else ""


# ---- the plugin's first run ---------------------------------------------------------------------------------------


def test_first_run_downloads_the_web_app_checks_it_and_prunes_old_versions(tmp_path, env):
    root = _tree(tmp_path)
    tarball, digest = _web_release(tmp_path)
    release_files.stamp(VERSION, Path(f"{tarball}.sha256"), root)  # as the tagged commit holds it
    cache = env["cache"]
    _fake_venv(cache, root)
    old_venv = _fake_venv(cache, Path("/elsewhere"), "0.0.1")
    (cache / "web-0.0.1").mkdir()
    (cache / "web-0.0.1" / "index.html").write_text("old")
    reviews = cache / "web-1x__repo" / "pr-3"  # a reviewed repo's folder whose name happens to start like one
    reviews.mkdir(parents=True)
    e = {**env["env"], "FAKE_ASSET": str(tarball)}

    r = _prr(root, e, "doctor")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "venv-python -P -m prr doctor"
    assert f"release download v{VERSION} -R {RELEASE} -p web-dist.tar.gz" in _calls(env)
    web = cache / f"web-{VERSION}"
    assert (web / "index.html").read_text() == "<h1>release</h1>" and (web / "assets" / "app.js").exists()
    assert not old_venv.exists() and not (cache / "web-0.0.1").exists()
    assert reviews.exists()
    assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"] == VERSION
    assert f"sha256={digest}" in (root / "skills" / "pr-prism" / "web-dist.sha256").read_text()

    env["log"].unlink()
    r = _prr(root, e, "status")  # the second run uses what the first one set up
    assert r.returncode == 0 and r.stdout.strip() == "venv-python -P -m prr status"
    assert _calls(env) == ""


def test_a_web_app_that_does_not_match_the_committed_checksum_is_refused(tmp_path, env):
    root = _tree(tmp_path)
    tarball, _ = _web_release(tmp_path)
    other, _ = _web_release(tmp_path / "other", "<h1>tampered</h1>")
    release_files.stamp(VERSION, Path(f"{tarball}.sha256"), root)
    _fake_venv(env["cache"], root)

    r = _prr(root, {**env["env"], "FAKE_ASSET": str(other)}, "doctor")

    assert r.returncode == 1
    assert "doesn't match the checksum committed in the repo" in r.stderr
    assert "venv-python" not in r.stdout
    assert not (env["cache"] / f"web-{VERSION}").exists()


def test_a_checksum_for_another_version_is_not_used(tmp_path, env):
    root = _tree(tmp_path)
    tarball, _ = _web_release(tmp_path)
    release_files.stamp("0.0.1", Path(f"{tarball}.sha256"), root)  # pyproject was bumped after the last release
    boot = _load("bootstrap_other_version", root / "skills" / "pr-prism" / "bin" / "bootstrap.py")
    assert boot.committed_sha("0.0.1") and boot.committed_sha(VERSION) is None


def test_without_a_release_it_builds_the_web_app_with_node(tmp_path, env):
    root = _tree(tmp_path)  # no web-dist.sha256 for this version
    _fake_venv(env["cache"], root)
    _script(env["fakebin"] / "npm", 'echo "npm $*" >> "$FAKE_LOG"\n'
                                    'case "$1" in ci) mkdir node_modules ;; run) mkdir -p dist && '
                                    'echo built > dist/index.html ;; esac\n')

    r = _prr(root, env["env"], "doctor")

    assert r.returncode == 0, r.stderr
    assert "building the web app with Node" in r.stderr
    assert (env["cache"] / f"web-{VERSION}" / "index.html").read_text().strip() == "built"
    assert "npm ci" in _calls(env) and "gh " not in _calls(env)
    assert not (root / "web" / "node_modules").exists() and not (root / "web" / "dist").exists()  # built in a copy


def test_without_a_release_or_node_it_stops_with_a_plain_error(tmp_path, env, monkeypatch):
    root = _tree(tmp_path)
    boot = _load("bootstrap_no_node", root / "skills" / "pr-prism" / "bin" / "bootstrap.py")
    monkeypatch.setattr(boot.shutil, "which", lambda name: None)
    with pytest.raises(boot.SetupError, match="Node isn't installed to build it; install Node 20"):
        boot.ensure_web(root, tmp_path / "cache", VERSION)


def test_the_venv_is_made_once_per_version_and_folder(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    boot = _load("bootstrap_venv", root / "skills" / "pr-prism" / "bin" / "bootstrap.py")
    made = []

    def fake_make(venv, src):
        made.append(src)
        _script(venv / "bin" / "python", "exit 0\n")

    monkeypatch.setattr(boot, "make_venv", fake_make)
    cache = tmp_path / "cache"
    cache.mkdir()
    py = boot.ensure_venv(root, cache, VERSION)
    assert py == cache / f"venv-{VERSION}" / "bin" / "python"
    assert (cache / f"venv-{VERSION}" / ".prr-ready").read_text().strip() == str(root)
    boot.ensure_venv(root, cache, VERSION)
    assert made == [root]
    boot.ensure_venv(tmp_path / "another-copy", cache, VERSION)  # the same version from another folder
    assert made == [root, tmp_path / "another-copy"]

    def broken(venv, src):
        venv.mkdir()
        raise boot.SetupError("pip couldn't install PR Prism's Python packages: no network; check your network")

    monkeypatch.setattr(boot, "make_venv", broken)
    with pytest.raises(boot.SetupError, match="pip couldn't install"):
        boot.ensure_venv(root, cache, "9.9.10")
    assert not (cache / "venv-9.9.10").exists()  # no half-made venv is left to be trusted later


def test_a_version_a_review_server_still_runs_is_not_pruned(tmp_path, monkeypatch):
    boot = _load("bootstrap_prune", SKILL / "bin" / "bootstrap.py")
    cache = tmp_path / "cache"
    for v in ("0.0.1", "0.0.2"):
        (cache / f"venv-{v}").mkdir(parents=True)
        (cache / f"venv-{v}" / "pyvenv.cfg").write_text("")
        (cache / f"web-{v}").mkdir()
        (cache / f"web-{v}" / "index.html").write_text("")
    busy = f"{cache}/venv-0.0.1/bin/python -P -m prr _serve /x/pr-1"
    monkeypatch.setattr(boot, "_commands", lambda: [busy, "/bin/zsh"])
    removed = boot.prune(cache, VERSION)
    assert sorted(p.name for p in removed) == ["venv-0.0.2", "web-0.0.2"]
    assert (cache / "venv-0.0.1").exists() and (cache / "web-0.0.1").exists()
    monkeypatch.setattr(boot, "_commands", lambda: None)  # can't tell what runs: keep everything
    assert boot.prune(cache, VERSION) == []


def test_the_web_app_tarball_is_reproducible_and_unpacks_only_plain_files(tmp_path):
    boot = _load("bootstrap_unpack", SKILL / "bin" / "bootstrap.py")
    first, digest = _web_release(tmp_path / "a")
    second, again = _web_release(tmp_path / "b")
    assert digest == again == boot.sha256(first) == boot.sha256(second)
    assert Path(f"{first}.sha256").read_text() == f"{digest}  web-dist.tar.gz\n"
    boot.unpack(first, tmp_path / "web")
    assert (tmp_path / "web" / "index.html").exists()

    for name, kind in (("../escape.html", tarfile.REGTYPE), ("index.html", tarfile.SYMTYPE)):
        bad = tmp_path / f"bad-{kind!r}.tar.gz"
        with tarfile.open(bad, "w:gz") as tf:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            data = b"x" if kind == tarfile.REGTYPE else b""
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        with pytest.raises(boot.SetupError, match="unexpected entry"):
            boot.unpack(bad, tmp_path / "web2")
        assert not (tmp_path / "web2").exists()


def test_without_python_311_it_says_so_plainly(tmp_path, env):
    root = _tree(tmp_path)
    old = tmp_path / "oldpython"
    for name in ("python3", "python3.11", "python3.12", "python3.13", "python3.14"):
        _script(old / name, "exit 1\n")  # every python on PATH is too old
    e = {k: v for k, v in env["env"].items() if k != "PRR_PYTHON"}
    e["PATH"] = f"{old}:/usr/bin:/bin"
    r = _prr(root, e, "doctor")
    assert r.returncode == 1
    assert "needs Python 3.11+" in r.stderr and "brew install python@3.12" in r.stderr


# ---- the dev checkout (install.sh's symlink) ------------------------------------------------------------------------


def test_the_symlink_install_resolves_claude_skill_dir_and_keeps_the_dev_venv(tmp_path, env):
    root = _tree(tmp_path)
    _script(root / ".venv" / "bin" / "python", 'echo "dev-venv $*"\n')
    link = env["home"] / ".claude" / "skills" / "pr-prism"
    link.parent.mkdir(parents=True)
    link.symlink_to(root / "skills" / "pr-prism")

    # Claude Code puts the skill's folder, as found in ~/.claude/skills, in place of ${CLAUDE_SKILL_DIR}.
    skill_md = (link / "SKILL.md").read_text()
    assert "allowed-tools: Bash(${CLAUDE_SKILL_DIR}/bin/prr *)" in skill_md
    used = set(re.findall(r"\$\{CLAUDE_SKILL_DIR\}(/[\w./-]+)", skill_md))
    assert used == {"/bin/prr"}
    text = skill_md.replace("${CLAUDE_SKILL_DIR}", str(link))
    commands = re.findall(rf"`({re.escape(str(link))}/bin/prr [^`]*)`", text)
    assert any(c.endswith("/bin/prr doctor") for c in commands)
    assert os.access(link / "bin" / "prr", os.X_OK)

    r = subprocess.run([str(link / "bin" / "prr"), "doctor"], env=env["env"], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dev-venv -P -m prr doctor"  # the checkout's .venv, as before; no bootstrap
    assert not env["cache"].exists()
