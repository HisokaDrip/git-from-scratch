import random

from conftest import git, needs_git
from tinygit import objects as obj
from tinygit.objects import Commit, TreeEntry


def test_empty_blob_hash_is_the_famous_one():
    assert obj.hash_object(b"blob", b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


@needs_git
def test_blob_hashes_match_git(tmp_path):
    rng = random.Random(1)
    for i in range(20):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 3000)))
        f = tmp_path / f"f{i}"
        f.write_bytes(data)
        assert obj.hash_object(b"blob", data) == git(tmp_path, "hash-object", str(f)).strip()


def test_loose_roundtrip():
    kind, body = obj.decompress_loose(obj.compress(b"blob", b"hello\n"))
    assert (kind, body) == (b"blob", b"hello\n")


@needs_git
def test_tree_order_matches_mktree(tmp_path):
    git(tmp_path, "init", "-q")
    blob = git(tmp_path, "hash-object", "-w", "--stdin", input="x\n").strip()
    sub = git(tmp_path, "mktree", input=f"100644 blob {blob}\tinner\n").strip()
    # the classic trap: "foo" the directory sorts as "foo/", after "foo-bar" and "foo.txt"
    entries = [
        TreeEntry(0o40000, "foo", sub),
        TreeEntry(0o100644, "foo.txt", blob),
        TreeEntry(0o100644, "foo-bar", blob),
        TreeEntry(0o100755, "run.sh", blob),
        TreeEntry(0o100644, "Zed", blob),
    ]
    mine = obj.hash_object(b"tree", obj.encode_tree(entries))
    listing = "".join(
        f"{e.mode:06o} {'tree' if e.is_dir else 'blob'} {e.sha}\t{e.name}\n" for e in entries
    )
    assert mine == git(tmp_path, "mktree", input=listing).strip()


def test_tree_decode_inverts_encode():
    entries = [TreeEntry(0o100644, f"file{i}", f"{i:040x}") for i in range(5)]
    assert obj.decode_tree(obj.encode_tree(entries)) == entries


def test_commit_roundtrip_keeps_multiline_headers():
    c = Commit("a" * 40, ["b" * 40, "c" * 40], "A <a@x> 1 +0000", "B <b@x> 2 -0700",
               "subject\n\nbody text\n", extra=[("gpgsig", "-----BEGIN-----\nabc\n-----END-----")])
    back = Commit.decode(c.encode())
    assert back == c
    assert back.subject == "subject"


def test_subject_folds_first_paragraph():
    c = Commit("a" * 40, [], "x", "x", "fix the thing\nreally this time\n\nlonger story\n")
    assert c.subject == "fix the thing really this time"
