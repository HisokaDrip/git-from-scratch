"""A repository: object database, refs, and the porcelain built on top."""
from __future__ import annotations

import fnmatch
import heapq
import os
import re
import time
import zlib
from pathlib import Path

from . import objects as obj
from .index import IndexEntry, read_index, write_index
from .objects import Commit, TreeEntry
from .pack import Pack


class GitError(Exception):
    pass


class Repo:
    def __init__(self, worktree: Path):
        self.root = Path(worktree).resolve()
        self.gitdir = self.root / ".git"
        self._packs: list[Pack] | None = None

    # --- setup ---------------------------------------------------------------

    @classmethod
    def init(cls, path: str | Path, branch: str = "main") -> "Repo":
        repo = cls(Path(path))
        g = repo.gitdir
        if g.exists():
            raise GitError(f"{g} already exists")
        for d in ("objects/info", "objects/pack", "refs/heads", "refs/tags"):
            (g / d).mkdir(parents=True)
        (g / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (g / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
            "\tbare = false\n\tlogallrefupdates = true\n"
        )
        (g / "description").write_text("Unnamed repository; edit this file to name it.\n")
        return repo

    @classmethod
    def find(cls, start: str | Path = ".") -> "Repo":
        p = Path(start).resolve()
        for cand in (p, *p.parents):
            if (cand / ".git").is_dir():
                return cls(cand)
        raise GitError("not a git repository (or any of the parent directories)")

    # --- object database -----------------------------------------------------

    def _loose_path(self, sha: str) -> Path:
        return self.gitdir / "objects" / sha[:2] / sha[2:]

    @property
    def packs(self) -> list[Pack]:
        if self._packs is None:
            self._packs = [Pack(p, resolve_external=self._read_loose)
                           for p in sorted((self.gitdir / "objects/pack").glob("*.idx"))]
        return self._packs

    def _read_loose(self, sha: str):
        p = self._loose_path(sha)
        return obj.decompress_loose(p.read_bytes()) if p.exists() else None

    def read(self, sha: str) -> tuple[bytes, bytes]:
        got = self._read_loose(sha)
        if got is None:
            for pack in self.packs:
                got = pack.get(sha)
                if got:
                    break
        if got is None:
            raise GitError(f"object {sha} not found")
        kind, body = got
        if obj.hash_object(kind, body) != sha:
            raise GitError(f"object {sha} is corrupt (hash mismatch)")
        return kind, body

    def write(self, kind: bytes, body: bytes) -> str:
        sha = obj.hash_object(kind, body)
        p = self._loose_path(sha)
        if not p.exists() and not any(pk.idx.find(bytes.fromhex(sha)) is not None for pk in self.packs):
            p.parent.mkdir(exist_ok=True)
            tmp = p.parent / f"tmp_{os.getpid()}_{sha[2:8]}"
            tmp.write_bytes(obj.compress(kind, body))
            tmp.chmod(0o444)  # git's objects are read-only, match it
            os.replace(tmp, p)
        return sha

    def read_commit(self, sha: str) -> Commit:
        kind, body = self.read(sha)
        if kind == b"tag":  # peel annotated tags
            headers, _ = obj.parse_headers(body)
            return self.read_commit(dict(headers)["object"])
        if kind != b"commit":
            raise GitError(f"{sha} is a {kind.decode()}, not a commit")
        return Commit.decode(body)

    def read_tree(self, sha: str) -> list[TreeEntry]:
        kind, body = self.read(sha)
        if kind != b"tree":
            raise GitError(f"{sha} is not a tree")
        return obj.decode_tree(body)

    def flatten_tree(self, sha: str | None, prefix: str = "") -> dict[str, tuple[int, str]]:
        if sha is None:
            return {}
        out = {}
        for e in self.read_tree(sha):
            path = prefix + e.name
            if e.is_dir:
                out.update(self.flatten_tree(e.sha, path + "/"))
            elif e.mode != 0o160000:  # skip submodule gitlinks
                out[path] = (e.mode, e.sha)
        return out

    # --- refs ----------------------------------------------------------------

    def _packed_refs(self) -> dict[str, str]:
        f = self.gitdir / "packed-refs"
        refs = {}
        if f.exists():
            for line in f.read_text().splitlines():
                if line and line[0] not in "#^":
                    sha, name = line.split(" ", 1)
                    refs[name] = sha
        return refs

    def read_ref(self, name: str, depth: int = 0) -> str | None:
        if depth > 5:
            raise GitError(f"symref loop at {name}")
        f = self.gitdir / name
        if f.is_file():
            val = f.read_text().strip()
            if val.startswith("ref: "):
                return self.read_ref(val[5:], depth + 1)
            return val
        return self._packed_refs().get(name)

    def update_ref(self, name: str, sha: str) -> None:
        f = self.gitdir / name
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".lock")
        tmp.write_text(sha + "\n")
        os.replace(tmp, f)

    def head_target(self) -> str | None:
        """refs/heads/<branch> HEAD points at, or None when detached."""
        val = (self.gitdir / "HEAD").read_text().strip()
        return val[5:] if val.startswith("ref: ") else None

    def head(self) -> str | None:
        return self.read_ref("HEAD")

    def branches(self) -> dict[str, str]:
        out = {k[11:]: v for k, v in self._packed_refs().items() if k.startswith("refs/heads/")}
        heads = self.gitdir / "refs/heads"
        for f in heads.rglob("*"):
            if f.is_file():
                out[f.relative_to(heads).as_posix()] = f.read_text().strip()
        return dict(sorted(out.items()))

    def resolve(self, rev: str) -> str:
        m = re.fullmatch(r"(.+?)((?:[~^]\d*)*)", rev)
        base, suffix = m.group(1), m.group(2)
        sha = None
        for cand in (base, f"refs/heads/{base}", f"refs/tags/{base}", f"refs/remotes/{base}"):
            if cand == base and not cand.isupper() and not cand.startswith("refs/"):
                continue
            sha = self.read_ref(cand)
            if sha:
                break
        if sha is None and re.fullmatch(r"[0-9a-f]{4,40}", base):
            sha = self._expand_prefix(base)
        if sha is None:
            raise GitError(f"unknown revision {rev!r}")

        for op, num in re.findall(r"([~^])(\d*)", suffix):
            n = int(num) if num else 1
            if op == "~":
                for _ in range(n):
                    sha = self._parent(sha, 0, rev)
            else:
                sha = self._parent(sha, n - 1, rev) if n else sha
        return sha

    def _parent(self, sha, i, rev):
        parents = self.read_commit(sha).parents
        if i >= len(parents):
            raise GitError(f"{rev}: commit {sha[:7]} has no parent #{i + 1}")
        return parents[i]

    def _expand_prefix(self, prefix: str) -> str | None:
        hits = set()
        d = self.gitdir / "objects" / prefix[:2]
        if d.is_dir():
            hits |= {prefix[:2] + f.name for f in d.iterdir() if (prefix[:2] + f.name).startswith(prefix)}
        for pk in self.packs:
            hits |= set(pk.idx.prefix_matches(prefix))
        if len(hits) > 1:
            raise GitError(f"short sha {prefix} is ambiguous")
        return hits.pop() if hits else None

    # --- working tree --------------------------------------------------------

    def _ignore_rules(self) -> list[tuple[str, bool, bool, bool]]:
        rules = []
        f = self.root / ".gitignore"
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                neg = line.startswith("!")
                line = line.lstrip("!")
                dir_only = line.endswith("/")
                line = line.rstrip("/")
                anchored = "/" in line
                rules.append((line.lstrip("/"), neg, dir_only, anchored))
        return rules

    def is_ignored(self, rel: str, is_dir: bool, rules=None) -> bool:
        # a subset of gitignore semantics: globs, !negation, trailing / for
        # dirs, and anchoring when the pattern has a slash. no ** yet.
        rules = self._ignore_rules() if rules is None else rules
        ignored = False
        name = rel.rsplit("/", 1)[-1]
        for pat, neg, dir_only, anchored in rules:
            if dir_only and not is_dir:
                continue
            target = rel if anchored else name
            if fnmatch.fnmatchcase(target, pat):
                ignored = not neg
        return ignored

    def walk(self) -> list[str]:
        rules = self._ignore_rules()
        files = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = sorted(
                d for d in dirnames
                if d != ".git" and not self.is_ignored(rel_dir + d, True, rules)
            )
            for fn in filenames:
                rel = rel_dir + fn
                if not self.is_ignored(rel, False, rules):
                    files.append(rel)
        return sorted(files)

    def _hash_file(self, rel: str, write: bool) -> tuple[str, os.stat_result]:
        p = self.root / rel
        st = p.lstat()
        data = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
        sha = self.write(b"blob", data) if write else obj.hash_object(b"blob", data)
        return sha, st

    @property
    def index_path(self) -> Path:
        return self.gitdir / "index"

    def _looks_clean(self, e: IndexEntry, st: os.stat_result) -> bool:
        # the "racy git" problem: a file edited within the same timestamp tick
        # as the index was written has matching stat data but new contents.
        # git's fix is to distrust any entry whose mtime isn't strictly older
        # than the index file itself, so we do the same.
        if not e.stat_matches(st):
            return False
        try:
            idx = self.index_path.stat()
        except FileNotFoundError:
            return False
        return (e.mtime_s, e.mtime_ns) < (int(idx.st_mtime), idx.st_mtime_ns % 1_000_000_000)

    def add(self, paths: list[str]) -> list[str]:
        index = read_index(self.index_path)
        all_files = self.walk()
        touched = []
        for raw in paths:
            rel = Path(os.path.abspath(raw)).relative_to(self.root).as_posix()
            rel = "" if rel == "." else rel
            under = lambda f: not rel or f == rel or f.startswith(rel + "/")
            matched = [f for f in all_files if under(f)]
            gone = [f for f in index if under(f) and not (self.root / f).exists()]
            if not matched and not gone:
                raise GitError(f"pathspec {raw!r} did not match any files")
            for f in matched:
                old = index.get(f)
                st = (self.root / f).lstat()
                if old and self._looks_clean(old, st):
                    continue
                sha, st = self._hash_file(f, write=True)
                index[f] = IndexEntry.from_stat(f, sha, st)
                touched.append(f)
            for f in gone:
                del index[f]
                touched.append(f)
        write_index(self.index_path, index)
        return touched

    def rm_cached(self, paths: list[str]) -> None:
        index = read_index(self.index_path)
        for p in paths:
            index.pop(p, None)
        write_index(self.index_path, index)

    def write_tree(self, index: dict[str, IndexEntry] | None = None) -> str:
        index = read_index(self.index_path) if index is None else index
        root: dict = {}
        for path, e in index.items():
            *dirs, leaf = path.split("/")
            node = root
            for d in dirs:
                node = node.setdefault(d, {})
            node[leaf] = e

        def build(node: dict) -> str:
            entries = []
            for name, val in node.items():
                if isinstance(val, dict):
                    entries.append(TreeEntry(0o40000, name, build(val)))
                else:
                    entries.append(TreeEntry(val.mode, name, val.sha))
            return self.write(b"tree", obj.encode_tree(entries))

        return build(root)

    # --- config / identity ---------------------------------------------------

    def config(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in (Path.home() / ".gitconfig", self.gitdir / "config"):
            if not f.exists():
                continue
            section = ""
            for line in f.read_text().splitlines():
                line = line.split("#")[0].strip()
                if m := re.fullmatch(r"\[\s*([\w.-]+)(?:\s+\"(.*)\")?\s*\]", line):
                    section = m.group(1).lower() + (f".{m.group(2)}" if m.group(2) else "")
                elif "=" in line and section:
                    k, v = (s.strip() for s in line.split("=", 1))
                    out[f"{section}.{k.lower()}"] = v.strip('"')
        return out

    def ident(self, role: str) -> str:
        cfg = self.config()
        name = os.environ.get(f"GIT_{role}_NAME") or cfg.get("user.name") or "tinygit"
        email = os.environ.get(f"GIT_{role}_EMAIL") or cfg.get("user.email") or "tinygit@localhost"
        date = os.environ.get(f"GIT_{role}_DATE")
        if date:
            ts, tz = date.split()
        else:
            now = time.time()
            off = time.localtime(now).tm_gmtoff // 60
            sign = "+" if off >= 0 else "-"
            ts, tz = str(int(now)), f"{sign}{abs(off) // 60:02d}{abs(off) % 60:02d}"
        return f"{name} <{email}> {ts} {tz}"

    # --- porcelain -----------------------------------------------------------

    def commit(self, message: str) -> str:
        tree = self.write_tree()
        parent = self.head()
        if parent and self.read_commit(parent).tree == tree:
            raise GitError("nothing to commit, working tree clean")
        if not message.endswith("\n"):
            message += "\n"
        c = Commit(tree, [parent] if parent else [], self.ident("AUTHOR"),
                   self.ident("COMMITTER"), message)
        sha = self.write(b"commit", c.encode())
        self.update_ref(self.head_target() or "HEAD", sha)
        return sha

    def log(self, start: str = "HEAD", limit: int | None = None):
        """Every ancestor, newest committer date first (like `git log`)."""
        tip = self.resolve(start)
        seen = {tip}
        c = self.read_commit(tip)
        # ties on timestamp go to whoever was queued first, which is what
        # git's date-order does too. a counter keeps the heap from comparing shas.
        counter = 0
        heap = [(-obj.split_ident(c.committer)[1], counter, tip, c)]
        n = 0
        while heap and (limit is None or n < limit):
            _, _, sha, c = heapq.heappop(heap)
            yield sha, c
            n += 1
            for p in c.parents:
                if p not in seen:
                    seen.add(p)
                    pc = self.read_commit(p)
                    counter += 1
                    heapq.heappush(heap, (-obj.split_ident(pc.committer)[1], counter, p, pc))

    def status(self) -> dict[str, list[str]]:
        head = self.head()
        head_files = self.flatten_tree(self.read_commit(head).tree) if head else {}
        index = read_index(self.index_path)
        files = set(self.walk())

        staged, unstaged, untracked = [], [], []
        for path in sorted(set(head_files) | set(index)):
            h, i = head_files.get(path), index.get(path)
            if h is None:
                staged.append(f"new file:   {path}")
            elif i is None:
                staged.append(f"deleted:    {path}")
            elif (i.mode, i.sha) != h:
                staged.append(f"modified:   {path}")

        dirty_index = False
        for path, e in index.items():
            p = self.root / path
            if not p.exists() and not p.is_symlink():
                unstaged.append(f"deleted:    {path}")
                continue
            st = p.lstat()
            if self._looks_clean(e, st):
                continue
            sha, st = self._hash_file(path, write=False)
            if sha != e.sha or e.mode != IndexEntry.from_stat(path, sha, st).mode:
                unstaged.append(f"modified:   {path}")
            else:
                # contents identical, just a touched mtime. refresh the cached
                # stat like git does so the next status is fast again.
                index[path] = IndexEntry.from_stat(path, sha, st)
                dirty_index = True
        if dirty_index:
            write_index(self.index_path, index)

        untracked = sorted(files - set(index))
        return {"staged": staged, "unstaged": sorted(unstaged), "untracked": untracked}

    def create_branch(self, name: str, start: str = "HEAD") -> str:
        if not re.fullmatch(r"[\w./-]+", name) or ".." in name or name.endswith((".lock", "/")):
            raise GitError(f"{name!r} is not a valid branch name")
        if self.read_ref(f"refs/heads/{name}"):
            raise GitError(f"branch {name!r} already exists")
        sha = self.resolve(start)
        self.update_ref(f"refs/heads/{name}", sha)
        return sha

    def checkout(self, target: str) -> str:
        st = self.status()
        if st["staged"] or st["unstaged"]:
            raise GitError("you have local changes; commit them first")

        is_branch = self.read_ref(f"refs/heads/{target}") is not None
        sha = self.resolve(target)
        old = self.flatten_tree(self.read_commit(self.head()).tree) if self.head() else {}
        new = self.flatten_tree(self.read_commit(sha).tree)

        untracked = set(st["untracked"])
        clobber = [p for p in new if p in untracked]
        if clobber:
            raise GitError(f"checkout would overwrite untracked files: {', '.join(clobber)}")

        for path in old.keys() - new.keys():
            (self.root / path).unlink()
            # tidy up directories we just emptied
            d = (self.root / path).parent
            while d != self.root and not any(d.iterdir()):
                d.rmdir()
                d = d.parent

        index = {}
        for path, (mode, blob) in new.items():
            p = self.root / path
            if old.get(path) != (mode, blob):
                p.parent.mkdir(parents=True, exist_ok=True)
                _, data = self.read(blob)
                if p.is_symlink() or p.exists():
                    p.unlink()
                if mode == 0o120000:
                    os.symlink(data.decode(), p)
                else:
                    p.write_bytes(data)
                    p.chmod(0o755 if mode == 0o100755 else 0o644)
            index[path] = IndexEntry.from_stat(path, blob, p.lstat())
        write_index(self.index_path, index)

        head = self.gitdir / "HEAD"
        head.write_text(f"ref: refs/heads/{target}\n" if is_branch else sha + "\n")
        return sha

    def fsck(self) -> tuple[int, list[str]]:
        """Re-hash every reachable object from every branch. returns (count, problems)."""
        problems, seen = [], set()
        stack = list(self.branches().values())
        if self.head():
            stack.append(self.head())
        while stack:
            sha = stack.pop()
            if sha in seen:
                continue
            seen.add(sha)
            try:
                kind, body = self.read(sha)
            except GitError as e:
                problems.append(str(e))
                continue
            except zlib.error as e:
                problems.append(f"{sha}: bad zlib stream ({e})")
                continue
            if kind == b"commit":
                c = Commit.decode(body)
                stack += [c.tree, *c.parents]
            elif kind == b"tree":
                stack += [e.sha for e in obj.decode_tree(body) if e.mode != 0o160000]
        return len(seen), problems
