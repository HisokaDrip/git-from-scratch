import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# pin identities/dates so commit shas are reproducible across runs
os.environ.update({
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0530", "GIT_COMMITTER_DATE": "1700000000 +0530",
    "GIT_CONFIG_NOSYSTEM": "1",
})

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="real git not installed")


def git(cwd, *args, input=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          input=input, text=input is None or isinstance(input, str)).stdout


@pytest.fixture
def home(tmp_path, monkeypatch):
    # keep the developer's ~/.gitconfig out of it
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path
