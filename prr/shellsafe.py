"""Does a shell command only look at this PR? Then the chat runs it without asking the reviewer.

The chat's PreToolUse hook (prr/policy.py) sends every shell command here. A command runs without a card when every
program in it only reads, and everything it reads is inside the scope: the PR's worktree and the PR folder, minus
the review server's own files (server.json holds the login token). This module also gives the keys that "Always
allow in this chat" remembers.

When in doubt the answer is "no", and the reviewer is asked. Things that make it "no":
- any program not on the lists below, or a program given with a path (`./grep` could be a file from the PR);
- writing to a file (`>` anywhere except `/dev/null`), here-docs, backticks, `$(…)` inside quotes;
- an environment variable set for a command (`GIT_EXTERNAL_DIFF=x git diff`), or a shell variable that changes
  where programs are found (`PATH=…`, zsh's `path=…`);
- options that write or run other programs (`sed -i`, `find -exec`, `sort -o`, `rg --pre`, awk's `system()`).

And, so a command can't read outside the PR (`_paths_in_roots`):
- a path argument (after splitting `--opt=value` and `-f/path`) or a `<` file that resolves outside the scope;
  relative paths resolve against the shell's folder and any `cd` earlier in the command;
- `~`, `$VAR` or `${…}` (unless the variable was set in the same command from in-scope values or a file search);
- `cd`, `pushd`, `popd` with no folder, `-`, or a folder outside the scope; a shell already outside the scope;
- `xargs` fed by anything but a file search (`find`, `git ls-files`, `grep -l`, `rg -l`/`--files`) or a file;
- options that read file names from a file (`--files0-from`, `xargs -a`, `file -f`), sed's `r` command;
- `gh` naming another repo (`-R`, `--repo`, a URL);
- printing the environment (`printenv`, `env`, `set`, `export -p`, `jq env`, awk's `ENVIRON`, `ps e`).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

# Programs that only read, whatever their options.
_READ_ONLY = {
    "cat", "head", "tail", "wc", "cut", "tr", "echo", "printf", "ls", "file", "stat", "basename", "dirname",
    "realpath", "readlink", "nl", "column", "fold", "rev", "paste", "diff", "cmp", "comm", "grep", "egrep", "fgrep",
    "pwd", "cd", "true", "false", "test", "[", "which", "type", "strings", "od", "hexdump", "du", "seq",
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
_SEPARATORS = {";", ";;", "&", "&&", "||", "|", "|&"}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SAFE_VAR = re.compile(r"^[a-z_][a-z0-9_]*$")
_ZSH_PATH_VARS = {"path", "fpath", "cdpath", "manpath", "module_path"}  # zsh ties these to PATH and friends
_SED_WRITE_OR_RUN = re.compile(r"(?:^|[^A-Za-z_])[gpIiMm0-9]*[wWe](?=\s|$|[;}])")
_SED_READ = re.compile(r"(?:^|[^A-Za-z_\\])[rR](?![A-Za-z])")  # `1r file`, `/x/R file`
_AWK_WRITE_OR_RUN = re.compile(r"\bsystem\s*\(|\bgetline\b|\bprintf?\b[^;}\n]*[>|]|\bARGV\b")  # ARGV can name other files
_JQ_ENV = re.compile(r"\benv\b|\$ENV\b|\$__loc__")
_SUB = re.compile(r"\x00(\d+)\x00")  # stands for one $(…) inside a word
_ALWAYS_FINE = {"/dev/null", "/dev/stdin", "-"}
# Where credentials live. Outside the known readers, a program's words are only scanned for these
# (`python3 -c "open('~/.ssh/id_rsa')"`).
SECRET_PLACES = ("~/.ssh", "~/.config/gh", "~/.aws", "~/.kube", "~/.docker", "~/.netrc")
SECRET_HINT = re.compile(r"\.ssh\b|\.config/gh\b|\.aws\b|\.netrc\b|\.docker/config\.json|\.kube\b")
_GITHUB_URL = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)", re.I)


@dataclass(frozen=True)
class Scope:
    """Where commands may read: inside the roots, except the private files and any folder that holds one."""

    roots: tuple[str, ...]
    private: tuple[str, ...] = ()
    repo: str = ""  # owner/repo of the PR: the only repo `gh` may read without asking

    @classmethod
    def of(cls, roots: list, private: list | tuple = (), repo: str = "") -> Scope:
        return cls(tuple(os.path.realpath(str(r)) for r in roots), tuple(os.path.realpath(str(p)) for p in private),
                   repo.lower())

    def contains(self, path: str) -> bool:
        p = os.path.realpath(path)
        return any(_under(p, r) for r in self.roots) and not any(_under(f, p) for f in self.private)


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@dataclass
class _Cmd:
    words: list[str]
    after: str = ""                                    # the operator before it: "", ";", "&&", "|", "(" …
    reads: list[str] = field(default_factory=list)     # files given with `<`
    sub: int | None = None                             # the $(…) it runs in (numbered in order)


def _split_operators(tok: str) -> list[str] | None:
    out = []
    while tok:
        op = next((o for o in _OPERATORS if tok.startswith(o)), None)
        if op is None:
            return None
        out.append(op)
        tok = tok[len(op):]
    return out


def _tokens(command: str) -> list[str] | None:
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        raw = list(lex)
    except ValueError:  # unbalanced quotes
        return None
    tokens: list[str] = []
    for tok in raw:
        if "`" in tok or "$(" in tok or "\x00" in tok:
            return None  # a command hidden inside a quoted word
        if tok and all(ch in "();<>|&" for ch in tok):
            ops = _split_operators(tok)
            if ops is None:
                return None
            tokens += ops
        else:
            tokens.append(tok)
    return tokens


def _parse(command: str) -> list[_Cmd] | None:
    """The command as simple commands in order; None when it does something we don't follow. A `$(…)` inside a word
    is replaced by a marker (\\x00n\\x00), and the commands inside it carry `sub=n`."""
    tokens = _tokens(command)
    if tokens is None:
        return None
    cmds: list[_Cmd] = []
    subs = 0
    stack: list[tuple[_Cmd | None, int | None]] = []  # (the command a $(…) belongs to, or None for `( … )`; outer sub)
    cur, sub = _Cmd([]), None

    def finish(c: _Cmd) -> None:
        if c.words or c.reads:
            cmds.append(c)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok == "(":
            if cur.words and cur.words[-1].endswith("$"):  # $( … ): its output becomes part of this word
                cur.words[-1] = f"{cur.words[-1][:-1]}\x00{subs}\x00"
                stack.append((cur, sub))
                sub, subs = subs, subs + 1
                cur = _Cmd([], "(", sub=sub)
            else:  # ( … ): a subshell
                finish(cur)
                stack.append((None, sub))
                cur = _Cmd([], "(", sub=sub)
        elif tok == ")":
            finish(cur)
            if not stack:
                return None
            outer, sub = stack.pop()
            cur = outer if outer is not None else _Cmd([], ")", sub=sub)
        elif tok in _SEPARATORS:
            finish(cur)
            cur = _Cmd([], tok, sub=sub)
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
            if tok == "<":
                cur.reads.append(nxt)
            i += 1
        elif tok in _OPERATORS:
            return None  # here-docs, `<&`
        else:
            cur.words.append(tok)
        i += 1
    finish(cur)
    if stack:
        return None  # an unclosed `(`
    return cmds


def simple_commands(command: str) -> list[list[str]] | None:
    """The command split into simple commands (word lists), or None when it does something we don't follow."""
    parsed = _parse(command)
    return None if parsed is None else [c.words for c in parsed if c.words]


def _words(argv: list[str]) -> list[str]:
    """Drop shell keywords around the real command (`do grep …`, `then cat …`)."""
    while argv and argv[0] in _SKIP_WORDS:
        argv = argv[1:]
    return argv


def _options(args: list[str]) -> list[str]:
    return [a for a in args if a.startswith("-")]


def _expansions(command: str) -> list[str]:
    """Variables the shell expands ($x, ${x}), outside single quotes. "{" stands for a form we don't follow."""
    names, quote, i = [], "", 0
    while i < len(command):
        c = command[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1  # the next character is literal
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif c == "$":
            rest = command[i + 1:]
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rest) or re.match(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", rest)
            if m:
                names.append(m.group(m.lastindex or 0))
            elif rest[:1] in ("{", "'"):
                names.append("{")  # ${x:-…}, ${!x}, $'…'
            # `$(` is a command (checked like any other); `$?`, `$1`, `$$` … are the shell's own values
        i += 1
    return names


# --------------------------------------------------------------------------------------
# Per-program rules: does it only read?
# --------------------------------------------------------------------------------------


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


def _xargs_split(args: list[str]) -> tuple[list[str], list[str]]:
    """xargs' own options, and the command it runs."""
    takes_value = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in takes_value else 1
    return args[:i], args[i:]


def _xargs_ok(args: list[str]) -> bool:
    rest = _xargs_split(args)[1]
    return not rest or _read_only(rest)


def _git_split(args: list[str]) -> tuple[list[str], list[str]] | None:
    """git's own options (only -C and harmless ones), and the sub-command with its arguments."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C":
            i += 2
        elif args[i] in ("--no-pager", "-P", "--no-optional-locks"):
            i += 1
        else:
            return None  # `-c key=value` and the like can run programs
    return args[:i], args[i:]


def _git_ok(args: list[str]) -> bool:
    split = _git_split(args)
    if split is None or not split[1] or split[1][0] not in _GIT_READ_ONLY:
        return False
    return not any(a.startswith(("--output", "-O", "--open-files-in-pager")) for a in split[1][1:])


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
    "jq": lambda args: not any(re.search(r"\b(import|include)\b", a) for a in args if not a.startswith("-")),
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


# --------------------------------------------------------------------------------------
# Per-program rules: which arguments name files?
# --------------------------------------------------------------------------------------

_NO_FILE_ARGS = {"echo", "printf", "tr", "seq", "true", "false", "pwd", "basename", "dirname", "exit", "which", "type",
                 "cd", "pushd", "popd", "gh"}
_GREP_VALUES = {"-A", "-B", "-C", "-m", "-d", "-D", "-e", "-f", "--context", "--after-context", "--before-context",
                "--max-count", "--regexp", "--file", "--include", "--exclude", "--exclude-dir", "--exclude-from",
                "--label", "--devices", "--directories", "--binary-files"}
# Options whose value is given separately or glued (`-A 3`, `-A3`).
_VALUES = {
    "grep": _GREP_VALUES,
    "rg": {"-A", "-B", "-C", "-m", "-e", "-f", "-g", "-t", "-T", "-M", "-j", "-r", "-E", "-d", "--glob", "--iglob",
           "--type", "--type-not", "--type-add", "--max-columns", "--threads", "--replace", "--encoding",
           "--max-depth", "--max-count", "--context", "--after-context", "--before-context", "--regexp", "--file",
           "--ignore-file", "--sort", "--sortr", "--pre-glob", "--path-separator", "--max-filesize", "--colors",
           "--context-separator", "--field-match-separator", "--field-context-separator"},
    "sed": {"-e", "-f", "-l", "--expression", "--file", "--line-length"},
    "awk": {"-F", "-v", "-f", "-e", "--field-separator", "--assign", "--file", "--source"},
    "jq": {"-f", "-L", "--from-file", "--indent"},
    "cut": {"-d", "-f", "-c", "-b", "--delimiter", "--fields", "--characters", "--bytes", "--output-delimiter"},
    "sort": {"-t", "-k", "-S", "-T", "--field-separator", "--key", "--buffer-size", "--temporary-directory",
             "--parallel"},
    "head": {"-n", "-c", "--lines", "--bytes"}, "tail": {"-n", "-c", "--lines", "--bytes"},
    "column": {"-s", "-c", "-t"}, "paste": {"-d", "--delimiters"},
    "diff": {"-x", "-X", "-I", "-F", "-U", "-C", "-L", "--exclude", "--label"},
    "git": {"-L", "-n", "-S", "-G", "--format", "--pretty", "--since", "--until", "--author", "--grep"},  # sub-commands
}
# Values that are patterns, scripts or separators, not files.
_NOT_FILES = {
    "grep": {"-e", "--regexp", "--include", "--exclude", "--exclude-dir", "--label"},
    "rg": {"-e", "--regexp", "-g", "--glob", "--iglob", "-r", "--replace", "--path-separator", "--context-separator"},
    "sed": {"-e", "--expression"}, "awk": {"-F", "-v", "-e", "--field-separator", "--assign", "--source"},
    "cut": {"-d", "--delimiter", "--output-delimiter"}, "sort": {"-t", "--field-separator"}, "column": {"-s"},
    "paste": {"-d", "--delimiters"}, "diff": {"-x", "--exclude", "-I", "-F", "-L", "--label"},
    "git": {"-L", "-S", "-G", "--format", "--pretty", "--since", "--until", "--author", "--grep"},
}
# The first operand is a pattern or script, unless one of these options gives it.
_PATTERN_FIRST = {"grep": {"-e", "--regexp", "-f", "--file"}, "rg": {"-e", "--regexp", "-f", "--file"},
                  "sed": {"-e", "--expression", "-f", "--file"}, "awk": {"-f", "--file", "-e", "--source"},
                  "jq": {"-f", "--from-file"}}
for _alias, _prog in (("egrep", "grep"), ("fgrep", "grep"), ("gawk", "awk")):
    _VALUES[_alias], _NOT_FILES[_alias], _PATTERN_FIRST[_alias] = _VALUES[_prog], _NOT_FILES[_prog], _PATTERN_FIRST[_prog]
_NAMES_FROM_FILE = ("--files0-from", "--files-from", "--arg-file")  # file names come from inside a file


def _scan(prog: str, args: list[str]) -> list[tuple[str, str]]:
    """Each argument as (option, value): ("", operand), ("-f", value) for an option's value (`-f x`, `-fx`,
    `--file=x`), or ("-", path) for a path glued to options we don't know (`-q/etc/passwd`)."""
    values = _VALUES.get(prog, set())
    out: list[tuple[str, str]] = []
    i, operands_only = 0, False
    while i < len(args):
        a = args[i]
        if operands_only or a == "-" or not a.startswith("-"):
            out.append(("", a))
        elif a == "--":
            operands_only = True
        elif a.startswith("--"):
            name, eq, value = a.partition("=")
            if not eq and name in values and i + 1 < len(args):
                eq, value = "=", args[i + 1]
                i += 1
            if eq:
                out.append((name, value))
        else:  # -abc, -A3, -A 3, -f/path
            for j, ch in enumerate(a[1:], 1):
                if "-" + ch in values:
                    value = a[j + 1:]
                    if not value and i + 1 < len(args):
                        value = args[i + 1]
                        i += 1
                    out.append(("-" + ch, value))
                    break
            else:
                m = re.search(r"[/~.$\x00]", a[1:])
                if m:
                    out.append(("-", a[1 + m.start():]))
        i += 1
    return out


def _file_args(prog: str, args: list[str]) -> list[str]:
    """The arguments of one command that may name files: `--opt=value` and glued `-f/path` are split first, and
    the program's pattern, script or separators are left out."""
    if prog in _NO_FILE_ARGS:
        return []
    if prog == "git":
        split = _git_split(args)
        if split is None or not split[1]:
            return []
        own, (sub, *rest) = split
        dirs = [own[i + 1] for i, a in enumerate(own[:-1]) if a == "-C"]
        return dirs + _file_args("grep" if sub == "grep" else "git-sub", rest)
    if prog == "xargs":
        rest = _xargs_split(args)[1]
        return _file_args(rest[0], rest[1:]) if rest else []
    if prog == "find":  # start folders, and files named by tests; -name/-path patterns aren't files
        starts = []
        for a in args:
            if a.startswith(("-", "(", "!", ")")):
                break
            starts.append(a)
        return starts + [args[i + 1] for i, a in enumerate(args[:-1])
                         if a in ("-newer", "-anewer", "-cnewer", "-samefile", "-files0-from")]
    key = "git" if prog == "git-sub" else prog
    items = _scan(key, args)
    not_files = _NOT_FILES.get(key, set())
    pattern_opts = _PATTERN_FIRST.get(key)
    pattern_given = pattern_opts is None or any(opt in pattern_opts for opt, _ in items)
    out = []
    for opt, value in items:
        if opt == "" and not pattern_given:
            pattern_given = True  # the pattern or script
        elif opt not in not_files:
            out.append(value)
    return out


def _producer(words: list[str]) -> bool:
    """A file search: every line it prints is a file name under the folders it was given."""
    prog, args = words[0], words[1:]
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if prog == "find":
        return not any(a in ("-printf", "-fprintf", "-ls", "-fls") for a in args)
    if prog in ("grep", "egrep", "fgrep"):
        return "l" in short or "L" in short or any(a in ("--files-with-matches", "--files-without-match") for a in args)
    if prog == "rg":
        return (("l" in short or any(a in ("--files", "--files-with-matches", "--files-without-match") for a in args))
                and not any(a.startswith("--path-separator") for a in args))
    if prog == "git":
        split = _git_split(args)
        if split is None or not split[1]:
            return False
        sub, rest = split[1][0], split[1][1:]
        if sub == "ls-files":
            return not any(a.startswith("--format") for a in rest)
        if sub == "diff":
            return any(a in ("--name-only", "--name-status") for a in rest)
        if sub == "grep":
            return any(a in ("-l", "-L", "--name-only", "--files-with-matches", "--files-without-match") for a in rest)
    return False


def _filter(words: list[str]) -> bool:
    """A step that only keeps some of the lines it is given (so file names stay file names)."""
    prog, args = words[0], words[1:]
    if prog in ("head", "tail"):
        return all(a.startswith("-") or a.isdigit() for a in args)
    if prog in ("sort", "uniq"):
        return all(a.startswith("-") for a in args)
    if prog in ("grep", "egrep", "fgrep"):
        opts = _options(args)
        if any(o in ("-o", "--only-matching", "-r", "-R", "--recursive") or
               (not o.startswith("--") and any(ch in o[1:] for ch in "orR")) for o in opts):
            return False
        return len(_file_args(prog, args)) == 0
    return False


# --------------------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------------------


@dataclass
class Report:
    read_only: bool                                     # every program in it only reads
    outside: list[str] = field(default_factory=list)    # why it may read outside the scope, in plain words
    env: list[str] = field(default_factory=list)        # why it may print environment variables
    paths: list[str] = field(default_factory=list)      # the outside paths it names, resolved
    # Outside paths given to programs that aren't plain readers (`rm -rf /tmp/x`, `node /tmp/x.js`): those programs
    # always ask anyway, and with ask_before_actions = false they aren't reads this module can judge.
    elsewhere: list[str] = field(default_factory=list)


def secret_place(path: str) -> bool:
    """Inside (or one of) the folders and files that hold credentials: ~/.ssh, ~/.config/gh, ~/.aws, …"""
    p = os.path.realpath(os.path.expanduser(path))
    return any(_under(p, os.path.realpath(os.path.expanduser(s))) for s in SECRET_PLACES)


def _env_dump(words: list[str]) -> str:
    prog, args = words[0], words[1:]
    if prog == "printenv":
        return "prints environment variables"
    if prog == "env":
        rest = [a for a in args if not a.startswith("-") and not _ASSIGN.match(a)]
        return "" if rest else "prints environment variables"
    if prog == "set" and not args:
        return "prints shell and environment variables"
    if prog in ("export", "declare", "typeset", "readonly") and all(a.startswith("-") for a in args):
        return "prints environment variables"
    if prog == "compgen" and any(a in ("-e", "-v") for a in args):
        return "lists environment variables"
    if prog == "ps" and any("e" in a or "E" in a for a in args if not a.startswith("--")):
        return "shows other programs' environment variables"
    if prog == "jq" and any(_JQ_ENV.search(a) for a in args):
        return "prints environment variables (jq env)"
    if prog in ("awk", "gawk") and any("ENVIRON" in a for a in args):
        return "prints environment variables (awk ENVIRON)"
    return ""


def _gh_repos(words: list[str]) -> list[str]:
    """Repos a `gh` command names (GH_REPO=, -R/--repo, a URL, `repos/o/r`, or `gh repo view o/r`)."""
    out = []
    for i, a in enumerate(words):
        if a.startswith("GH_REPO="):
            out.append(a.split("=", 1)[1])
        elif a in ("-R", "--repo") and i + 1 < len(words):
            out.append(words[i + 1])
        elif a.startswith("--repo="):
            out.append(a.split("=", 1)[1])
        elif a.startswith("-R") and len(a) > 2:
            out.append(a[2:])
        elif m := _GITHUB_URL.search(a):
            out.append(f"{m.group(1)}/{m.group(2)}")
        elif m := re.match(r"^/?repos/([\w.-]+)/([\w.-]+)", a):
            out.append(f"{m.group(1)}/{m.group(2)}")
    plain = [a for a in words[words.index("gh") + 1:] if not a.startswith("-")] if "gh" in words else []
    if plain[:2] == ["repo", "view"] and len(plain) > 2:
        out.append(plain[2])
    return out


def analyze(command: str, scope: Scope, cwd: str) -> Report:
    """Does it only read, and does it stay inside the scope? Every reason is a short plain-language phrase."""
    cmds = _parse(command)
    if cmds is None:  # can't follow it: it asks anyway; with ask_before_actions = false only the obvious is blocked
        rep = Report(read_only=False)
        if SECRET_HINT.search(command):
            rep.outside.append("mentions a folder that holds secrets")
        if re.search(r"\b(printenv|ENVIRON)\b", command):
            rep.env.append("prints environment variables")
        return rep
    rep = Report(read_only=all(_read_only(c.words) for c in cmds if c.words))

    def outside(reason: str, path: str = "") -> None:
        if reason not in rep.outside:
            rep.outside.append(reason)
        if path and path not in rep.paths:
            rep.paths.append(path)

    def pipeline_before(c: _Cmd) -> list[_Cmd]:
        same = [x for x in cmds if x.sub == c.sub]
        k = next(i for i, x in enumerate(same) if x is c)
        chain = []
        while k > 0 and same[k].after == "|":
            k -= 1
            chain.insert(0, same[k])
        return chain

    def searches(chain: list[_Cmd]) -> bool:
        words = [_words(x.words) for x in chain]
        return (bool(chain) and all(words) and not any(x.reads for x in chain) and _producer(words[0])
                and all(_filter(w) for w in words[1:]) and all(x.after == "|" for x in chain[1:]))

    def sub_is_search(n: int) -> bool:
        part = [c for c in cmds if c.sub == n]
        return bool(part) and part[0].after == "(" and searches(part)

    def value_ok(value: str, cwds: list[str]) -> bool:
        """A variable's value: in-scope literal words, or exactly one file search's output."""
        found = _SUB.findall(value)
        if found:
            return _SUB.fullmatch(value) is not None and sub_is_search(int(found[0]))
        if value.startswith("~") or "$" in value:
            return False
        return all(scope.contains(os.path.join(d, value)) for d in cwds) if value else True

    real_cwd = os.path.realpath(cwd)
    cwds = [real_cwd]
    if not scope.contains(real_cwd):
        outside("runs in a folder outside this PR", real_cwd)

    safe_vars: dict[str, bool] = {}
    for c in cmds:
        words = _words(c.words)
        prog = words[0] if words else ""
        if prog in ("cd", "pushd"):
            target = next((a for a in words[1:] if a == "-" or not a.startswith("-")), None)
            if target is None or target == "-":
                outside(f"{prog} without a folder inside this PR")
            elif target.startswith("~") or "$" in target or _SUB.search(target):
                outside(f"{prog} to a folder given by ~, a variable or a command")
            else:
                for d in list(cwds):
                    new = os.path.realpath(os.path.join(d, target))
                    if not scope.contains(new):
                        outside(f"changes to {target}, outside this PR", new)
                    elif new not in cwds:
                        cwds.append(new)
            continue
        if prog == "popd":
            outside("popd (goes back to a folder we can't see)")
            continue
        if prog == "for" and len(words) >= 3 and words[2] == "in":
            for v in words[3:]:
                safe_vars[words[1]] = safe_vars.get(words[1], True) and value_ok(v, cwds)
            continue
        if words and all(_ASSIGN.match(a) for a in words):
            for a in words:
                name, value = a.split("=", 1)
                safe_vars[name] = safe_vars.get(name, True) and value_ok(value, cwds)
            continue
        assigned = words
        while words and _ASSIGN.match(words[0]):  # `X=1 cmd …`: the command after the assignments
            words = words[1:]
        if words[:1] == ["gh"]:
            for repo in _gh_repos(assigned):
                if repo.lower() != scope.repo:
                    outside(f"reads another GitHub repo ({repo})")
        reader = ""
        if words:
            prog, args = words[0], words[1:]
            reader = prog
            if reason := _env_dump(words):
                rep.env.append(reason)
            if prog == "xargs":
                own, inner = _xargs_split(args)
                reader = inner[0] if inner else ""
                if any(a in ("-a", "--arg-file") or a.startswith(("-a", "--arg-file=")) for a in own):
                    outside("xargs reads file names from a file")
                elif c.reads or not searches(pipeline_before(c)):
                    outside("xargs gets file names from something other than a file search")
                if inner and (reason := _env_dump(inner)):
                    rep.env.append(reason)
            if any(a.split("=")[0] in _NAMES_FROM_FILE for a in args) or (prog == "file" and "-f" in args):
                outside(f"{prog} reads file names from a file")
            if prog == "sed" and any(_SED_READ.search(a) for a in args if not a.startswith("-")):
                outside("sed reads a file named in its script")
            if prog not in _READ_ONLY and prog not in _RULES and SECRET_HINT.search(" ".join(args)):
                outside("mentions a folder that holds secrets")
        known = reader in _READ_ONLY or reader in _RULES
        named = [(cand, known) for cand in (_file_args(words[0], words[1:]) if words else [])]
        for cand, by_reader in named + [(r, True) for r in c.reads]:
            if cand in _ALWAYS_FINE:
                continue
            found = _SUB.findall(cand)
            if found:
                if not (_SUB.fullmatch(cand) and sub_is_search(int(found[0]))):
                    outside("uses another command's output as a file name")
                continue
            if "$" in cand:
                continue  # checked below, with the variables
            if re.search(r"\{[^}]*(,|\.\.)[^}]*\}", cand):
                outside(f"reads {cand} ({{…}} can name other files)")
                continue
            fulls = ([os.path.realpath(os.path.expanduser(cand))] if cand.startswith("~") else
                     [os.path.realpath(os.path.join(d, cand)) for d in cwds])
            for full in fulls:
                if scope.contains(full) and not cand.startswith("~"):
                    continue
                if by_reader or secret_place(full) or any(_under(f, full) for f in scope.private):
                    outside(f"reads {cand}", full)
                elif full not in rep.elsewhere:
                    rep.elsewhere.append(full)
    for name in _expansions(command):
        if name == "{":
            outside("uses ${…}, which could point outside this PR")
        elif not safe_vars.get(name, False):
            outside(f"uses ${name}, which could point outside this PR or hold a secret")
    return rep


def _paths_in_roots(command: str, scope: Scope, cwd: str) -> bool:
    """True when nothing the command names can be outside the scope, and it doesn't print the environment."""
    rep = analyze(command, scope, cwd)
    return not rep.outside and not rep.env and not rep.elsewhere


def is_read_only(command: str, scope: Scope, cwd: str) -> bool:
    rep = analyze(command, scope, cwd)
    return rep.read_only and not rep.outside and not rep.env and _path_is_safe()


def _key(argv: list[str]) -> str:
    """`npm test`, `python3`, `awk`: the program, plus its sub-command when it has one."""
    if len(argv) > 1 and re.match(r"^[a-z][a-z0-9-]*$", argv[1]):
        return " ".join(argv[:2])
    return argv[0]


def approval_keys(command: str, scope: Scope | None = None, cwd: str = "") -> list[str]:
    """What "Always allow in this chat" remembers for a command: one key per part that isn't read-only. A command
    that reads outside the scope or prints the environment is only ever allowed again exactly as it was."""
    cmds = simple_commands(command)
    whole = " ".join(command.split())
    if cmds is None:
        return [whole]  # can't tell what it runs: only this exact command
    if scope is not None:
        rep = analyze(command, scope, cwd)
        if rep.outside or rep.env or rep.elsewhere:
            return [whole]
    keys = []
    for c in cmds:
        words = _words(c)
        if words and not _read_only(words):
            k = _key(words)
            if k not in keys:
                keys.append(k)
    return keys or [whole]
