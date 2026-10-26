"""Tests for the GUI approval dialogs: display failures and message layout."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from ghsudo import __main__ as main


def _script(code: str) -> list[str]:
    """A stand-in dialog: a python one-liner run as the GUI subprocess."""
    return [sys.executable, "-c", code]


def _fail_with(stderr: str, rc: int = 1) -> list[str]:
    return _script(f"import sys; sys.stderr.write({stderr!r}); sys.exit({rc})")


# ---------------------------------------------------------------------------
# Dialog process control
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists("/bin/sleep"), reason="needs POSIX helper binaries"
)
class TestRunGui:
    """The dialog subprocess answers, times out, or is dismissed by its peer."""

    def test_exit_code_returned(self):
        assert main._run_gui(["/bin/true"]) == 0
        assert main._run_gui(["/bin/false"]) == 1

    def test_missing_tool_is_unavailable(self):
        assert main._run_gui(["/nonexistent/dialog-tool"]) is None

    def test_timeout_is_a_denial(self):
        begin = time.monotonic()
        assert main._run_gui(["/bin/sleep", "30"], timeout=1) == 1
        assert time.monotonic() - begin < 10

    def test_cancel_dismisses_the_dialog(self):
        cancel = main._GuiCancel()
        threading.Timer(0.3, cancel.set).start()
        begin = time.monotonic()
        assert main._run_gui(["/bin/sleep", "30"], timeout=60, cancel=cancel) is None
        assert time.monotonic() - begin < 10

    def test_cancel_kills_the_subprocess_immediately(self):
        # Regression: cancel.set() must kill the dialog subprocess itself,
        # synchronously, on the calling thread — not just flip a flag for
        # _run_gui's own (daemon) polling thread to notice on its own time.
        # A daemon thread killed by process exit before its next poll would
        # never run the kill at all, orphaning the dialog window.
        cancel = main._GuiCancel()

        thread = threading.Thread(
            target=main._run_gui,
            args=(["/bin/sleep", "30"],),
            kwargs={"timeout": 60, "cancel": cancel},
        )
        thread.start()

        # Wait until the subprocess is actually registered so we cancel a
        # live process, not a not-yet-launched one.
        proc = None
        for _ in range(100):
            with cancel._lock:
                proc = cancel._proc
            if proc is not None:
                break
            time.sleep(0.02)
        assert proc is not None, "dialog subprocess never registered"

        begin = time.monotonic()
        cancel.set()
        elapsed = time.monotonic() - begin
        # set() itself must have killed it — no poll interval to wait out.
        # A generous margin under _CANCEL_POLL_INTERVAL (0.2s) so this still
        # fails if a regression falls back to polling for the kill instead
        # of doing it synchronously inside set().
        assert elapsed < 0.1
        assert proc.poll() is not None
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# GUI-only approval (no ntfy)
# ---------------------------------------------------------------------------


class TestAskApprovalWithoutNtfy:
    """Zero behaviour change for users who never configure ntfy."""

    def test_gui_approval_returned(self, no_ntfy, monkeypatch):
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: True)
        assert main._ask_approval("gh pr merge 1", "acme") is True

    def test_gui_denial_returned(self, no_ntfy, monkeypatch):
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: False)
        assert main._ask_approval("gh pr merge 1", "acme") is False

    def test_no_display_exits_no_interactive(self, no_ntfy, monkeypatch):
        monkeypatch.setattr(main, "_has_display", lambda: False)
        with pytest.raises(SystemExit) as exc:
            main._ask_approval("gh pr merge 1", "acme")
        assert exc.value.code == main.EXIT_NO_INTERACTIVE

    def test_no_gui_toolkit_exits_no_interactive(self, no_ntfy, monkeypatch):
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: None)
        with pytest.raises(SystemExit) as exc:
            main._ask_approval("gh pr merge 1", "acme")
        assert exc.value.code == main.EXIT_NO_INTERACTIVE

    def test_gui_runs_on_the_main_thread(self, no_ntfy, monkeypatch):
        """Without a second channel there is nothing to race, so no thread hop."""
        seen = []
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(
            main,
            "_ask_gui",
            lambda *a, **k: seen.append(threading.current_thread()) or True,
        )
        main._ask_approval("gh pr merge 1", "acme")
        assert seen == [threading.main_thread()]


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
            "The Wayland connection broke. Did the Wayland compositor die?\n",
            "Gdk-Message: Error 71 (Protocol error) dispatching to Wayland display.\n",
            "Unable to init server: Could not connect: Connection refused\n",
            "Failed to create wl_display (No such file or directory)\n",
            "qt.qpa.wayland: Failed to connect to Wayland display\n",
        ],
    )
    def test_display_error_raises(self, stderr):
        with pytest.raises(main._DialogError):
            main._run_gui(_fail_with(stderr))

    def test_plain_deny_is_still_a_denial(self):
        assert main._run_gui(_fail_with("")) == 1

    def test_unrelated_warning_on_deny_is_still_a_denial(self):
        warning = "(zenity:7): Gtk-WARNING **: Theme parsing error\n"
        assert main._run_gui(_fail_with(warning)) == 1

    def test_killed_by_signal_raises(self):
        with pytest.raises(main._DialogError):
            main._run_gui(_script("import os, signal; os.kill(os.getpid(), 9)"))


class TestAskGuiDisplayFailure:
    def test_display_failure_propagates_and_stops_fallbacks(self, monkeypatch):
        tried = []

        def _broken(*_a, **_k):
            tried.append("xmessage")
            raise main._DialogError("X connection broken")

        def _never(*_a, **_k):
            tried.append("zenity")
            return False

        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(main.platform, "system", lambda: "Linux")
        monkeypatch.setattr(main, "_ask_xmessage", _broken)
        monkeypatch.setattr(main, "_ask_zenity", _never)
        monkeypatch.setattr(main, "_ask_kdialog", _never)
        with pytest.raises(main._DialogError):
            main._ask_gui("gh pr merge 1", "acme")
        # The display is gone, not the toolkit: the next toolkit would only
        # hang or fail on the same display, so it must not be tried.
        assert tried == ["xmessage"]

    def _setup(self, monkeypatch, zenity, kdialog):
        tried = []

        def _x11_broken(*_a, **_k):
            tried.append("xmessage")
            raise main._DialogError("xmessage lost its display connection")

        def _answer(name, value):
            def _ask(*_a, **_k):
                tried.append(name)
                return value

            return _ask

        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr(main.platform, "system", lambda: "Linux")
        monkeypatch.setattr(main, "_ask_xmessage", _x11_broken)
        monkeypatch.setattr(main, "_ask_zenity", _answer("zenity", zenity))
        monkeypatch.setattr(main, "_ask_kdialog", _answer("kdialog", kdialog))
        return tried

    def test_wayland_toolkit_tried_after_x11_only_failure(self, monkeypatch):
        """xmessage is X11-only; zenity/kdialog can still reach Wayland."""
        tried = self._setup(monkeypatch, zenity=True, kdialog=None)
        assert main._ask_gui("gh pr merge 1", "acme") is True
        assert tried == ["xmessage", "zenity"]

    def test_failure_reraised_when_no_wayland_toolkit_installed(self, monkeypatch):
        tried = self._setup(monkeypatch, zenity=None, kdialog=None)
        with pytest.raises(main._DialogError):
            main._ask_gui("gh pr merge 1", "acme")
        assert tried == ["xmessage", "zenity", "kdialog"]

    def test_wayland_toolkit_failure_is_not_retried(self, monkeypatch):
        tried = self._setup(monkeypatch, zenity=None, kdialog=None)

        def _zenity_broken(*_a, **_k):
            tried.append("zenity")
            raise main._DialogError("zenity lost its display connection")

        monkeypatch.setattr(main, "_ask_zenity", _zenity_broken)
        with pytest.raises(main._DialogError):
            main._ask_gui("gh pr merge 1", "acme")
        assert tried == ["xmessage", "zenity"]


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
        text = main._display_safe("git commit -m 'two  spaces   and\nnewline' ")
        text += "y" * 150
        lines = main._wrap_command(text)
        indent = len(main._COMMAND_INDENT)
        assert "".join(line[indent:] for line in lines) == text
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


# ---------------------------------------------------------------------------
# The displayed command cannot forge dialog lines
# ---------------------------------------------------------------------------

_FORGERS = [
    "\r",
    "\n",
    " ",
    " ",
    "\x85",
    "\x0b",
    "\x0c",
    "‮",
    "⁦",
    "‏",
    "​",
    "\t",
]


class TestDisplaySafe:
    @pytest.mark.parametrize("char", _FORGERS)
    def test_control_and_format_chars_are_escaped(self, char):
        shown = main._display_safe(f"gh api -f 'x={char}Repository: evil/repo'")
        assert char not in shown
        assert "\\" in shown

    def test_printable_text_untouched(self):
        cmd = "gh pr comment 1 --body 'zażółć ✓ <b>&amp;</b> \\n'"
        assert main._display_safe(cmd) == cmd

    def test_escape_is_readable(self):
        assert main._display_safe("a\rb") == "a\\rb"
        assert main._display_safe("a\nb") == "a\\nb"
        assert main._display_safe("a‮b") == "a\\u202eb"

    @pytest.mark.parametrize("char", _FORGERS)
    def test_no_forged_header_in_dialog(self, char):
        cmd = f"gh api -f 'note={char}Repository: evil/harmless{char}' -X DELETE x"
        msg = main._format_approval_msg(cmd, "acme", "acme/widget")
        headers = [
            line
            for line in msg.splitlines()
            if line.startswith(("Repository:", "Command to execute:"))
        ]
        assert headers == ["Repository: acme/widget", "Command to execute:"]
        if char != "\n":  # the message's own layout uses newlines
            assert char not in msg

    def test_repo_slug_is_escaped_too(self):
        msg = main._format_approval_msg("gh pr list", "acme", "a/b Command: x")
        assert " " not in msg

    def test_ntfy_message_is_escaped(self):
        msg = main._format_ntfy_message("gh api\r‮evil", "acme", "a/b x")
        assert not any(c in msg for c in "\r‮ ")
        assert msg.count("\n") == 1

    def test_kdialog_doubles_backslashes(self, monkeypatch):
        """kdialog unescapes `\\n` itself, so a literal backslash-n would break lines."""
        seen = {}

        def _capture(cmd, **_k):
            seen["cmd"] = cmd
            return 1

        monkeypatch.setattr(main, "_run_gui", _capture)
        main._ask_kdialog("echo 'a\\nRepository: evil'", "acme")
        text = seen["cmd"][seen["cmd"].index("--yesno") + 1]
        assert "a\\\\nRepository" in text
        assert "\nRepository: evil" not in text


# ---------------------------------------------------------------------------
# Display failure without another channel says so, not "install xmessage"
# ---------------------------------------------------------------------------


class TestGuiOnlyDisplayLost:
    def test_message_and_exit_code(self, monkeypatch, capsys):
        def _broken(*_a, **_k):
            raise main._DialogError("xmessage lost its display connection")

        monkeypatch.setattr(main, "_load_ntfy_config", lambda: None)
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main.platform, "system", lambda: "Linux")
        monkeypatch.setattr(main, "_ask_xmessage", _broken)
        with pytest.raises(SystemExit) as exc:
            main._ask_approval("gh pr merge 1", "acme")
        assert exc.value.code == main.EXIT_NO_INTERACTIVE
        err = capsys.readouterr().err
        assert "lost its display" in err
        assert "Install one of" not in err
        assert "no supported GUI dialog tool" not in err


# ---------------------------------------------------------------------------
# Partial stderr is kept when a grandchild holds the pipe open
# ---------------------------------------------------------------------------


class TestLingeringStderr:
    def test_display_error_seen_despite_lingering_child(self):
        script = (
            "import subprocess, sys;"
            'sys.stderr.write("Error: Can\'t open display: :9\\n");'
            "sys.stderr.flush();"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']);"
            "sys.exit(1)"
        )
        with pytest.raises(main._DialogError):
            main._run_gui(_script(script))
