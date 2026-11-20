import random

import pytest

from conftest import git, needs_git
from tinygit.pack import apply_delta
from tinygit.repo import Repo

pytestmark = needs_git


def make_history(path, commits=40):
    """A repo whose files mutate a little each commit, so gc produces long delta chains."""
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    rng = random.Random(5)
    lines = [f"def f{i}(x):\n    return x * {i}\n" for i in range(200)]
    for n in range(commits):
        for _ in range(3):
            lines[rng.randrange(len(lines))] = f"def g{rng.randrange(10**6)}(): pass\n"
        (path / "big.py").write_text("".join(lines))
        (path / f"notes{n % 4}.txt").write_text(f"rev {n}\n" * (n + 1))
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"rev {n}")
    return path


def all_objects(path):
    out = git(path, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    return [line.split() for line in out.splitlines()]


@pytest.mark.parametrize("delta_style", ["ofs", "ref"])
def test_every_packed_object_matches_git(tmp_path, delta_style):
    repo_dir = make_history(tmp_path / "r")
    use_ofs = "true" if delta_style == "ofs" else "false"
    git(repo_dir, "-c", f"repack.usedeltabaseoffset={use_ofs}", "repack", "-adf",
        "--depth=50", "--window=50")
    git(repo_dir, "prune-packed")
    assert not any((repo_dir / ".git/objects").glob("[0-9a-f][0-9a-f]/*")), "should be fully packed"

    verify = git(repo_dir, "verify-pack", "-v", *map(str, (repo_dir / ".git/objects/pack").glob("*.idx")))
    assert "chain length" in verify, "expected deltas in the pack"

    repo = Repo(repo_dir)
    objs = all_objects(repo_dir)
    assert len(objs) > 100
    for sha, kind in objs:
        got_kind, body = repo.read(sha)  # read() re-hashes, so this checks content too
        assert got_kind.decode() == kind


def test_log_and_fsck_on_packed_repo(tmp_path):
    repo_dir = make_history(tmp_path / "r", commits=15)
    git(repo_dir, "gc", "-q", "--aggressive")
    repo = Repo(repo_dir)
    ours = [sha for sha, _ in repo.log()]
    assert ours == git(repo_dir, "rev-list", "HEAD").split()
    n, problems = repo.fsck()
    assert problems == [] and n == len(git(repo_dir, "rev-list", "--objects", "--all").splitlines())


def test_apply_delta_copy_and_insert():
    base = b"hello brave new world"
    # header: src len 21, dst len 15; copy 6 bytes @0, insert "old ", copy 5 @16
    delta = bytes([21, 15, 0x90, 6, 4]) + b"old " + bytes([0x91, 16, 5])
    assert apply_delta(base, delta) == b"hello old world"


def test_apply_delta_rejects_wrong_base():
    with pytest.raises(ValueError):
        apply_delta(b"short", bytes([99, 1, 1]) + b"x")
