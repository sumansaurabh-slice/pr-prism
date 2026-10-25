#!/usr/bin/env python3
"""First-run setup for `prr`, then the hand-off to it. Standard library only.

bin/prr runs this with the system python3 (3.11+) whenever the folder is not a dev checkout set up by install.sh
(one with <repo>/.venv), for example a Claude Code plugin install. Once per version it makes sure of:

1. Python: ~/.cache/pr-prism/venv-<version>, with `pip install -e <repo root>`. Claude Code keeps each plugin version
   in its own folder (~/.claude/plugins/cache/<marketplace>/pr-prism/<version>/) and removes it only 14 days after an
   update, so an editable install of that folder stays valid while the version is in use. The venv remembers which
   folder it was installed from, and is made again if that changes.
2. The web app: PRR_WEB_DIST, else <repo>/web/dist if it is built (dev), else ~/.cache/pr-prism/web-<version>. That
   one comes from the GitHub release v<version> and must match skills/pr-prism/web-dist.sha256, the checksum committed
   in the tagged repo (never one fetched from the same release). With no release for this version, it is built with
   Node instead.
3. Older versions: the venv-*/web-* folders of other versions are removed, unless a running review server uses them.

Then it runs `prr` from the venv with the same arguments. Every failure ends in one plain line with the fix.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("prr: PR Prism needs Python 3.11 or newer (found %d.%d). Install it, e.g. `brew install python@3.12`."
             % sys.version_info[:2])

import contextlib  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import tomllib  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Iterator  # noqa: E402

RELEASE_REPO = "sumansaurabh-slice/pr-prism"
ASSET = "web-dist.tar.gz"
SKILL_DIR = Path(__file__).resolve().parent.parent  # skills/pr-prism
ROOT = SKILL_DIR.parent.parent                      # the repo, or the plugin's folder
SHA_FILE = SKILL_DIR / "web-dist.sha256"
READY = ".prr-ready"  # written last into a finished venv; holds the folder it was installed from
_VERSIONED = re.compile(r"^(venv|web)-([0-9][0-9A-Za-z.+-]*)$")


class SetupError(Exception):
    """Why the setup stopped, in one plain line that ends with the fix."""


def log(msg: str) -> None:
    print(f"prr: {msg}", file=sys.stderr, flush=True)


def cache_home() -> Path:
    return Path.home() / ".cache" / "pr-prism"


def read_version(root: Path) -> str:
    try:
        return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as e:
        raise SetupError(f"can't read the version from {root / 'pyproject.toml'} ({e}); reinstall PR Prism") from e


def _run(cmd: list[str], what: str, fix: str, cwd: Path | None = None, timeout: int = 900) -> None:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SetupError(f"{what} ({e}); {fix}") from e
    if r.returncode:
        tail = " ".join((r.stderr or r.stdout).strip().splitlines()[-2:])[-300:]
        raise SetupError(f"{what}: {tail or f'exit code {r.returncode}'}; {fix}")


# ---- Python ------------------------------------------------------------------------------------------------------


def make_venv(venv: Path, root: Path) -> None:
    _run([sys.executable, "-m", "venv", str(venv)], "couldn't create a Python environment",
         "install Python's venv module (on Debian/Ubuntu: apt install python3-venv) and run the command again")
    _run([str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
          "-e", str(root)], "pip couldn't install PR Prism's Python packages",
         "check your network connection and run the command again")


def ensure_venv(root: Path, cache: Path, version: str) -> Path:
    venv = cache / f"venv-{version}"
    py = venv / "bin" / "python"
    marker = venv / READY
    if py.exists() and marker.exists() and marker.read_text().strip() == str(root):
        return py
    if venv.exists():  # half made, or installed from another folder
        shutil.rmtree(venv)
    log(f"first run of PR Prism {version}: setting up Python in {venv} (about a minute)")
    try:
        make_venv(venv, root)
    except SetupError:
        shutil.rmtree(venv, ignore_errors=True)
        raise
    marker.write_text(f"{root}\n")
    return py


# ---- Web app -----------------------------------------------------------------------------------------------------


def find_web(root: Path, cache: Path, version: str) -> Path | None:
    """The built web app, in the order prr/config.py looks for it."""
    if os.environ.get("PRR_WEB_DIST"):
        return Path(os.environ["PRR_WEB_DIST"])
    for d in (root / "web" / "dist", cache / f"web-{version}"):
        if (d / "index.html").is_file():
            return d
    return None


def committed_sha(version: str) -> str | None:
    """The web app's sha256 that scripts/release.sh committed for this version, or None."""
    try:
        text = SHA_FILE.read_text()
    except FileNotFoundError:
        return None
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    digest = fields.get("sha256", "").strip().lower()
    if fields.get("version", "").strip() != version or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return digest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(version: str, dest: Path) -> Path:
    gh = shutil.which("gh")
    if not gh:
        raise SetupError("couldn't download the web app: gh isn't installed; install it (brew install gh), "
                         "run gh auth login, and run the command again")
    _run([gh, "release", "download", f"v{version}", "-R", RELEASE_REPO, "-p", ASSET, "-D", str(dest), "--clobber"],
         f"couldn't download the web app for v{version} from github.com/{RELEASE_REPO}",
         "check that gh is logged in (gh auth status) and run the command again", timeout=300)
    path = dest / ASSET
    if not path.is_file():
        raise SetupError(f"the v{version} release has no {ASSET}; tell the PR Prism maintainer")
    return path


def unpack(tarball: Path, target: Path) -> None:
    """Extract into a temporary folder next to target, then move it into place. Only plain files and folders."""
    tmp = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            members = tf.getmembers()
            for m in members:
                if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts:
                    raise SetupError(f"the downloaded web app holds an unexpected entry ({m.name}), so it wasn't "
                                     "used; tell the PR Prism maintainer")
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tf.extractall(tmp, members=members, **extra)
        if not (tmp / "index.html").is_file():
            raise SetupError("the downloaded web app has no index.html; tell the PR Prism maintainer")
        if target.exists():
            shutil.rmtree(target)
        os.replace(tmp, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_web(root: Path, target: Path) -> None:
    """Build the web app with Node from a copy of <root>/web, so the install folder stays untouched."""
    npm = shutil.which("npm")
    fix = "install Node 20+ (brew install node) and run the command again"
    work = Path(tempfile.mkdtemp(prefix=".web-build-", dir=target.parent))
    try:
        src = work / "web"
        shutil.copytree(root / "web", src, ignore=shutil.ignore_patterns("node_modules", "dist"))
        install = "ci" if (src / "package-lock.json").exists() else "install"
        _run([npm or "npm", install, "--no-audit", "--no-fund", "--loglevel=error"], "npm couldn't install the "
             "web app's packages", fix, cwd=src)
        _run([npm or "npm", "run", "build", "--silent"], "couldn't build the web app", fix, cwd=src)
        if not (src / "dist" / "index.html").is_file():
            raise SetupError(f"the web app build made no index.html; {fix}")
        if target.exists():
            shutil.rmtree(target)
        os.replace(src / "dist", target)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ensure_web(root: Path, cache: Path, version: str) -> Path:
    found = find_web(root, cache, version)
    if found:
        return found
    target = cache / f"web-{version}"
    expected = committed_sha(version)
    if expected:
        with tempfile.TemporaryDirectory(dir=cache) as tmp:
            try:
                tarball = download(version, Path(tmp))
            except SetupError as e:
                reason = str(e)
            else:
                got = sha256(tarball)
                if got != expected:
                    raise SetupError(
                        f"the downloaded web app for v{version} doesn't match the checksum committed in the repo "
                        f"(got {got[:12]}…, expected {expected[:12]}…), so it wasn't used; tell the PR Prism "
                        "maintainer, or set PRR_WEB_DIST to a web app you built yourself")
                unpack(tarball, target)
                return target
    else:
        reason = f"there is no prebuilt web app for v{version} ({SHA_FILE.name} is missing or for another version)"
    if not shutil.which("npm") or not (root / "web" / "package.json").is_file():
        raise SetupError(f"{reason}, and Node isn't installed to build it; install Node 20+ (brew install node) "
                         "and run the command again")
    log(f"{reason}; building the web app with Node instead (a minute or two)")
    build_web(root, target)
    return target


# ---- Old versions ------------------------------------------------------------------------------------------------


def _commands() -> list[str] | None:
    """Every running process's command line, or None when `ps` doesn't work."""
    try:
        r = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.splitlines() if r.returncode == 0 else None


def prune(cache: Path, version: str) -> list[Path]:
    """Remove venv-*/web-* folders of other versions. A version a review server still runs is kept (its venv path is
    in the server's command line), with its web app; so is everything when `ps` can't tell."""
    old: dict[str, list[Path]] = {}
    for d in cache.iterdir() if cache.is_dir() else []:
        m = _VERSIONED.match(d.name)
        if not m or m.group(2) == version or not d.is_dir() or d.is_symlink():
            continue
        if (d / ("pyvenv.cfg" if m.group(1) == "venv" else "index.html")).exists():
            old.setdefault(m.group(2), []).append(d)
    if not old:
        return []
    running = _commands()
    if running is None:
        return []
    removed = []
    for v, dirs in old.items():
        venv = str(cache / f"venv-{v}") + "/"
        if any(venv in cmd for cmd in running):
            continue
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed


# ---- Entry -------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def locked(cache: Path) -> Iterator[None]:
    """One setup at a time: two `prr` commands started together on a new version wait for each other."""
    with (cache / ".setup.lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def setup(root: Path = ROOT, cache: Path | None = None) -> Path:
    """Make sure this version's venv and web app exist; return the venv's python."""
    cache = cache or cache_home()
    version = read_version(root)
    cache.mkdir(parents=True, exist_ok=True)
    with locked(cache):
        py = ensure_venv(root, cache, version)
        ensure_web(root, cache, version)
        prune(cache, version)
    return py


def main(argv: list[str]) -> int:
    try:
        py = setup()
    except SetupError as e:
        log(str(e))
        return 1
    # -P: don't put the cwd on sys.path, so a "prr" folder in the user's repo can't shadow this package.
    os.execv(py, [str(py), "-P", "-m", "prr", *argv])
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
