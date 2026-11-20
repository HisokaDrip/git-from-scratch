"""Read-only packfile support (pack v2 + idx v2), including both delta kinds.

Real repos are mostly packed after a clone or `git gc`, so without this you
can only read repos you made yourself. Format notes live in git's
Documentation/gitformat-pack.txt; the comments below are the bits that
tripped me up.
"""
from __future__ import annotations

import bisect
import struct
import zlib
from pathlib import Path

OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG = 1, 2, 3, 4
OBJ_OFS_DELTA, OBJ_REF_DELTA = 6, 7
TYPE_NAMES = {OBJ_COMMIT: b"commit", OBJ_TREE: b"tree", OBJ_BLOB: b"blob", OBJ_TAG: b"tag"}


class PackIndex:
    def __init__(self, path: Path):
        data = path.read_bytes()
        if data[:4] != b"\377tOc" or struct.unpack(">I", data[4:8])[0] != 2:
            raise ValueError(f"{path.name}: only idx v2 is supported")
        self.fanout = struct.unpack(">256I", data[8:8 + 1024])
        n = self.fanout[-1]
        pos = 8 + 1024
        self.shas = [data[pos + i * 20: pos + i * 20 + 20] for i in range(n)]
        pos += 20 * n + 4 * n  # skip the crc32 table, we trust zlib's adler
        offsets = struct.unpack(f">{n}I", data[pos:pos + 4 * n])
        pos += 4 * n
        # offsets with the MSB set point into a table of 8-byte offsets,
        # only used for packs over 2GB. cheap enough to support anyway.
        big = data[pos:-40]
        self.offsets = [
            struct.unpack(">Q", big[(o & 0x7FFFFFFF) * 8:(o & 0x7FFFFFFF) * 8 + 8])[0]
            if o & 0x80000000 else o
            for o in offsets
        ]
        self.by_offset = dict(zip(self.offsets, self.shas))

    def __len__(self) -> int:
        return len(self.shas)

    def find(self, sha: bytes) -> int | None:
        lo = self.fanout[sha[0] - 1] if sha[0] else 0
        hi = self.fanout[sha[0]]
        i = bisect.bisect_left(self.shas, sha, lo, hi)
        if i < hi and self.shas[i] == sha:
            return self.offsets[i]
        return None

    def prefix_matches(self, prefix: str) -> list[str]:
        out = []
        for s in self.shas:
            h = s.hex()
            if h.startswith(prefix):
                out.append(h)
        return out


def apply_delta(base: bytes, delta: bytes) -> bytes:
    def varint(pos: int) -> tuple[int, int]:
        val = shift = 0
        while True:
            b = delta[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return val, pos

    src_size, pos = varint(0)
    dst_size, pos = varint(pos)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            # copy from base: bits 0-3 say which offset bytes follow,
            # bits 4-6 which size bytes. missing bytes are zero.
            off = size = 0
            for i in range(4):
                if op & (1 << i):
                    off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            out += base[off:off + (size or 0x10000)]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0 is reserved")
    if len(out) != dst_size:
        raise ValueError("delta produced wrong size")
    return bytes(out)


class Pack:
    def __init__(self, idx_path: Path, resolve_external=None):
        self.idx = PackIndex(idx_path)
        self.data = idx_path.with_suffix(".pack").read_bytes()
        if self.data[:4] != b"PACK":
            raise ValueError("not a packfile")
        version, count = struct.unpack(">II", self.data[4:12])
        if version not in (2, 3) or count != len(self.idx):
            raise ValueError("pack/idx mismatch")
        # REF_DELTA bases may live outside this pack (thin packs); let the repo
        # hand us a lookup for those.
        self.resolve_external = resolve_external
        self._cache: dict[int, tuple[bytes, bytes]] = {}

    def get(self, sha_hex: str) -> tuple[bytes, bytes] | None:
        off = self.idx.find(bytes.fromhex(sha_hex))
        return None if off is None else self._read_at(off)

    def _read_at(self, offset: int) -> tuple[bytes, bytes]:
        # deep delta chains (git defaults to depth 50) hammer the same bases,
        # so memoising by offset is the difference between ms and seconds.
        hit = self._cache.get(offset)
        if hit is not None:
            return hit
        d, pos = self.data, offset
        b = d[pos]
        pos += 1
        kind = (b >> 4) & 7
        size, shift = b & 0x0F, 4
        while b & 0x80:
            b = d[pos]
            pos += 1
            size |= (b & 0x7F) << shift
            shift += 7

        if kind == OBJ_OFS_DELTA:
            # not a plain varint: each continuation adds one before shifting,
            # so there are no redundant encodings.
            b = d[pos]
            pos += 1
            rel = b & 0x7F
            while b & 0x80:
                b = d[pos]
                pos += 1
                rel = ((rel + 1) << 7) | (b & 0x7F)
            base_kind, base = self._read_at(offset - rel)
            result = (base_kind, apply_delta(base, self._inflate(pos, size)))
        elif kind == OBJ_REF_DELTA:
            base_sha = d[pos:pos + 20].hex()
            pos += 20
            got = self.get(base_sha)
            if got is None and self.resolve_external:
                got = self.resolve_external(base_sha)
            if got is None:
                raise KeyError(f"missing delta base {base_sha}")
            base_kind, base = got
            result = (base_kind, apply_delta(base, self._inflate(pos, size)))
        elif kind in TYPE_NAMES:
            result = (TYPE_NAMES[kind], self._inflate(pos, size))
        else:
            raise ValueError(f"bad pack object type {kind} at {offset}")

        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[offset] = result
        return result

    def _inflate(self, pos: int, size: int) -> bytes:
        out = zlib.decompressobj().decompress(self.data[pos:pos + size * 2 + 1024], size)
        if len(out) != size:
            # compressed stream bigger than my guess; just hand it the rest
            out = zlib.decompressobj().decompress(self.data[pos:], size)
        return out
