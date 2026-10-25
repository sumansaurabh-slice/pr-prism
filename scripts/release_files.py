#!/usr/bin/env python3
"""The files scripts/release.sh makes. Standard library only.

  release_files.py pack <web/dist> <out/web-dist.tar.gz>   the web app as a reproducible tarball, plus <tarball>.sha256
  release_files.py stamp <version> <tarball.sha256>        commit-ready: skills/pr-prism/web-dist.sha256 and the
                                                           version in .claude-plugin/plugin.json
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHA_FILE = Path("skills/pr-prism/web-dist.sha256")
PLUGIN_JSON = Path(".claude-plugin/plugin.json")


def pack(dist: Path, out: Path) -> str:
    """Tar dist's contents (index.html at the top) with fixed owners, modes and times, so the same build gives the
    same bytes. Writes `<sha256>  <name>` to <out>.sha256 and returns the sha256."""
    if not (dist / "index.html").is_file():
        raise SystemExit(f"{dist} has no index.html; build the web app first")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for p in sorted(dist.rglob("*")):
            info = tf.gettarinfo(str(p), arcname=p.relative_to(dist).as_posix())
            if not (info.isfile() or info.isdir()):
                raise SystemExit(f"{p}: only plain files and folders can go in the web app")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if info.isdir() else 0o644
            if info.isfile():
                with p.open("rb") as fh:
                    tf.addfile(info, fh)
            else:
                tf.addfile(info)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n")
    return digest


def stamp(version: str, sha_file: Path, root: Path = ROOT) -> None:
    """Write the checksum bin/prr checks downloads against, and set plugin.json's version (only that line changes)."""
    digest = sha_file.read_text().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(f"{sha_file} doesn't hold a sha256")
    (root / SHA_FILE).write_text(
        f"# The sha256 of web-dist.tar.gz on the GitHub release v{version}, written by scripts/release.sh.\n"
        "# bin/prr (bootstrap.py) uses a download only when it matches.\n"
        f"version={version}\nsha256={digest}\n")
    path = root / PLUGIN_JSON
    text, count = re.subn(r'("version"\s*:\s*")[^"]*(")', rf"\g<1>{version}\g<2>", path.read_text(), count=1)
    if count != 1 or json.loads(text).get("version") != version:
        raise SystemExit(f"couldn't set the version in {path}")
    path.write_text(text)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "pack":
        print(pack(Path(argv[1]), Path(argv[2])))
        return 0
    if len(argv) == 3 and argv[0] == "stamp":
        stamp(argv[1], Path(argv[2]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
