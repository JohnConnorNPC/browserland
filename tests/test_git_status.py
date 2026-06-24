"""git_status.collect + the porcelain-v2 parser (todo2 task 6)."""

import os
import shutil
from pathlib import Path

import pytest

from webterm.agent import git_status

REPO = Path(__file__).resolve().parents[1]


def test_parse_clean_branch():
    out = (
        "# branch.oid abc123\n"
        "# branch.head main\n"
        "# branch.ab +0 -0\n"
    )
    info = git_status._parse(out)
    assert info["ok"] is True
    assert info["branch"] == "main"
    assert info["detached"] is False
    assert info["ahead"] == 0 and info["behind"] == 0
    assert info["dirty"] is False and info["dirty_count"] == 0


def test_parse_dirty_and_ahead_behind():
    out = (
        "# branch.head feature\n"
        "# branch.ab +2 -3\n"
        "1 M. N... 100644 100644 100644 aaa bbb staged.py\n"
        "1 .M N... 100644 100644 100644 aaa bbb unstaged.py\n"
        "1 MM N... 100644 100644 100644 aaa bbb both.py\n"
        "? untracked.txt\n"
        "u UU N... 1 2 3 aaa bbb ccc conflict.py\n"
    )
    info = git_status._parse(out)
    assert info["branch"] == "feature"
    assert info["ahead"] == 2 and info["behind"] == 3
    # staged: M. and MM -> 2 ; unstaged: .M and MM -> 2
    assert info["staged"] == 2
    assert info["unstaged"] == 2
    assert info["untracked"] == 1
    assert info["conflicts"] == 1
    assert info["dirty"] is True
    assert info["dirty_count"] == 2 + 2 + 1 + 1


def test_parse_detached():
    info = git_status._parse("# branch.head (detached)\n")
    assert info["detached"] is True


def test_collect_no_cwd():
    assert git_status.collect("")["error"] == "no_cwd"
    assert git_status.collect("/no/such/dir/xyz")["error"] == "no_cwd"


def test_collect_not_a_repo(tmp_path):
    res = git_status.collect(str(tmp_path))
    assert res["ok"] is False
    assert res["error"] in ("not_a_repo", "git_not_found")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_collect_on_this_repo():
    res = git_status.collect(str(REPO))
    # This source tree is a git repo, so collect must succeed and name a branch.
    assert res["ok"] is True, res
    assert isinstance(res["branch"], str)
    assert isinstance(res["dirty_count"], int)
