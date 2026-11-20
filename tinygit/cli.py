from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import objects as obj
from .diff import diffstat, unified
from .index import read_index
from .repo import GitError, Repo


def _fmt_date(ts: int, tz: str) -> str:
    sign = 1 if tz[0] == "+" else -1
    off = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    return datetime.fromtimestamp(ts, timezone(off)).strftime("%a %b %-d %H:%M:%S %Y ") + tz


def _blob_text(repo: Repo, sha: str | None) -> str:
    if sha is None:
        return ""
    data = repo.read(sha)[1]
    if b"\0" in data[:8000]:
        return None  # binary, same heuristic git uses
    return data.decode("utf-8", "replace")


def cmd_init(a):
    repo = Repo.init(a.path, a.branch)
    print(f"Initialized empty Git repository in {repo.gitdir}/")


def cmd_hash_object(a):
    data = Path(a.file).read_bytes()
    if a.write:
        print(Repo.find().write(a.type.encode(), data))
    else:
        print(obj.hash_object(a.type.encode(), data))


def cmd_cat_file(a):
    repo = Repo.find()
    kind, body = repo.read(repo.resolve(a.object))
    if a.t:
        print(kind.decode())
    elif a.s:
        print(len(body))
    elif kind == b"tree":
        for e in obj.decode_tree(body):
            t = "tree" if e.is_dir else ("commit" if e.mode == 0o160000 else "blob")
            print(f"{e.mode:06o} {t} {e.sha}\t{e.name}")
    else:
        sys.stdout.buffer.write(body)


def cmd_add(a):
    Repo.find().add(a.paths)


def cmd_rm(a):
    Repo.find().rm_cached(a.paths)


def cmd_ls_files(a):
    for path, e in read_index(Repo.find().index_path).items():
        print(f"{e.mode:06o} {e.sha} 0\t{path}" if a.stage else path)


def cmd_write_tree(a):
    print(Repo.find().write_tree())


def cmd_commit(a):
    repo = Repo.find()
    first = repo.head() is None
    sha = repo.commit(a.message)
    branch = (repo.head_target() or "HEAD").removeprefix("refs/heads/")
    root = " (root-commit)" if first else ""
    print(f"[{branch}{root} {sha[:7]}] {a.message.splitlines()[0]}")


def cmd_log(a):
    repo = Repo.find()
    for sha, c in repo.log(a.rev, a.n):
        if a.oneline:
            print(f"{sha[:7]} {c.subject}")
            continue
        who, ts, tz = obj.split_ident(c.author)
        print(f"commit {sha}")
        if len(c.parents) > 1:
            print("Merge: " + " ".join(p[:7] for p in c.parents))
        print(f"Author: {who}\nDate:   {_fmt_date(ts, tz)}\n")
        for line in c.message.rstrip("\n").splitlines():
            print("    " + line)
        print()


def cmd_status(a):
    repo = Repo.find()
    branch = repo.head_target()
    print(f"On branch {branch.removeprefix('refs/heads/')}" if branch
          else f"HEAD detached at {repo.head()[:7]}")
    st = repo.status()
    sections = [("Changes to be committed:", st["staged"]),
                ("Changes not staged for commit:", st["unstaged"]),
                ("Untracked files:", st["untracked"])]
    if not any(s for _, s in sections):
        print("nothing to commit, working tree clean")
    for title, items in sections:
        if items:
            print(f"\n{title}")
            for it in items:
                print(f"\t{it}")


def cmd_diff(a):
    repo = Repo.find()
    index = read_index(repo.index_path)
    if a.cached:
        head = repo.head()
        old = {p: s for p, (_, s) in repo.flatten_tree(repo.read_commit(head).tree).items()} if head else {}
        new = {p: e.sha for p, e in index.items()}
        pairs = [(p, old.get(p), new.get(p)) for p in sorted(old.keys() | new.keys())
                 if old.get(p) != new.get(p)]
        load_new = lambda p, s: _blob_text(repo, s)
    else:
        pairs = []
        for p, e in index.items():
            f = repo.root / p
            if not f.exists():
                pairs.append((p, e.sha, None))
            elif not repo._looks_clean(e, f.lstat()) and obj.hash_object(b"blob", f.read_bytes()) != e.sha:
                pairs.append((p, e.sha, "worktree"))
        def load_new(p, s):
            if s is None:
                return ""
            data = (repo.root / p).read_bytes()
            return None if b"\0" in data[:8000] else data.decode("utf-8", "replace")

    total_add = total_del = 0
    for path, old_sha, new_sha in pairs:
        a_text, b_text = _blob_text(repo, old_sha), load_new(path, new_sha)
        if a.stat:
            if a_text is None or b_text is None:
                print(f" {path} | Bin")
                continue
            add, rem = diffstat(a_text, b_text)
            total_add, total_del = total_add + add, total_del + rem
            print(f" {path} | {add + rem:>3} {'+' * min(add, 40)}{'-' * min(rem, 40)}")
            continue
        print(f"diff --git a/{path} b/{path}")
        if a_text is None or b_text is None:
            print(f"Binary files a/{path} and b/{path} differ")
            continue
        sys.stdout.write(unified(a_text, b_text,
                                 f"a/{path}" if old_sha else "/dev/null",
                                 f"b/{path}" if new_sha else "/dev/null"))
    if a.stat and pairs:
        print(f" {len(pairs)} file{'s' * (len(pairs) != 1)} changed, "
              f"{total_add} insertions(+), {total_del} deletions(-)")


def cmd_branch(a):
    repo = Repo.find()
    if a.name:
        repo.create_branch(a.name, a.start)
        return
    current = (repo.head_target() or "").removeprefix("refs/heads/")
    for name, sha in repo.branches().items():
        print(f"{'*' if name == current else ' '} {name:<20} {sha[:7]}")


def cmd_checkout(a):
    repo = Repo.find()
    if a.b:
        repo.create_branch(a.target)
    sha = repo.checkout(a.target)
    print(f"Switched to {'branch ' + repr(a.target) if repo.head_target() else 'detached HEAD ' + sha[:7]}")


def cmd_rev_parse(a):
    print(Repo.find().resolve(a.rev))


def cmd_fsck(a):
    repo = Repo.find()
    t = time.perf_counter()
    n, problems = repo.fsck()
    for p in problems:
        print("error:", p)
    packed = sum(len(p.idx) for p in repo.packs)
    print(f"checked {n} reachable objects ({packed} in {len(repo.packs)} pack(s)) "
          f"in {time.perf_counter() - t:.2f}s, {len(problems)} problem(s)")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tinygit", description="git, the hard way")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("path", nargs="?", default=".")
    s.add_argument("-b", "--branch", default="main"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("hash-object"); s.add_argument("file"); s.add_argument("-w", dest="write", action="store_true")
    s.add_argument("-t", dest="type", default="blob"); s.set_defaults(fn=cmd_hash_object)
    s = sub.add_parser("cat-file"); s.add_argument("object")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-t", action="store_true"); g.add_argument("-s", action="store_true")
    g.add_argument("-p", action="store_true"); s.set_defaults(fn=cmd_cat_file)
    s = sub.add_parser("add"); s.add_argument("paths", nargs="+"); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("rm", help="unstage (always --cached)"); s.add_argument("paths", nargs="+"); s.set_defaults(fn=cmd_rm)
    s = sub.add_parser("ls-files"); s.add_argument("-s", dest="stage", action="store_true"); s.set_defaults(fn=cmd_ls_files)
    s = sub.add_parser("write-tree"); s.set_defaults(fn=cmd_write_tree)
    s = sub.add_parser("commit"); s.add_argument("-m", dest="message", required=True); s.set_defaults(fn=cmd_commit)
    s = sub.add_parser("log"); s.add_argument("rev", nargs="?", default="HEAD")
    s.add_argument("-n", type=int); s.add_argument("--oneline", action="store_true"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("diff"); s.add_argument("--cached", "--staged", action="store_true")
    s.add_argument("--stat", action="store_true"); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("branch"); s.add_argument("name", nargs="?"); s.add_argument("start", nargs="?", default="HEAD")
    s.set_defaults(fn=cmd_branch)
    s = sub.add_parser("checkout"); s.add_argument("target"); s.add_argument("-b", action="store_true")
    s.set_defaults(fn=cmd_checkout)
    s = sub.add_parser("rev-parse"); s.add_argument("rev"); s.set_defaults(fn=cmd_rev_parse)
    s = sub.add_parser("fsck"); s.set_defaults(fn=cmd_fsck)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except GitError as e:
        print(f"fatal: {e}", file=sys.stderr)
        return 128
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
