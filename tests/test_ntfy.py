"""Tests for the ntfy notification/approval channel."""

from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from ghsudo import __main__ as main


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def ntfy_home(tmp_path, monkeypatch):
    """Redirect config storage to a temp dir and stub the slow machine-key KDF."""
    monkeypatch.setattr(main, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(main, "_NOTIFY_PATH", tmp_path / "notify.enc")
    monkeypatch.setattr(main, "_derive_machine_key", lambda: b"\x2a" * 32)
    return tmp_path


def _cfg(**kwargs) -> main._NtfyConfig:
    defaults = {"topic": "ghsudo-test", "server": "https://ntfy.example"}
    return main._NtfyConfig(**{**defaults, **kwargs})


def _line(event: str = "message", message: str = "") -> bytes:
    return json.dumps({"event": event, "message": message}).encode() + b"\n"


class _FakeStream:
    """Stand-in for the urlopen response of a `/topic/json` subscription."""

    def __init__(self, lines, *, raise_at_end: BaseException | None = None):
        self._lines = list(lines)
        self._raise_at_end = raise_at_end
        self.closed = False
        self.status = 200

    def __iter__(self):
        for line in self._lines:
            if self.closed:
                raise ValueError("I/O operation on closed file")
            yield line
        if self._raise_at_end is not None:
            raise self._raise_at_end

    def read(self):
        return b""

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class _FakeResponse:
    """Stand-in for a publish response."""

    def __init__(self, status: int = 200):
        self.status = status

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeUrlopen:
    """Routes publish (POST) and subscribe (GET .../json) calls to canned results."""

    def __init__(self, *, stream=None, publish_status: int = 200, publish_error=None):
        self.stream = stream
        self.publish_status = publish_status
        self.publish_error = publish_error
        self.published: list[dict] = []
        self.subscribed: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/json"):
            self.subscribed.append(url)
            if self.stream is None:
                raise AssertionError("unexpected subscription")
            return self.stream
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append(json.loads(req.data.decode()))
        return _FakeResponse(self.publish_status)


def _patch_urlopen(fake):
    return patch("urllib.request.urlopen", fake)


# ---------------------------------------------------------------------------
# Config storage
# ---------------------------------------------------------------------------


class TestNtfyConfigStorage:
    """Encrypted-at-rest config round-trips and validates its inputs."""

    def test_missing_file_is_not_configured(self, ntfy_home):
        assert main._load_ntfy_config() is None

    def test_round_trip(self, ntfy_home):
        saved = _cfg(
            mode=main._MODE_REMOTE_APPROVE,
            auth_token="tk_secret",
            timeout=120,
        )
        main._save_ntfy_config(saved)
        assert main._load_ntfy_config() == saved

    def test_file_is_encrypted_and_0600(self, ntfy_home):
        main._save_ntfy_config(_cfg(auth_token="tk_secret"))
        raw = main._NOTIFY_PATH.read_bytes()
        assert b"tk_secret" not in raw
        assert b"ghsudo-test" not in raw
        assert main._NOTIFY_PATH.stat().st_mode & 0o777 == 0o600

    def test_disabled_config_is_not_configured(self, ntfy_home):
        main._save_ntfy_config(_cfg(enabled=False))
        assert main._load_ntfy_config() is None

    def test_undecryptable_file_is_not_configured(self, ntfy_home):
        main._NOTIFY_PATH.write_bytes(b"\x01garbage-that-will-never-decrypt")
        assert main._load_ntfy_config() is None

    def test_unknown_fields_ignored(self, ntfy_home):
        blob = main._encrypt_blob(
            json.dumps({"topic": "ghsudo-test", "future_field": 1}),
            main._derive_machine_key(),
        )
        main._NOTIFY_PATH.write_bytes(blob)
        cfg = main._load_ntfy_config()
        assert cfg is not None
        assert cfg.topic == "ghsudo-test"

    def test_default_timeout_is_300s(self):
        assert main._NTFY_DEFAULT_TIMEOUT == 300
        assert _cfg().timeout == 300

    def test_save_creates_file_0600_even_with_permissive_umask(self, ntfy_home):
        old_umask = os.umask(0o000)
        try:
            main._save_ntfy_config(_cfg(auth_token="tk_secret"))
        finally:
            os.umask(old_umask)
        assert main._NOTIFY_PATH.stat().st_mode & 0o777 == 0o600

    def test_save_leaves_no_stray_temp_files(self, ntfy_home):
        main._save_ntfy_config(_cfg())
        leftovers = [p for p in main._CONFIG_DIR.iterdir() if p.name != "notify.enc"]
        assert leftovers == []


class TestNtfyConfigValidation:
    """Invalid stored values must never yield a usable config."""

    def _store(self, payload: dict) -> None:
        main._NOTIFY_PATH.write_bytes(
            main._encrypt_blob(json.dumps(payload), main._derive_machine_key())
        )

    def test_topic_with_path_traversal_rejected(self, ntfy_home):
        self._store({"topic": "../../evil"})
        assert main._load_ntfy_config() is None

    def test_empty_topic_rejected(self, ntfy_home):
        self._store({"topic": ""})
        assert main._load_ntfy_config() is None

    def test_non_http_server_rejected(self, ntfy_home):
        self._store({"topic": "ghsudo-test", "server": "file:///etc/passwd"})
        assert main._load_ntfy_config() is None

    def test_unknown_mode_downgrades_to_notify(self, ntfy_home):
        self._store({"topic": "ghsudo-test", "mode": "auto-approve-everything"})
        cfg = main._load_ntfy_config()
        assert cfg is not None
        assert cfg.mode == main._MODE_NOTIFY

    def test_absurd_timeout_falls_back_to_default(self, ntfy_home):
        self._store({"topic": "ghsudo-test", "timeout": 0})
        cfg = main._load_ntfy_config()
        assert cfg is not None
        assert cfg.timeout == main._NTFY_DEFAULT_TIMEOUT

    def test_server_with_query_rejected(self, ntfy_home):
        self._store({"topic": "ghsudo-test", "server": "https://ntfy.sh/?x=1"})
        assert main._load_ntfy_config() is None

    def test_server_with_fragment_rejected(self, ntfy_home):
        # A fragment is never sent to the server, so the reply action URL
        # (built by appending "/{topic}" to the whole server string) would
        # silently publish to the wrong place.
        self._store({"topic": "ghsudo-test", "server": "https://ntfy.sh/#x"})
        assert main._load_ntfy_config() is None

    def test_server_with_path_is_allowed(self, ntfy_home):
        # A path is fine — e.g. a self-hosted instance behind a reverse-proxy
        # subpath.
        self._store({"topic": "ghsudo-test", "server": "https://example.com/ntfy"})
        cfg = main._load_ntfy_config()
        assert cfg is not None
        assert cfg.server == "https://example.com/ntfy"

    def test_trailing_slash_stripped_from_server(self, ntfy_home):
        self._store({"topic": "ghsudo-test", "server": "https://ntfy.example/"})
        cfg = main._load_ntfy_config()
        assert cfg is not None
        assert cfg.server == "https://ntfy.example"


class TestNtfyEnvVarsAreInert:
    """ntfy config comes only from the on-disk encrypted file — never the env.

    The agent invoking ghsudo controls its own child environment, so an
    env-settable channel (topic, server, or mode) would let it redirect
    notifications, or worse, point ghsudo at a topic/server it owns.
    GHSUDO_NTFY_{TOPIC,SERVER,MODE} are read by nothing in the codebase; these
    tests are the regression guard against that mechanism quietly coming back.
    """

    def test_env_vars_do_not_create_a_config(self, ntfy_home, monkeypatch):
        monkeypatch.setenv("GHSUDO_NTFY_TOPIC", "from-env")
        monkeypatch.setenv("GHSUDO_NTFY_SERVER", "https://ntfy.internal")
        monkeypatch.setenv("GHSUDO_NTFY_MODE", main._MODE_REMOTE_APPROVE)
        assert main._load_ntfy_config() is None

    def test_env_vars_do_not_affect_stored_config(self, ntfy_home, monkeypatch):
        saved = _cfg(
            topic="stored-topic",
            server="https://ntfy.sh",
            mode=main._MODE_REMOTE_APPROVE,
            auth_token="tk_secret",
        )
        main._save_ntfy_config(saved)
        monkeypatch.setenv("GHSUDO_NTFY_TOPIC", "hijacked")
        monkeypatch.setenv("GHSUDO_NTFY_SERVER", "https://agent-controlled.example")
        monkeypatch.setenv("GHSUDO_NTFY_MODE", main._MODE_NOTIFY)
        cfg = main._load_ntfy_config()
        assert cfg == saved

    def test_build_ntfy_config_takes_no_env_parameter(self):
        # Regression guard on the signature itself: no env/env_used knob left
        # for a future change to accidentally wire back up.
        import inspect

        assert list(inspect.signature(main._build_ntfy_config).parameters) == ["data"]


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


class TestNtfyPublish:
    """Publishing uses the JSON API so arbitrary command text is safe to send."""

    def test_publishes_json_to_server_root(self):
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            assert main._ntfy_publish(
                _cfg(), topic="ghsudo-test", title="T", message="M"
            )
        assert fake.published == [
            {"topic": "ghsudo-test", "title": "T", "message": "M"}
        ]

    def test_tags_sent_as_json_array_not_bare_string(self):
        # ntfy's JSON API rejects a bare string for "tags" with HTTP 400 —
        # it must be an array, even for a single tag.
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            main._ntfy_publish(
                _cfg(),
                topic="ghsudo-test",
                title="T",
                message="M",
                tags="closed_lock_with_key",
            )
        assert fake.published[0]["tags"] == ["closed_lock_with_key"]
        assert isinstance(fake.published[0]["tags"], list)

    def test_comma_separated_tags_become_multiple_array_entries(self):
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            main._ntfy_publish(
                _cfg(), topic="ghsudo-test", title="T", message="M", tags="a, b ,c"
            )
        assert fake.published[0]["tags"] == ["a", "b", "c"]

    def test_no_tags_key_when_tags_omitted(self):
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            main._ntfy_publish(_cfg(), topic="ghsudo-test", title="T", message="M")
        assert "tags" not in fake.published[0]

    def test_multiline_message_survives(self):
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            main._ntfy_publish(
                _cfg(), topic="ghsudo-test", title="T", message="line1\nline2 ✓"
            )
        assert fake.published[0]["message"] == "line1\nline2 ✓"

    def test_auth_token_sent_as_bearer(self):
        fake = MagicMock(return_value=_FakeResponse())
        with _patch_urlopen(fake):
            main._ntfy_publish(
                _cfg(auth_token="tk_x"), topic="t", title="T", message="M"
            )
        req = fake.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tk_x"

    def test_network_failure_returns_false(self):
        fake = _FakeUrlopen(publish_error=OSError("no route to host"))
        with _patch_urlopen(fake):
            assert not main._ntfy_publish(
                _cfg(), topic="ghsudo-test", title="T", message="M"
            )

    def test_actions_included(self):
        fake = _FakeUrlopen()
        actions = main._ntfy_reply_actions(_cfg(), "reply-topic")
        with _patch_urlopen(fake):
            main._ntfy_publish(
                _cfg(), topic="t", title="T", message="M", actions=actions
            )
        sent = fake.published[0]["actions"]
        assert [a["label"] for a in sent] == ["Allow", "Deny"]
        assert [a["body"] for a in sent] == ["allow", "deny"]
        assert all(a["url"] == "https://ntfy.example/reply-topic" for a in sent)

    def test_actions_carry_auth_for_self_hosted(self):
        actions = main._ntfy_reply_actions(_cfg(auth_token="tk_x"), "reply-topic")
        assert all(a["headers"]["Authorization"] == "Bearer tk_x" for a in actions)


class TestNotifyAsync:
    """Heads-up notifications must never block or break the caller."""

    def test_publishes_command_and_repo(self):
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            main._notify_async(_cfg(), "gh pr merge 1", "acme", "acme/widget").join(5)
        assert fake.published[0]["message"].count("acme/widget") == 1
        assert "gh pr merge 1" in fake.published[0]["message"]

    def test_network_failure_is_swallowed(self):
        fake = _FakeUrlopen(publish_error=OSError("down"))
        with _patch_urlopen(fake):
            main._notify_async(_cfg(), "gh pr merge 1", "acme", None).join(5)

    def test_returns_immediately_when_publish_hangs(self):
        started = threading.Event()
        release = threading.Event()

        def _hang(_req, timeout=None):
            started.set()
            release.wait(10)
            return _FakeResponse()

        try:
            with _patch_urlopen(_hang):
                begin = time.monotonic()
                main._notify_async(_cfg(), "gh pr merge 1", "acme", None)
                elapsed = time.monotonic() - begin
                assert started.wait(5)
                assert elapsed < 1
        finally:
            release.set()


# ---------------------------------------------------------------------------
# Remote-approve channel
# ---------------------------------------------------------------------------


class TestNtfyChannel:
    """The reply stream decides only on an explicit allow/deny."""

    def _run(self, lines, **kwargs):
        fake = _FakeUrlopen(stream=_FakeStream(lines, **kwargs))
        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5),
            "gh pr merge 1",
            "acme",
            "acme/widget",
        )
        with _patch_urlopen(fake):
            return chan.run(), fake, chan

    def test_allow(self):
        result, fake, _ = self._run(
            [_line("open"), _line("keepalive"), _line("message", "allow")]
        )
        assert result is True
        assert fake.published[0]["topic"] == "ghsudo-test"

    def test_deny(self):
        result, _, _ = self._run([_line("message", "deny")])
        assert result is False

    def test_reply_is_case_and_whitespace_insensitive(self):
        result, _, _ = self._run([_line("message", "  Allow\n")])
        assert result is True

    def test_malformed_lines_ignored(self):
        result, _, _ = self._run(
            [b"not json\n", b"\n", b"[]\n", _line("message", "allow")]
        )
        assert result is True

    def test_unrecognized_body_ignored_then_denied_on_timeout(self):
        result, _, _ = self._run(
            [_line("message", "maybe")], raise_at_end=TimeoutError()
        )
        assert result is False

    def test_keepalive_only_stream_times_out_as_denial(self):
        result, _, _ = self._run([_line("keepalive")] * 3, raise_at_end=TimeoutError())
        assert result is False

    def test_non_oserror_stream_failure_is_contained(self):
        """http.client raises HTTPException, which is not an OSError."""
        result, _, _ = self._run(
            [_line("open")], raise_at_end=RuntimeError("IncompleteRead")
        )
        assert result is None

    def test_stream_ending_early_is_unavailable(self):
        result, _, _ = self._run([_line("open")])
        assert result is None

    def test_publish_failure_is_unavailable(self):
        fake = _FakeUrlopen(
            stream=_FakeStream([]), publish_error=OSError("no route to host")
        )
        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5), "cmd", "acme", None
        )
        with _patch_urlopen(fake):
            assert chan.run() is None
        assert fake.stream.closed

    def test_subscribe_failure_is_unavailable(self):
        def _fail(_req, timeout=None):
            raise OSError("no route to host")

        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5), "cmd", "acme", None
        )
        with _patch_urlopen(_fail):
            assert chan.run() is None

    def test_deadline_starts_before_publish_not_after(self, monkeypatch):
        """The reply-acceptance window must not be extended by however long
        subscribe+publish take — it must match the GUI channel's deadline,
        which starts at thread launch, not after those calls complete."""
        clock = {"t": 1000.0}
        monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])

        real_publish = main._ntfy_publish

        def slow_publish(*args, **kwargs):
            clock["t"] += 10  # simulate a slow publish, past the 5s timeout
            return real_publish(*args, **kwargs)

        monkeypatch.setattr(main, "_ntfy_publish", slow_publish)

        fake = _FakeUrlopen(stream=_FakeStream([_line("message", "allow")]))
        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5), "cmd", "acme", None
        )
        with _patch_urlopen(fake):
            result = chan.run()

        # Deadline (t=1000+5=1005) already elapsed by the time publish
        # returns (t=1010), so this must read as a timeout/denial even
        # though an "allow" line is sitting right there in the stream.
        assert result is False

    def test_subscribes_before_publishing(self):
        """A reply tapped the instant the push lands must not be missed."""
        order: list[str] = []
        fake = _FakeUrlopen(stream=_FakeStream([_line("message", "allow")]))
        real_call = fake.__call__

        def _record(req, timeout=None):
            order.append("subscribe" if req.full_url.endswith("/json") else "publish")
            return real_call(req, timeout=timeout)

        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5), "cmd", "acme", None
        )
        with _patch_urlopen(_record):
            chan.run()
        assert order == ["subscribe", "publish"]

    def test_reply_topic_is_fresh_and_never_the_configured_topic(self):
        seen = set()
        for _ in range(3):
            _, fake, _ = self._run([_line("message", "allow")])
            url = fake.subscribed[0]
            assert "/ghsudo-test/" not in url
            seen.add(url)
        assert len(seen) == 3

    def test_cancel_before_run_returns_unavailable(self):
        fake = _FakeUrlopen(stream=_FakeStream([_line("message", "allow")]))
        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5), "cmd", "acme", None
        )
        chan.cancel()
        with _patch_urlopen(fake):
            assert chan.run() is None
        assert not fake.published

    def test_cancel_while_waiting_closes_the_stream(self):
        subscribed = threading.Event()

        class _BlockingStream(_FakeStream):
            def __iter__(self):
                subscribed.set()
                while not self.closed:
                    time.sleep(0.01)
                raise ValueError("I/O operation on closed file")
                yield  # pragma: no cover — keeps this a generator

        fake = _FakeUrlopen(stream=_BlockingStream([]))
        chan = main._NtfyChannel(
            _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=30), "cmd", "acme", None
        )
        result: list[bool | None] = []
        with _patch_urlopen(fake):
            worker = threading.Thread(target=lambda: result.append(chan.run()))
            worker.start()
            assert subscribed.wait(5)
            chan.cancel()
            worker.join(5)
        assert result == [None]
        assert fake.stream.closed


class TestParseNtfyReply:
    """Line-level parsing decides allow/deny and ignores everything else."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (_line("message", "allow"), True),
            (_line("message", "deny"), False),
            (_line("message", "ALLOW"), True),
            (_line("message", "allow the thing"), None),
            (_line("keepalive"), None),
            (_line("open"), None),
            (b"", None),
            (b"\n", None),
            (b"{not json\n", None),
            (b'"allow"\n', None),
            (b"123\n", None),
            (json.dumps({"event": "message"}).encode(), None),
        ],
    )
    def test_parse(self, raw, expected):
        assert main._parse_ntfy_reply(raw) is expected


# ---------------------------------------------------------------------------
# Channel race
# ---------------------------------------------------------------------------


def _channel(name, result, *, delay=0.0, raises=None):
    """Build a (name, run, cancel) triple plus a record of cancellation."""
    cancelled = threading.Event()

    def run():
        if delay:
            threading.Event().wait(delay)
        if raises is not None:
            raise raises
        return result

    return (name, run, cancelled.set), cancelled


class TestRaceApproval:
    """First decisive answer wins; the losing channel is cancelled."""

    def test_fast_approve_beats_slow_channel(self):
        fast, _ = _channel("fast", True)
        slow, slow_cancelled = _channel("slow", False, delay=5)
        assert main._race_approval([fast, slow], timeout=5) is True
        assert slow_cancelled.is_set()

    def test_fast_approve_beats_slow_deny_in_either_order(self):
        slow, _ = _channel("slow", False, delay=5)
        fast, fast_cancelled = _channel("fast", True)
        assert main._race_approval([slow, fast], timeout=5) is True
        assert not fast_cancelled.is_set()

    def test_fast_deny_wins(self):
        fast, _ = _channel("fast", False)
        slow, slow_cancelled = _channel("slow", True, delay=5)
        assert main._race_approval([fast, slow], timeout=5) is False
        assert slow_cancelled.is_set()

    def test_unavailable_channel_does_not_win(self):
        dead, _ = _channel("dead", None)
        slow, _ = _channel("slow", True, delay=0.2)
        assert main._race_approval([dead, slow], timeout=5) is True

    def test_all_unavailable_returns_none(self):
        a, _ = _channel("a", None)
        b, _ = _channel("b", None)
        assert main._race_approval([a, b], timeout=5) is None

    def test_crashing_channel_does_not_stall_the_race(self):
        boom, _ = _channel("boom", None, raises=RuntimeError("kaboom"))
        good, _ = _channel("good", True, delay=0.1)
        assert main._race_approval([boom, good], timeout=5) is True

    def test_overall_timeout_returns_none_and_cancels_all(self):
        slow, cancelled = _channel("slow", True, delay=5)
        assert main._race_approval([slow], timeout=0.1) is None
        assert cancelled.is_set()

    def test_no_channels_returns_none(self):
        assert main._race_approval([], timeout=1) is None


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
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        begin = time.monotonic()
        assert main._run_gui(["/bin/sleep", "30"], timeout=60, cancel=cancel) is None
        assert time.monotonic() - begin < 10


# ---------------------------------------------------------------------------
# _ask_approval integration
# ---------------------------------------------------------------------------


@pytest.fixture
def no_ntfy(monkeypatch):
    monkeypatch.setattr(main, "_load_ntfy_config", lambda: None)


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


class TestAskApprovalNotifyMode:
    """Notify mode informs the user without touching the approval decision."""

    def test_gui_still_authoritative(self, monkeypatch):
        fake = _FakeUrlopen()
        monkeypatch.setattr(main, "_load_ntfy_config", lambda: _cfg())
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: False)
        with _patch_urlopen(fake):
            assert main._ask_approval("gh pr merge 1", "acme") is False

    def test_no_display_still_exits_no_interactive(self, monkeypatch):
        monkeypatch.setattr(main, "_load_ntfy_config", lambda: _cfg())
        monkeypatch.setattr(main, "_has_display", lambda: False)
        with _patch_urlopen(_FakeUrlopen()), pytest.raises(SystemExit) as exc:
            main._ask_approval("gh pr merge 1", "acme")
        assert exc.value.code == main.EXIT_NO_INTERACTIVE


class TestAskApprovalRemoteApprove:
    """Remote-approve races the local dialog against the push reply."""

    def _cfg_remote(self):
        return _cfg(mode=main._MODE_REMOTE_APPROVE, timeout=5)

    def test_ntfy_only_when_headless(self, monkeypatch):
        monkeypatch.setattr(main, "_load_ntfy_config", self._cfg_remote)
        monkeypatch.setattr(main, "_has_display", lambda: False)
        monkeypatch.setattr(main._NtfyChannel, "run", lambda self: True)
        assert main._ask_approval("gh pr merge 1", "acme") is True

    def test_ntfy_wins_over_pending_gui(self, monkeypatch):
        gui_cancelled = threading.Event()

        def _slow_gui(*_a, cancel=None, **_k):
            assert cancel is not None
            if cancel.wait(5):
                gui_cancelled.set()
            return None

        monkeypatch.setattr(main, "_load_ntfy_config", self._cfg_remote)
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", _slow_gui)
        monkeypatch.setattr(main._NtfyChannel, "run", lambda self: False)
        assert main._ask_approval("gh pr merge 1", "acme") is False
        assert gui_cancelled.wait(5)

    def test_gui_wins_over_pending_ntfy(self, monkeypatch):
        ntfy_cancelled = threading.Event()

        monkeypatch.setattr(main, "_load_ntfy_config", self._cfg_remote)
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: True)
        monkeypatch.setattr(
            main._NtfyChannel, "run", lambda self: ntfy_cancelled.wait(5) and None
        )
        monkeypatch.setattr(
            main._NtfyChannel, "cancel", lambda self: ntfy_cancelled.set()
        )
        assert main._ask_approval("gh pr merge 1", "acme") is True
        assert ntfy_cancelled.wait(5)

    def test_gui_dialog_window_matches_the_ntfy_window(self, monkeypatch):
        """The 60s dialog timeout must not auto-deny while the phone can still reply."""
        seen: dict = {}

        def _record_gui(*_a, timeout=None, **_k):
            seen["timeout"] = timeout
            return True

        monkeypatch.setattr(main, "_load_ntfy_config", self._cfg_remote)
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", _record_gui)
        monkeypatch.setattr(main._NtfyChannel, "run", lambda self: True)
        main._ask_approval("gh pr merge 1", "acme")
        assert seen["timeout"] == max(main._GUI_TIMEOUT, 5)

    def test_both_channels_unavailable_exits_no_interactive(self, monkeypatch):
        monkeypatch.setattr(main, "_load_ntfy_config", self._cfg_remote)
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: None)
        monkeypatch.setattr(main._NtfyChannel, "run", lambda self: None)
        with pytest.raises(SystemExit) as exc:
            main._ask_approval("gh pr merge 1", "acme")
        assert exc.value.code == main.EXIT_NO_INTERACTIVE


# ---------------------------------------------------------------------------
# cmd_run regression
# ---------------------------------------------------------------------------


class TestCmdRunNotifyRegression:
    """A broken ntfy must never stop an approved command from running."""

    def test_command_runs_when_notify_publish_fails(self, monkeypatch):
        monkeypatch.setattr(main, "_load_ntfy_config", lambda: _cfg())
        monkeypatch.setattr(main, "_load_token", lambda org: "gh-token")
        monkeypatch.setattr(main, "_detect_repo_slug", lambda cmd: "acme/widget")
        monkeypatch.setattr(main, "_has_display", lambda: True)
        monkeypatch.setattr(main, "_ask_gui", lambda *a, **k: True)
        completed = MagicMock(returncode=0)
        monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: completed)

        fake = _FakeUrlopen(publish_error=OSError("network is down"))
        with _patch_urlopen(fake):
            assert main.cmd_run(["gh", "pr", "merge", "1"], org="acme") == main.EXIT_OK


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestSetupNtfyFlags:
    """--setup-ntfy accepts its options in both spaced and inline form."""

    def test_spaced_values(self):
        assert main._parse_ntfy_flags(
            ["--mode", "remote-approve", "--server", "https://x", "--topic", "t"]
        ) == {"mode": "remote-approve", "server": "https://x", "topic": "t"}

    def test_inline_values(self):
        assert main._parse_ntfy_flags(["--mode=notify", "--topic=t"]) == {
            "mode": "notify",
            "topic": "t",
        }

    def test_no_flags(self):
        assert main._parse_ntfy_flags([]) == {}

    def test_unknown_flag_rejected(self):
        assert main._parse_ntfy_flags(["--wat"]) is None

    def test_missing_value_rejected(self):
        assert main._parse_ntfy_flags(["--topic"]) is None


class TestCmdSetupNtfy:
    """Setup validates, test-publishes, and only then stores the config."""

    def test_stores_config_after_successful_test_publish(self, ntfy_home):
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            rc = main.cmd_setup_ntfy(
                mode=main._MODE_REMOTE_APPROVE,
                server="https://ntfy.example",
                topic="ghsudo-abc",
            )
        assert rc == main.EXIT_OK
        stored = main._load_ntfy_config()
        assert stored is not None
        assert (stored.topic, stored.mode) == ("ghsudo-abc", main._MODE_REMOTE_APPROVE)
        assert fake.published[0]["topic"] == "ghsudo-abc"

    def test_existing_config_kept_unless_confirmed(self, ntfy_home, monkeypatch):
        main._save_ntfy_config(_cfg(topic="original"))
        monkeypatch.setattr("builtins.input", lambda _p: "no")
        with _patch_urlopen(_FakeUrlopen(publish_error=AssertionError("no network"))):
            assert main.cmd_setup_ntfy(topic="replacement") == main.EXIT_ERROR
        stored = main._load_ntfy_config()
        assert stored is not None
        assert stored.topic == "original"

    def test_existing_config_replaced_when_confirmed(self, ntfy_home, monkeypatch):
        main._save_ntfy_config(_cfg(topic="original"))
        monkeypatch.setattr("builtins.input", lambda _p: "yes")
        with _patch_urlopen(_FakeUrlopen()):
            rc = main.cmd_setup_ntfy(topic="replacement", server="https://ntfy.example")
        assert rc == main.EXIT_OK
        stored = main._load_ntfy_config()
        assert stored is not None
        assert stored.topic == "replacement"

    def test_failed_test_publish_stores_nothing(self, ntfy_home):
        fake = _FakeUrlopen(publish_error=OSError("unreachable"))
        with _patch_urlopen(fake):
            rc = main.cmd_setup_ntfy(topic="ghsudo-abc", server="https://ntfy.example")
        assert rc == main.EXIT_ERROR
        assert not main._NOTIFY_PATH.exists()

    def test_invalid_mode_rejected(self, ntfy_home):
        rc = main.cmd_setup_ntfy(mode="auto-approve", topic="ghsudo-abc")
        assert rc == main.EXIT_ERROR
        assert not main._NOTIFY_PATH.exists()

    def test_invalid_topic_rejected(self, ntfy_home):
        rc = main.cmd_setup_ntfy(topic="../evil")
        assert rc == main.EXIT_ERROR
        assert not main._NOTIFY_PATH.exists()

    def test_invalid_server_rejected(self, ntfy_home):
        rc = main.cmd_setup_ntfy(topic="ghsudo-abc", server="file:///etc/passwd")
        assert rc == main.EXIT_ERROR
        assert not main._NOTIFY_PATH.exists()

    def test_generated_topic_is_random_and_valid(self):
        topics = {main._generate_ntfy_topic() for _ in range(5)}
        assert len(topics) == 5
        assert all(main._NTFY_TOPIC_RE.match(t) for t in topics)


class TestVerifyAndList:
    """--verify pings ntfy when configured; --list reports it."""

    def test_verify_reports_ntfy_failure(self, ntfy_home, monkeypatch, capsys):
        monkeypatch.setattr(main, "_list_orgs", lambda: [])
        main._save_ntfy_config(_cfg())
        with _patch_urlopen(_FakeUrlopen(publish_error=OSError("down"))):
            rc = main.cmd_verify()
        assert rc != main.EXIT_OK
        assert "ntfy" in capsys.readouterr().err

    def test_verify_pings_configured_topic(self, ntfy_home, monkeypatch):
        monkeypatch.setattr(main, "_list_orgs", lambda: [])
        main._save_ntfy_config(_cfg())
        fake = _FakeUrlopen()
        with _patch_urlopen(fake):
            main.cmd_verify()
        assert fake.published[0]["topic"] == "ghsudo-test"

    def test_verify_without_ntfy_touches_no_network(self, ntfy_home, monkeypatch):
        monkeypatch.setattr(main, "_list_orgs", lambda: [])
        with _patch_urlopen(_FakeUrlopen(publish_error=AssertionError("no network"))):
            assert main.cmd_verify() == main.EXIT_NO_TOKEN

    def test_list_footer_when_configured(self, ntfy_home, monkeypatch, capsys):
        monkeypatch.setattr(main, "_list_orgs", lambda: ["acme"])
        main._save_ntfy_config(_cfg(mode=main._MODE_REMOTE_APPROVE))
        assert main.cmd_list() == main.EXIT_OK
        err = capsys.readouterr().err
        assert "ntfy" in err
        assert main._MODE_REMOTE_APPROVE in err

    def test_list_silent_about_ntfy_when_unconfigured(
        self, ntfy_home, monkeypatch, capsys
    ):
        monkeypatch.setattr(main, "_list_orgs", lambda: ["acme"])
        assert main.cmd_list() == main.EXIT_OK
        assert "ntfy" not in capsys.readouterr().err

    def test_usage_documents_setup_ntfy(self):
        assert "--setup-ntfy" in main._USAGE
