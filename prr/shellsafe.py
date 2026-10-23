"""Does a shell command only look at things? Then the chat runs it without asking the reviewer.

Claude Code already runs simple reads (`grep … | head`, `sed -n 10,20p f`) without asking. It still asks about
`awk`, `find`, `xargs`, `f=$(…)` and similar, even when they only read. This module answers "read-only?" for those,
and gives the keys that "Always allow in this chat" remembers.

When in doubt the answer is "no", and the reviewer is asked. Things that make it "no":
- any program not on the lists below, or a program given with a path (`./grep` could be a file from the PR);
- writing to a file (`>` anywhere except `/dev/null`), here-docs, backticks, `$(…)` inside a word;
- an environment variable set for a command (`GIT_EXTERNAL_DIFF=x git diff`), or a shell variable that changes
  where programs are found (`PATH=…`, zsh's `path=…`);
- options that write or run other programs (`sed -i`, `find -exec`, `sort -o`, `rg --pre`, awk's `system()`).
"""

from __future__ import annotations

import os
import re
import shlex

# Programs that only read, whatever their options.
_READ_ONLY = {
    "cat", "head", "tail", "wc", "cut", "tr", "echo", "printf", "ls", "file", "stat", "basename", "dirname",
    "realpath", "readlink", "nl", "column", "fold", "rev", "paste", "diff", "cmp", "comm", "grep", "egrep", "fgrep",
    "jq", "pwd", "cd", "true", "false", "test", "[", "which", "type", "strings", "od", "hexdump", "du", "seq",
    "sha256sum", "shasum", "md5", "md5sum", "exit",
}
_GIT_READ_ONLY = {
    "blame", "log", "show", "diff", "status", "grep", "rev-parse", "ls-files", "ls-tree", "cat-file", "shortlog",
    "describe", "merge-base", "rev-list", "name-rev", "show-ref", "for-each-ref", "whatchanged", "annotate",
}
_GH_READ_ONLY = {("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "list"), ("pr", "status"),
                 ("run", "view"), ("run", "list"), ("issue", "view"), ("issue", "list"), ("repo", "view")}
_SKIP_WORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "time"}
_END_WORDS = {"done", "fi", "}"}
# Longest first, so `)|` splits into `)` `|`, and `2>&1` keeps `>&`.
_OPERATORS = ["&>>", "<<<", ">>", "<<", "&&", "||", ";;", "|&", ">&", "<&", "&>", ">|", ";", "&", "|", "(", ")", "<", ">"]
_SEPARATORS = {";", ";;", "&", "&&", "||", "|", "|&", "(", ")"}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SAFE_VAR = re.compile(r"^[a-z_][a-z0-9_]*$")
_ZSH_PATH_VARS = {"path", "fpath", "cdpath", "manpath", "module_path"}  # zsh ties these to PATH and friends
_SED_WRITE_OR_RUN = re.compile(r"(?:^|[^A-Za-z_])[gpIiMm0-9]*[wWe](?=\s|$|[;}])")
_AWK_WRITE_OR_RUN = re.compile(r"\bsystem\s*\(|\bgetline\b|\bprintf?\b[^;}\n]*[>|]")


def _split_operators(tok: str) -> list[str] | None:
    out = []
    while tok:
        op = next((o for o in _OPERATORS if tok.startswith(o)), None)
        if op is None:
            return None
        out.append(op)
        tok = tok[len(op):]
    return out


def simple_commands(command: str) -> list[list[str]] | None:
    """The command split into simple commands (word lists), or None when it does something we don't follow."""
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        raw = list(lex)
    except ValueError:  # unbalanced quotes
        return None
    tokens: list[str] = []
    for tok in raw:
        if "`" in tok or "$(" in tok:
            return None  # a command hidden inside a word
        if tok and all(ch in "();<>|&" for ch in tok):
            ops = _split_operators(tok)
            if ops is None:
                return None
            tokens += ops
        else:
            tokens.append(tok)
    cmds, cur, i = [], [], 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok in _SEPARATORS:
            cmds.append(cur)
            cur = []
        elif tok in (">", ">>", "&>", "&>>", ">|"):
            if nxt != "/dev/null":
                return None  # writes a file
            i += 1
        elif tok == ">&":
            if nxt not in ("1", "2", "/dev/null"):
                return None
            i += 1
        elif tok in ("<", "<<<"):
            if nxt is None or nxt in _SEPARATORS or nxt in _OPERATORS:
                return None  # `<(…)` and friends
            i += 1
        elif tok in _OPERATORS:
            return None  # here-docs, `<&`
        else:
            cur.append(tok)
        i += 1
    cmds.append(cur)
    return [c for c in cmds if c]


def _words(argv: list[str]) -> list[str]:
    """Drop shell keywords around the real command (`do grep …`, `then cat …`)."""
    while argv and argv[0] in _SKIP_WORDS:
        argv = argv[1:]
    return argv


def _options(args: list[str]) -> list[str]:
    return [a for a in args if a.startswith("-")]


def _sed_ok(args: list[str]) -> bool:
    for a in args:
        if a.startswith("--in-place") or a in ("-f", "--file") or a.startswith("--file="):
            return False
        if a.startswith("-") and not a.startswith("--") and ("i" in a[1:] or "f" in a[1:]):
            return False
    return not any(_SED_WRITE_OR_RUN.search(a) for a in args if not a.startswith("-"))


def _awk_ok(args: list[str]) -> bool:
    if any(a.split("=")[0] in ("-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec") for a in args):
        return False
    return not any(_AWK_WRITE_OR_RUN.search(a) for a in args if not a.startswith("-"))


def _find_ok(args: list[str]) -> bool:
    return not any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls") or a.startswith("-fprint") for a in args)


def _xargs_ok(args: list[str]) -> bool:
    takes_value = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in takes_value else 1
    rest = args[i:]
    return not rest or _read_only(rest)


def _git_ok(args: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C":
            i += 2
        elif args[i] in ("--no-pager", "-P", "--no-optional-locks"):
            i += 1
        else:
            return False  # `-c key=value` and the like can run programs
    if i >= len(args) or args[i] not in _GIT_READ_ONLY:
        return False
    rest = args[i + 1:]
    return not any(a.startswith(("--output", "-O", "--open-files-in-pager")) for a in rest)


def _gh_ok(args: list[str]) -> bool:
    words = [a for a in args if not a.startswith("-")]
    return tuple(words[:2]) in _GH_READ_ONLY and not any(a in ("-w", "--web") for a in args)


_RULES = {
    "sed": _sed_ok,
    "awk": _awk_ok, "gawk": _awk_ok,
    "find": _find_ok,
    "xargs": _xargs_ok,
    "git": _git_ok,
    "gh": _gh_ok,
    "rg": lambda args: not any(a.startswith("--pre") for a in args),
    "sort": lambda args: not any(a.startswith(("--output", "--compress")) or re.match(r"^-[A-Za-z]*o", a) for a in args),
    "tree": lambda args: not any(a.startswith("-o") for a in args),
    "uniq": lambda args: len(args) == len(_options(args)),  # `uniq in out` writes `out`
}


def _read_only(argv: list[str]) -> bool:
    argv = _words(argv)
    if not argv or argv[0] in _END_WORDS or argv[0] == "for":
        return True  # `done`, or a `for x in a b` header (its words are only values)
    if all(_ASSIGN.match(a) for a in argv):  # `f=…`: a shell variable, not passed to programs
        return all(_SAFE_VAR.match(_ASSIGN.match(a).group(1)) and _ASSIGN.match(a).group(1) not in _ZSH_PATH_VARS
                   for a in argv)
    prog, args = argv[0], argv[1:]
    if prog in _READ_ONLY:
        return True
    rule = _RULES.get(prog)
    return bool(rule and rule(args))


def _path_is_safe() -> bool:
    """A relative PATH entry (`.`, `node_modules/.bin`) could pick up a program from the PR's files."""
    return all(p.startswith("/") for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def is_read_only(command: str) -> bool:
    cmds = simple_commands(command)
    return bool(cmds) and _path_is_safe() and all(_read_only(c) for c in cmds)


def _key(argv: list[str]) -> str:
    """`npm test`, `python3`, `awk`: the program, plus its sub-command when it has one."""
    if len(argv) > 1 and re.match(r"^[a-z][a-z0-9-]*$", argv[1]):
        return " ".join(argv[:2])
    return argv[0]


def approval_keys(command: str) -> list[str]:
    """What "Always allow in this chat" remembers for a command: one key per part that isn't read-only."""
    cmds = simple_commands(command)
    whole = " ".join(command.split())
    if cmds is None:
        return [whole]  # can't tell what it runs: only this exact command
    keys = []
    for c in cmds:
        words = _words(c)
        if words and not _read_only(words):
            k = _key(words)
            if k not in keys:
                keys.append(k)
    return keys or [whole]
