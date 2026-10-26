"""Tests for the GUI approval dialogs: display failures and message layout."""

from __future__ import annotations

import sys
import threading

import pytest

from ghsudo import __main__ as main


def _script(code: str) -> list[str]:
    """A stand-in dialog: a python one-liner run as the GUI subprocess."""
    return [sys.executable, "-c", code]


def _fail_with(stderr: str, rc: int = 1) -> list[str]:
    return _script(f"import sys; sys.stderr.write({stderr!r}); sys.exit({rc})")


# ---------------------------------------------------------------------------
# Display connection failures are not denials
# ---------------------------------------------------------------------------


class TestRunGuiDisplayFailure:
    """A dialog that loses (or never gets) its display has not answered."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "X connection to :77 broken (explicit kill or server shutdown).\n",
            "Error: Can't open display: :12345\n",
            "(zenity:42): Gtk-WARNING **: 11:00:20.369: Failed to open display\n",
            "kdialog: cannot connect to X server :0\n",
            "qt.qpa.xcb: could not connect to display :0\n",
            "XIO:  fatal IO error 11 (Resource temporarily unavailable) on X server\n",
            "Error reading events from display: Broken pipe\n",
            "Lost connection to Wayland compositor.\n",
        ],
    )
    def test_display_error_raises(self, stderr):
        with pytest.raises(main._DialogFailed):
            main._run_gui(_fail_with(stderr))

    def test_plain_deny_is_still_a_denial(self):
        assert main._run_gui(_fail_with("")) == 1

    def test_unrelated_warning_on_deny_is_still_a_denial(self):
        warning = "(zenity:7): Gtk-WARNING **: Theme parsing error\n"
        assert main._run_gui(_fail_with(warning)) == 1

    def test_killed_by_signal_raises(self):
        with pytest.raises(main._DialogFailed):
            main._run_gui(_script("import os, signal; os.kill(os.getpid(), 9)"))


class TestAskGuiDisplayFailure:
    def test_display_failure_is_unavailable_and_stops_fallbacks(self, monkeypatch):
        tried = []

        def _broken(*_a, **_k):
            tried.append("xmessage")
            raise main._DialogFailed("X connection broken")

        def _never(*_a, **_k):
            tried.append("zenity")
            return False

        monkeypatch.setattr(main.platform, "system", lambda: "Linux")
        monkeypatch.setattr(main, "_ask_xmessage", _broken)
        monkeypatch.setattr(main, "_ask_zenity", _never)
        monkeypatch.setattr(main, "_ask_kdialog", _never)
        assert main._ask_gui("gh pr merge 1", "acme") is None
        # The display is gone, not the toolkit: the next toolkit would only
        # hang or fail on the same display, so it must not be tried.
        assert tried == ["xmessage"]


class TestAskApprovalDisplayLost:
    def test_ntfy_still_answers_after_the_dialog_crashes(self, monkeypatch):
        """Regression: an X failure used to read as Deny and beat the phone."""
        cfg = main._NtfyConfig(
            topic="t",
            server="https://ntfy.example",
            mode=main._MODE_REMOTE_APPROVE,
            timeout=5,
        )
        gui_done = threading.Event()
        real_run_gui = main._run_gui
        launched = []

        def _dialog_losing_x(cmd, **kwargs):
            # Run the real dialog plumbing, but with a process that dies
            # exactly the way xmessage does when its X server goes away.
            launched.append(cmd[0])
            try:
                return real_run_gui(
                    _fail_with("X connection to :0 broken (server shutdown).\n"),
                    **kwargs,
                )
            finally:
                gui_done.set()

        def _phone(_self):
            gui_done.wait(5)
            return True

        monkeypatch.setattr(main, "_load_ntfy_config", lambda: cfg)
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main.platform, "system", lambda: "Linux")
        monkeypatch.setattr(main, "_run_gui", _dialog_losing_x)
        monkeypatch.setattr(main._NtfyChannel, "run", _phone)
        assert main._ask_approval("gh pr merge 1", "acme") is True
        assert launched == ["xmessage"]


# ---------------------------------------------------------------------------
# Long commands wrap in the dialog
# ---------------------------------------------------------------------------


class TestApprovalMessageWrapping:
    def test_long_command_is_wrapped(self):
        cmd = "gh api -X POST repos/acme/widget/issues " + " ".join(
            f"-f field{i}=value{i}" for i in range(20)
        )
        msg = main._format_approval_msg(cmd, "acme")
        assert all(len(line) <= main._DIALOG_WIDTH for line in msg.splitlines())
        assert "field19=value19" in msg

    def test_long_unbroken_token_is_wrapped(self):
        cmd = "gh api " + "x" * 300
        msg = main._format_approval_msg(cmd, "acme")
        assert all(len(line) <= main._DIALOG_WIDTH for line in msg.splitlines())

    def test_wrapping_preserves_the_command_exactly(self):
        cmd = "git commit -m 'two  spaces   and\nnewline' " + "y" * 150
        lines = main._wrap_command(cmd)
        indent = len(main._COMMAND_INDENT)
        rejoined = "".join(line[indent:] for line in lines)
        assert rejoined == cmd.replace("\n", "")
        assert len(lines) > 2

    def test_short_command_untouched(self):
        msg = main._format_approval_msg("gh pr merge 1", "acme")
        assert f"\n{main._COMMAND_INDENT}gh pr merge 1\n" in msg


class TestZenityMarkup:
    def test_zenity_disables_pango_markup(self, monkeypatch):
        """The command must be shown literally, not rendered as markup."""
        seen = {}

        def _capture(cmd, **_k):
            seen["cmd"] = cmd
            return 1

        monkeypatch.setattr(main, "_run_gui", _capture)
        main._ask_zenity("echo '<span size=\"1\">x</span>'", "acme")
        assert "--no-markup" in seen["cmd"]
