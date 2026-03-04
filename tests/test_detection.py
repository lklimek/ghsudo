"""Tests for org and repo slug detection functions."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import subprocess


from ghsudo.__main__ import (
    _detect_repo_slug,
    _detect_repo_slug_from_args,
    _detect_repo_slug_from_git_remote,
    _format_approval_msg,
)


# ---------------------------------------------------------------------------
# _detect_repo_slug_from_args
# ---------------------------------------------------------------------------


class TestDetectRepoSlugFromArgs:
    """Tests for extracting owner/repo from gh CLI arguments."""

    def test_dash_R_separate(self):
        assert (
            _detect_repo_slug_from_args(["gh", "pr", "-R", "acme/widget", "list"])
            == "acme/widget"
        )

    def test_dash_R_attached(self):
        assert (
            _detect_repo_slug_from_args(["gh", "pr", "-Racme/widget", "list"])
            == "acme/widget"
        )

    def test_long_repo_separate(self):
        assert (
            _detect_repo_slug_from_args(["gh", "issue", "--repo", "Org/Repo"])
            == "org/repo"
        )

    def test_long_repo_equals(self):
        assert (
            _detect_repo_slug_from_args(["gh", "--repo=Org/Repo", "pr", "list"])
            == "org/repo"
        )

    def test_no_repo_flag(self):
        assert _detect_repo_slug_from_args(["gh", "pr", "list"]) is None

    def test_dash_R_at_end_no_value(self):
        assert _detect_repo_slug_from_args(["gh", "pr", "-R"]) is None

    def test_repo_without_slash(self):
        """A bare name without '/' should not match."""
        assert _detect_repo_slug_from_args(["gh", "-R", "onlyorg"]) is None

    def test_whitespace_trimmed(self):
        assert _detect_repo_slug_from_args(["gh", "-R", "  Org/Repo  "]) == "org/repo"

    def test_trailing_slash_rejected(self):
        assert _detect_repo_slug_from_args(["gh", "-R", "org/"]) is None

    def test_leading_slash_rejected(self):
        assert _detect_repo_slug_from_args(["gh", "-R", "/repo"]) is None

    def test_extra_segments_rejected(self):
        assert _detect_repo_slug_from_args(["gh", "-R", "a/b/c"]) is None


# ---------------------------------------------------------------------------
# _detect_repo_slug_from_git_remote
# ---------------------------------------------------------------------------


def _mock_git_remote(url: str):
    """Return a patch that makes `git remote get-url origin` return *url*."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 0
    result.stdout = url
    return patch("ghsudo.__main__.subprocess.run", return_value=result)


class TestDetectRepoSlugFromGitRemote:
    """Tests for extracting owner/repo from git origin remote."""

    def test_ssh_url(self):
        with _mock_git_remote("git@github.com:lklimek/ghsudo.git\n"):
            assert _detect_repo_slug_from_git_remote() == "lklimek/ghsudo"

    def test_ssh_url_no_dot_git(self):
        with _mock_git_remote("git@github.com:lklimek/ghsudo\n"):
            assert _detect_repo_slug_from_git_remote() == "lklimek/ghsudo"

    def test_https_url(self):
        with _mock_git_remote("https://github.com/Acme/Widget.git\n"):
            assert _detect_repo_slug_from_git_remote() == "acme/widget"

    def test_https_url_no_dot_git(self):
        with _mock_git_remote("https://github.com/Acme/Widget\n"):
            assert _detect_repo_slug_from_git_remote() == "acme/widget"

    def test_non_github_url(self):
        with _mock_git_remote("git@gitlab.com:org/repo.git\n"):
            assert _detect_repo_slug_from_git_remote() is None

    def test_git_failure(self):
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = 1
        result.stdout = ""
        with patch("ghsudo.__main__.subprocess.run", return_value=result):
            assert _detect_repo_slug_from_git_remote() is None

    def test_git_not_found(self):
        with patch(
            "ghsudo.__main__.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            assert _detect_repo_slug_from_git_remote() is None


# ---------------------------------------------------------------------------
# _detect_repo_slug  (combined)
# ---------------------------------------------------------------------------


class TestDetectRepoSlug:
    """Args take priority over git remote."""

    def test_args_preferred_over_remote(self):
        with _mock_git_remote("git@github.com:remote/repo.git"):
            slug = _detect_repo_slug(["gh", "-R", "arg/repo", "pr", "list"])
        assert slug == "arg/repo"

    def test_falls_back_to_remote(self):
        with _mock_git_remote("git@github.com:remote/repo.git"):
            slug = _detect_repo_slug(["gh", "pr", "list"])
        assert slug == "remote/repo"

    def test_none_when_nothing(self):
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = 1
        result.stdout = ""
        with patch("ghsudo.__main__.subprocess.run", return_value=result):
            assert _detect_repo_slug(["gh", "pr", "list"]) is None


# ---------------------------------------------------------------------------
# _format_approval_msg
# ---------------------------------------------------------------------------


class TestFormatApprovalMsg:
    """Approval message should show repo when available, org otherwise."""

    def test_with_repo(self):
        msg = _format_approval_msg("gh pr merge 1", "acme", repo="acme/widget")
        assert "Repository: acme/widget" in msg
        assert "Organization:" not in msg

    def test_without_repo(self):
        msg = _format_approval_msg("gh pr merge 1", "acme")
        assert "Organization: acme" in msg
        assert "Repository:" not in msg

    def test_command_present(self):
        msg = _format_approval_msg("gh pr merge 42 --merge", "x", repo="x/y")
        assert "gh pr merge 42 --merge" in msg
