"""Point this at any real git checkout and it diffs tinygit against git itself.

    python tools/crosscheck.py ~/src/some-repo

Checks: commit order + subjects from `log`, every object in the repo
byte-for-byte via `git cat-file --batch`, and that `status` agrees on a
clean tree. Exits non-zero on the first disagreement.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tinygit.repo import Repo  # noqa: E402


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, **kw).stdout


def main(path):
    repo = Repo.find(path)
    t0 = time.perf_counter()
    ours = [(sha, c.subject) for sha, c in repo.log()]
    t_log = time.perf_counter() - t0
    theirs = [tuple(line.split(" ", 1)) if " " in line else (line, "")
              for line in git(repo.root, "log", "--format=%H %s").decode().splitlines()]
    if ours != theirs:
        bad = next(i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b)
        sys.exit(f"log differs at entry {bad}: {ours[bad]} vs {theirs[bad]}")
    print(f"log:     {len(ours)} commits, same order and subjects ({t_log:.2f}s)")

    listing = git(repo.root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split()
    batch = git(repo.root, "cat-file", "--batch", input=b"\n".join(listing) + b"\n")
    t0, pos = time.perf_counter(), 0
    for sha in listing:
        header_end = batch.index(b"\n", pos)
        _, kind, size = batch[pos:header_end].split()
        body = batch[header_end + 1:header_end + 1 + int(size)]
        pos = header_end + 1 + int(size) + 1
        got_kind, got = repo.read(sha.decode())
        if (got_kind, got) != (kind, body):
            sys.exit(f"object {sha.decode()} differs")
    print(f"objects: {len(listing)} identical to git cat-file ({time.perf_counter() - t0:.2f}s)")

    clean = git(repo.root, "status", "--porcelain").strip() == b""
    st = repo.status()
    if clean != (not st["staged"] and not st["unstaged"] and not st["untracked"]):
        sys.exit(f"status disagrees: git clean={clean}, tinygit={st}")
    print(f"status:  agrees ({'clean' if clean else 'dirty'})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
