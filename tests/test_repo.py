import os

import pytest

from conftest import git, needs_git
from tinygit.cli import main
from tinygit.index import read_index
from tinygit.repo import GitError, Repo


@pytest.fixture
def repo(home):
    r = Repo.init(home / "w")
    files = {
        "README.md": "# hi\n",
        "src/app.py": "print('app')\n",
        "src/util/strings.py": "def up(s):\n    return s.upper()\n",
        "foo.txt": "a\n",
        "foo-bar/x": "b\n",
    }
    for rel, text in files.items():
        p = r.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (r.root / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (r.root / "run.sh").chmod(0o755)
    return r


def test_commit_is_reproducible(repo):
    repo.add([str(repo.root)])
    sha = repo.commit("first")
    # pinned author/committer dates in conftest -> stable sha
    assert len(sha) == 40 and repo.read_commit(sha).parents == []
    assert repo.resolve("main") == sha == repo.resolve(sha[:7])


@needs_git
def test_real_git_accepts_everything_we_write(repo):
    repo.add([str(repo.root)])
    repo.commit("first")
    (repo.root / "src/app.py").write_text("print('app v2')\n")
    (repo.root / "foo.txt").unlink()
    repo.add([str(repo.root)])
    second = repo.commit("second\n\nwith a body")

    git(repo.root, "fsck", "--strict", "--no-dangling")
    assert git(repo.root, "rev-parse", "HEAD").strip() == second
    assert git(repo.root, "status", "--porcelain") == ""
    assert git(repo.root, "log", "--format=%s").split("\n")[:2] == ["second", "first"]
    # tree + modes survive the trip, including the executable bit
    ls = git(repo.root, "ls-tree", "-r", "HEAD")
    assert "100755 blob" in ls.split("run.sh")[0].splitlines()[-1]
    assert git(repo.root, "write-tree").strip() == repo.read_commit(second).tree


@needs_git
def test_we_read_the_index_git_writes(home):
    d = home / "g"
    d.mkdir()
    git(d, "init", "-q")
    for name in ("a", "dir/b", "dir/sub/c", "long-" + "x" * 90):
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(name)
    git(d, "add", ".")
    git(d, "commit", "-qm", "x")  # makes git write a TREE extension too
    ours = read_index(d / ".git/index")
    theirs = git(d, "ls-files", "-s").splitlines()
    assert [f"{e.mode:06o} {e.sha} 0\t{p}" for p, e in ours.items()] == theirs
    assert Repo(d).status() == {"staged": [], "unstaged": [], "untracked": []}


def test_status_sections(repo):
    repo.add([str(repo.root)])
    repo.commit("first")
    (repo.root / "README.md").write_text("# changed\n")
    (repo.root / "new.txt").write_text("hello\n")
    (repo.root / "foo.txt").unlink()
    repo.add([str(repo.root / "new.txt")])
    st = repo.status()
    assert st["staged"] == ["new file:   new.txt"]
    assert st["unstaged"] == ["deleted:    foo.txt", "modified:   README.md"]
    assert st["untracked"] == []


def test_touching_a_file_is_not_a_modification(repo):
    repo.add([str(repo.root)])
    repo.commit("first")
    p = repo.root / "README.md"
    os.utime(p, (1, 1))
    assert repo.status()["unstaged"] == []
    # and the stat cache got refreshed
    assert read_index(repo.index_path)["README.md"].mtime_s == 1


def test_gitignore(repo):
    (repo.root / ".gitignore").write_text("*.log\nbuild/\n!keep.log\n/src/util/\n")
    (repo.root / "debug.log").write_text("x")
    (repo.root / "keep.log").write_text("x")
    (repo.root / "build").mkdir()
    (repo.root / "build/out.o").write_text("x")
    files = repo.walk()
    assert "debug.log" not in files and "keep.log" in files
    assert not any(f.startswith(("build/", "src/util/")) for f in files)
    assert "src/app.py" in files


def test_branch_and_checkout_roundtrip(repo):
    repo.add([str(repo.root)])
    first = repo.commit("first")
    repo.create_branch("feature")
    repo.checkout("feature")
    (repo.root / "src/util/strings.py").unlink()
    (repo.root / "feature.py").write_text("x = 1\n")
    repo.add([str(repo.root)])
    repo.commit("feature work")

    repo.checkout("main")
    assert (repo.root / "src/util/strings.py").exists()
    assert not (repo.root / "feature.py").exists()
    assert repo.head() == first and repo.head_target() == "refs/heads/main"

    repo.checkout("feature")
    assert not (repo.root / "src/util").exists(), "empty dirs should be cleaned up"
    assert repo.status() == {"staged": [], "unstaged": [], "untracked": []}


def test_checkout_refuses_to_lose_work(repo):
    repo.add([str(repo.root)])
    repo.commit("first")
    repo.create_branch("other")
    (repo.root / "README.md").write_text("unsaved\n")
    with pytest.raises(GitError, match="local changes"):
        repo.checkout("other")


def test_rev_suffixes(repo):
    repo.add([str(repo.root)])
    shas = [repo.commit("c0")]
    for i in range(1, 4):
        (repo.root / "foo.txt").write_text(f"{i}\n")
        repo.add([str(repo.root / "foo.txt")])
        shas.append(repo.commit(f"c{i}"))
    assert repo.resolve("HEAD~3") == shas[0]
    assert repo.resolve("HEAD^^") == shas[1]
    assert repo.resolve("main~1^") == shas[1]
    with pytest.raises(GitError):
        repo.resolve("HEAD~9")
    assert [c.subject for _, c in repo.log()] == ["c3", "c2", "c1", "c0"]


def test_nothing_to_commit(repo):
    repo.add([str(repo.root)])
    repo.commit("first")
    with pytest.raises(GitError, match="nothing to commit"):
        repo.commit("again")


def test_corruption_is_caught(repo):
    repo.add([str(repo.root)])
    sha = repo.commit("first")
    tree = repo.read_commit(sha).tree
    p = repo.gitdir / "objects" / tree[:2] / tree[2:]
    p.chmod(0o644)
    import zlib
    p.write_bytes(zlib.compress(b"tree 0\0"))
    n, problems = repo.fsck()
    assert any("corrupt" in x for x in problems)


def test_cli_smoke(repo, capsys, monkeypatch):
    monkeypatch.chdir(repo.root)
    assert main(["add", "."]) == 0
    assert main(["commit", "-m", "from the cli"]) == 0
    (repo.root / "README.md").write_text("# hi\nmore\n")
    main(["diff"])
    out = capsys.readouterr().out
    assert "+more" in out and "(root-commit)" in out
    assert main(["checkout", "nope"]) == 128
