import random
import subprocess

from conftest import needs_git
from tinygit.diff import diffstat, myers, unified


def lcs(a, b):
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    return dp[0][0]


def rand_lines(rng, n):
    return [rng.choice("abcde") for _ in range(n)]


def test_edit_script_rebuilds_both_sides():
    rng = random.Random(7)
    for _ in range(300):
        a, b = rand_lines(rng, rng.randrange(15)), rand_lines(rng, rng.randrange(15))
        edits = myers(a, b)
        assert [e.text for e in edits if e.op != "+"] == a
        assert [e.text for e in edits if e.op != "-"] == b


def test_edit_script_is_minimal():
    # myers is optimal: the number of kept lines must equal the LCS length
    rng = random.Random(11)
    for _ in range(300):
        a, b = rand_lines(rng, rng.randrange(20)), rand_lines(rng, rng.randrange(20))
        assert sum(e.op == " " for e in myers(a, b)) == lcs(a, b)


def test_identical_and_empty():
    assert unified("x\n", "x\n") == ""
    assert diffstat("", "a\nb\n") == (2, 0)
    assert diffstat("a\nb\n", "") == (0, 2)


def test_hunk_header_for_pure_insert_at_top():
    out = unified("b\n", "a\nb\n")
    assert "@@ -1 +1,2 @@" in out
    # with no context left, the empty side is reported at the line before it
    assert "@@ -0,0 +1 @@" in unified("", "a\n")


@needs_git
def test_patches_apply_cleanly_with_git_apply(tmp_path):
    rng = random.Random(3)
    for trial in range(40):
        a = [f"line {rng.randrange(30)}" for _ in range(rng.randrange(1, 60))]
        b = list(a)
        for _ in range(rng.randrange(1, 6)):
            op = rng.random()
            i = rng.randrange(len(b) + 1)
            if op < 0.4 and b:
                b.pop(min(i, len(b) - 1))
            elif op < 0.7:
                b.insert(i, f"new {rng.randrange(1000)}")
            elif b:
                b[min(i, len(b) - 1)] = f"changed {rng.randrange(1000)}"
        a_text, b_text = "".join(x + "\n" for x in a), "".join(x + "\n" for x in b)
        if a_text == b_text:
            continue
        (tmp_path / "f.txt").write_text(a_text)
        patch = unified(a_text, b_text, "a/f.txt", "b/f.txt")
        subprocess.run(["git", "apply", "--unsafe-paths", "-"], cwd=tmp_path, input=patch,
                       text=True, check=True, capture_output=True)
        assert (tmp_path / "f.txt").read_text() == b_text, f"trial {trial}"
