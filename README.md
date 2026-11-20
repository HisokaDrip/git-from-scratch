# git-from-scratch

`tinygit` is git's storage layer rewritten in plain Python (stdlib only, no
GitPython, no shelling out). It reads and writes the *real* on-disk format, so
the repos it makes are normal git repos, and it can read repos that real git
made, packfiles and all.

I wanted to actually understand what's inside `.git/` instead of just knowing
the commands. The rule I set myself: everything has to be byte-compatible
with git, and the tests check that by running real git against our output.

What's in here:

- **objects** - blob/tree/commit framing, SHA-1 ids, zlib loose objects. Tree
  entries use git's odd sort order (directories compare as `name/`), which is
  the first thing you get wrong.
- **packfiles** - idx v2 binary search via the fanout table, pack v2 parsing,
  both `OFS_DELTA` and `REF_DELTA`, delta instruction decoding, 64-bit offsets.
  Base objects are cached by offset because gc'd repos have delta chains 50 deep.
- **the index** - `.git/index` v2 read/write with real stat data, so git trusts
  our stat cache and vice versa. Handles the "racy git" case (a file modified in
  the same timestamp tick the index was written).
- **myers diff** - the O(ND) greedy algorithm with backtracking, unified hunks.
- **porcelain** - `init add rm commit status diff [--cached] [--stat] log branch
  checkout [-b] rev-parse cat-file hash-object ls-files write-tree fsck`, with
  `HEAD~2^2`-style revisions, short shas, packed-refs and a subset of `.gitignore`.

## running it

Needs Python 3.10+. Nothing to install, but there's a pyproject if you want the
`tinygit` command on your path:

```
pip install -e .           # or just: python -m tinygit ...
python -m pytest -q        # needs real git on PATH for the interop tests
```

## a session

Made with tinygit, then checked with real git:

```
$ tinygit init .
Initialized empty Git repository in /tmp/demo/.git/
$ tinygit add . && tinygit commit -m "first commit"
[main (root-commit) c0a8804] first commit
$ tinygit status
On branch main

Changes not staged for commit:
	modified:   src/hello.py
$ tinygit diff
diff --git a/src/hello.py b/src/hello.py
--- a/src/hello.py
+++ b/src/hello.py
@@ -1,2 +1,2 @@
 def greet(name):
-    return "hi " + name
+    return f"hey {name}"  # f-strings
$ tinygit add src && tinygit commit -m "use an f-string"
[main 497ed6d] use an f-string
$ git fsck --strict          # real git, no complaints
$ git log --stat --oneline
497ed6d use an f-string
 src/hello.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
c0a8804 first commit
 README.md    | 1 +
 src/hello.py | 2 ++
 2 files changed, 3 insertions(+)
$ tinygit checkout -b experiment
Switched to branch 'experiment'
```

And going the other way, reading a fresh clone of
[psf/requests](https://github.com/psf/requests) (one big pack, ~27k objects):

```
$ python tools/crosscheck.py /tmp/requests
log:     6496 commits, same order and subjects (0.17s)
objects: 26885 identical to git cat-file (1.47s)
status:  agrees (clean)

$ tinygit fsck
checked 25682 reachable objects (26885 in 1 pack(s)) in 1.18s, 0 problem(s)
```

`crosscheck.py` works on any checkout, so try it on your own repos. If it
finds a disagreement I'd like to know about it. That's how I found that git's
`%s` subject keeps double spaces but trims each line (see `Commit.subject`).

## tests

30 tests, most of them checked against real git:

- blob hashes vs `git hash-object`, tree encoding vs `git mktree`
- a repo with 40 commits of a slowly mutating file, repacked with
  `--depth=50` once with offset deltas and once with ref deltas; every object
  has to come back byte-identical
- we read the index git writes (with its TREE extension) and git reads ours:
  `git status --porcelain` is empty after a tinygit commit, `git write-tree`
  gives the same tree sha
- myers: the kept-line count must equal the LCS from a DP table (so the diff
  is minimal), and 40 random patches have to apply with `git apply`
- branch/checkout round trips, refusing to clobber local changes, corrupted
  objects caught by fsck

## things it doesn't do

No network (clone/fetch/push), no merges or conflict stages, no writing
packs, no `**` in gitignore, no SHA-256 repos. Annotated tags get peeled
when you resolve them but there's no `tag` command.

## what i'd do next

Writing packfiles is the obvious next step. Finding good delta bases is the
interesting part (git's sliding-window heuristic). After that, the smart HTTP
protocol so `tinygit clone` can talk to GitHub directly. Both feel doable now
that reading works.
