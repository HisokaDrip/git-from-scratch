"""Object model: blobs, trees, commits, tags and their canonical encodings.

Everything here is pure bytes-in / bytes-out so the same parsers work whether
the object came from a loose file or got rebuilt out of a packfile delta chain.
"""
from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field

TYPES = (b"blob", b"tree", b"commit", b"tag")


def frame(kind: bytes, body: bytes) -> bytes:
    return kind + b" " + str(len(body)).encode() + b"\0" + body


def hash_object(kind: bytes, body: bytes) -> str:
    return hashlib.sha1(frame(kind, body)).hexdigest()


def compress(kind: bytes, body: bytes) -> bytes:
    return zlib.compress(frame(kind, body), 1)


def decompress_loose(raw: bytes) -> tuple[bytes, bytes]:
    data = zlib.decompress(raw)
    header, _, body = data.partition(b"\0")
    kind, size = header.split(b" ")
    if int(size) != len(body):
        raise ValueError(f"object size mismatch: header says {int(size)}, got {len(body)}")
    if kind not in TYPES:
        raise ValueError(f"unknown object type {kind!r}")
    return kind, body


# --- trees -------------------------------------------------------------------

@dataclass(frozen=True)
class TreeEntry:
    mode: int
    name: str
    sha: str

    @property
    def is_dir(self) -> bool:
        return self.mode == 0o40000


def _tree_sort_key(e: TreeEntry) -> bytes:
    # git compares directory names as if they had a trailing slash, which is
    # why "foo.txt" sorts before the directory "foo" ... but after "foo-bar".
    # get this wrong and real git calls your tree corrupt.
    name = e.name.encode()
    return name + b"/" if e.is_dir else name


def encode_tree(entries: list[TreeEntry]) -> bytes:
    out = bytearray()
    for e in sorted(entries, key=_tree_sort_key):
        out += f"{e.mode:o} {e.name}".encode() + b"\0" + bytes.fromhex(e.sha)
    return bytes(out)


def decode_tree(body: bytes) -> list[TreeEntry]:
    entries, i = [], 0
    while i < len(body):
        sp = body.index(b" ", i)
        nul = body.index(b"\0", sp)
        mode = int(body[i:sp], 8)
        name = body[sp + 1:nul].decode("utf-8", "surrogateescape")
        sha = body[nul + 1:nul + 21].hex()
        entries.append(TreeEntry(mode, name, sha))
        i = nul + 21
    return entries


# --- commits (and tags, same header format) ----------------------------------

@dataclass
class Commit:
    tree: str
    parents: list[str]
    author: str
    committer: str
    message: str
    extra: list[tuple[str, str]] = field(default_factory=list)  # gpgsig, encoding, ...

    def encode(self) -> bytes:
        lines = [f"tree {self.tree}"]
        lines += [f"parent {p}" for p in self.parents]
        lines += [f"author {self.author}", f"committer {self.committer}"]
        for k, v in self.extra:
            # multi-line header values continue with a leading space
            lines.append(f"{k} " + v.replace("\n", "\n "))
        return ("\n".join(lines) + "\n\n" + self.message).encode()

    @classmethod
    def decode(cls, body: bytes) -> "Commit":
        headers, message = parse_headers(body)
        get = lambda k: next((v for hk, v in headers if hk == k), "")
        known = {"tree", "parent", "author", "committer"}
        return cls(
            tree=get("tree"),
            parents=[v for k, v in headers if k == "parent"],
            author=get("author"),
            committer=get("committer"),
            message=message,
            extra=[(k, v) for k, v in headers if k not in known],
        )


def parse_headers(body: bytes) -> tuple[list[tuple[str, str]], str]:
    text = body.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    headers: list[tuple[str, str]] = []
    for line in head.split("\n"):
        if line.startswith(" ") and headers:
            k, v = headers[-1]
            headers[-1] = (k, v + "\n" + line[1:])
        elif line:
            k, _, v = line.partition(" ")
            headers.append((k, v))
    return headers, message


def split_ident(ident: str) -> tuple[str, int, str]:
    """'Name <mail> 1700000000 +0530' -> ('Name <mail>', 1700000000, '+0530')"""
    who, ts, tz = ident.rsplit(" ", 2)
    return who, int(ts), tz
