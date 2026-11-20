"""The staging area: .git/index, version 2, byte-compatible with real git.

Stat data matters here. git uses ctime/mtime/size/ino to skip rehashing files
that haven't changed, so if we write zeros real git ends up re-reading every
file on the next `git status` (correct, just slow). we store the real values.
"""
from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

_ENTRY = struct.Struct(">10I20sH")  # 62 bytes before the path


@dataclass
class IndexEntry:
    path: str
    sha: str
    mode: int
    size: int
    mtime_s: int = 0
    mtime_ns: int = 0
    ctime_s: int = 0
    ctime_ns: int = 0
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0
    flags: int = 0  # stage bits etc. we only ever write stage 0

    @classmethod
    def from_stat(cls, path: str, sha: str, st: os.stat_result) -> "IndexEntry":
        return cls(
            path=path, sha=sha, mode=file_mode(st), size=st.st_size & 0xFFFFFFFF,
            mtime_s=int(st.st_mtime) & 0xFFFFFFFF, mtime_ns=st.st_mtime_ns % 1_000_000_000,
            ctime_s=int(st.st_ctime) & 0xFFFFFFFF, ctime_ns=st.st_ctime_ns % 1_000_000_000,
            dev=st.st_dev & 0xFFFFFFFF, ino=st.st_ino & 0xFFFFFFFF,
            uid=st.st_uid & 0xFFFFFFFF, gid=st.st_gid & 0xFFFFFFFF,
        )

    def stat_matches(self, st: os.stat_result) -> bool:
        return (self.size == st.st_size & 0xFFFFFFFF
                and self.mtime_s == int(st.st_mtime) & 0xFFFFFFFF
                and self.mtime_ns == st.st_mtime_ns % 1_000_000_000
                and self.mode == file_mode(st))


def file_mode(st: os.stat_result) -> int:
    import stat as stat_mod
    if stat_mod.S_ISLNK(st.st_mode):
        return 0o120000
    # git only tracks one bit of permission info: executable or not
    return 0o100755 if st.st_mode & 0o111 else 0o100644


def read_index(path: Path) -> dict[str, IndexEntry]:
    if not path.exists():
        return {}
    data = path.read_bytes()
    if hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise ValueError("index checksum mismatch")
    sig, version, count = struct.unpack(">4sII", data[:12])
    if sig != b"DIRC" or version not in (2, 3):
        raise ValueError(f"unsupported index (sig={sig!r}, version={version})")

    entries: dict[str, IndexEntry] = {}
    pos = 12
    for _ in range(count):
        (cs, cns, ms, mns, dev, ino, mode, uid, gid, size,
         sha, flags) = _ENTRY.unpack_from(data, pos)
        hdr = _ENTRY.size
        if version == 3 and flags & 0x4000:
            hdr += 2  # extended flags (intent-to-add, skip-worktree)
        name_end = data.index(b"\0", pos + hdr)
        name = data[pos + hdr:name_end].decode("utf-8", "surrogateescape")
        # entries are NUL padded out to a multiple of 8, always at least one NUL
        entry_len = (name_end - pos + 8) & ~7
        pos += entry_len
        if (flags >> 12) & 3:
            continue  # merge conflict stages; not handling merges
        entries[name] = IndexEntry(name, sha.hex(), mode, size, ms, mns, cs, cns,
                                   dev, ino, uid, gid, flags & 0xF000)
    # anything between here and the trailer is extensions (TREE, REUC, ...).
    # they're optional caches, safe to drop when we rewrite the file.
    return entries


def write_index(path: Path, entries: dict[str, IndexEntry]) -> None:
    out = bytearray(struct.pack(">4sII", b"DIRC", 2, len(entries)))
    # git sorts the index by raw path bytes, unlike trees (see objects.py)
    for name in sorted(entries, key=lambda p: p.encode("utf-8", "surrogateescape")):
        e = entries[name]
        raw = name.encode("utf-8", "surrogateescape")
        flags = (e.flags & 0xB000) | min(len(raw), 0xFFF)
        start = len(out)
        out += _ENTRY.pack(e.ctime_s, e.ctime_ns, e.mtime_s, e.mtime_ns, e.dev,
                           e.ino, e.mode, e.uid, e.gid, e.size,
                           bytes.fromhex(e.sha), flags)
        out += raw
        out += b"\0" * (8 - (len(out) - start) % 8)
    out += hashlib.sha1(out).digest()
    tmp = path.with_suffix(".lock")
    tmp.write_bytes(out)
    os.replace(tmp, path)
