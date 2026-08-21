#!/usr/bin/env python3
"""ghsudo — GitHub Sudo: re-execute commands with an elevated GitHub token.

Two-token model: Claude normally uses a read-only token. When a command gets
HTTP 403 (Forbidden), ghsudo re-runs it with a stored read-write token after
the user confirms via GUI popup or terminal prompt.

Supports per-organization tokens: each GitHub org/owner gets its own encrypted
token file. The org is auto-detected from command arguments or git remotes.

Token is stored AES-256-GCM encrypted, keyed to machine characteristics.
"""

from __future__ import annotations

import functools
import getpass
import hashlib
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ghsudo import __version__

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PREFIX = "[ghsudo]"
_VERSION_BYTE = b"\x01"
_PBKDF2_SALT = b"ghsu-claudius-token-encryption-v1"  # kept as-is for backward compat with existing encrypted tokens
_PBKDF2_ITERATIONS = 600_000
_NONCE_LEN = 12  # 96-bit nonce for AES-GCM
_GUI_TIMEOUT = 60  # seconds — dialog auto-denies if user doesn't respond

_CONFIG_DIR = Path.home() / ".config" / "ghsudo"
_TOKENS_DIR = _CONFIG_DIR / "tokens"
_NOTIFY_PATH = _CONFIG_DIR / "notify.enc"

_README_URL = "https://github.com/lklimek/ghsudo#readme"
_USER_AGENT = f"ghsudo/{__version__}"

_MACHINE_KEY_NOTE = (
    "Note: the encryption key is derived from this machine's identifiers, so "
    "code running as you can re-derive it. It protects stolen disks and stray "
    "backups, not this machine."
)

# ntfy notification channel
_MODE_NOTIFY = "notify"
_MODE_REMOTE_APPROVE = "remote-approve"
_NTFY_MODES = (_MODE_NOTIFY, _MODE_REMOTE_APPROVE)
_NTFY_DEFAULT_SERVER = "https://ntfy.sh"
_NTFY_DEFAULT_TIMEOUT = 300  # seconds — a phone reply is slower than a click
_NTFY_MAX_TIMEOUT = 3600
_NTFY_PUBLISH_TIMEOUT = 10
_NTFY_CANCEL_NOTICE_TIMEOUT = 2  # bounded, synchronous — see _notify_already_handled
_NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REPLY_ALLOW = "allow"
_REPLY_DENY = "deny"
_REPLY_TOPIC_BYTES = 24
_CANCEL_POLL_INTERVAL = 0.2  # seconds between cancellation checks while a dialog is up
_RACE_SLACK = 5  # seconds the race waits past a channel's own deadline

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DENIED = 2
EXIT_NO_INTERACTIVE = 3
EXIT_NO_TOKEN = 4


_VERBOSE = os.environ.get("GHSUDO_DEBUG", "") != ""
_T0 = time.monotonic()


def _err(msg: str) -> None:
    print(f"{_PREFIX} {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"{_PREFIX} {msg}", file=sys.stderr)


def _debug(msg: str) -> None:
    if _VERBOSE:
        elapsed = time.monotonic() - _T0
        print(f"{_PREFIX} [{elapsed:6.3f}s] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Machine key derivation
# ---------------------------------------------------------------------------


def _get_machine_id() -> str | None:
    """Return a stable, platform-specific machine identifier."""
    system = platform.system()

    if system == "Linux":
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                return Path(path).read_text().strip()
            except OSError:
                continue
        return None

    if system == "Darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in out.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        except (subprocess.SubprocessError, IndexError):
            pass
        return None

    if system == "Windows":
        # Try WMI first
        try:
            out = subprocess.run(
                ["wmic", "csproduct", "get", "UUID", "/value"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in out.stdout.splitlines():
                if line.startswith("UUID="):
                    return line.split("=", 1)[1].strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        # Fallback: registry
        try:
            import winreg  # noqa: PLC0415

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "MachineGuid")
                return str(val)
        except Exception:  # noqa: BLE001
            pass
        return None

    return None


@functools.cache
def _derive_machine_key() -> bytes:
    """Derive a 32-byte AES-256 key from stable machine identifiers.

    Cached: the 600k-iteration KDF is deterministic and several call sites
    (token store, ntfy config) need the key within one run.
    """
    _debug("deriving machine key")
    components: list[str] = []

    mid = _get_machine_id()
    if mid:
        components.append(mid)

    components.append(socket.gethostname())
    components.append(getpass.getuser())

    if not components:
        raise RuntimeError("Cannot derive machine key: no stable identifiers")

    raw = "|".join(components).encode("utf-8")
    key = hashlib.pbkdf2_hmac("sha256", raw, _PBKDF2_SALT, _PBKDF2_ITERATIONS)
    _debug("machine key derived")
    return key


# ---------------------------------------------------------------------------
# Encryption (AES-256-GCM)
# ---------------------------------------------------------------------------


def _require_cryptography():
    """Import and return AESGCM, or exit with actionable message."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import (  # noqa: PLC0415
            AESGCM,
        )

        return AESGCM
    except ImportError:
        _err("Required package 'cryptography' not installed.")
        _err("Install it with:  pip install cryptography")
        sys.exit(EXIT_ERROR)


def _encrypt_blob(plaintext: str, key: bytes) -> bytes:
    AESGCM = _require_cryptography()
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _VERSION_BYTE + nonce + ct


def _decrypt_blob(data: bytes, key: bytes) -> str:
    AESGCM = _require_cryptography()
    if len(data) < 1 + _NONCE_LEN + 1:
        raise ValueError("Encrypted file is too short or corrupted")
    version = data[0:1]
    if version != _VERSION_BYTE:
        raise ValueError(f"Unknown encrypted format version: {version!r}")
    nonce = data[1 : 1 + _NONCE_LEN]
    ct = data[1 + _NONCE_LEN :]
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")


# ---------------------------------------------------------------------------
# Token storage (per-org)
# ---------------------------------------------------------------------------

_ORG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_org_name(org: str) -> str:
    """Validate and normalize an org/owner name."""
    org = org.strip().lower()
    if not _ORG_NAME_RE.match(org):
        _err(f"Invalid org name: {org!r}")
        _err("Must match: letters, digits, dots, hyphens, underscores.")
        sys.exit(EXIT_ERROR)
    return org


def _token_path(org: str) -> Path:
    return _TOKENS_DIR / f"{org}.enc"


def _list_orgs() -> list[str]:
    """Return sorted list of orgs with stored tokens."""
    if not _TOKENS_DIR.exists():
        return []
    return sorted(p.stem for p in _TOKENS_DIR.glob("*.enc"))


def _save_token(org: str, token: str) -> None:
    key = _derive_machine_key()
    blob = _encrypt_blob(token, key)
    _TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    path = _token_path(org)
    path.write_bytes(blob)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows — rely on user-profile ACLs


def _load_token(org: str) -> str:
    path = _token_path(org)
    if not path.exists():
        orgs = _list_orgs()
        _err(f"ERROR: No token stored for org '{org}'.\n")
        if orgs:
            _err(f"Available orgs: {', '.join(orgs)}\n")
        _err("To set up a token, run:")
        _err(f"    ghsudo --setup {org}\n")
        _err("This will prompt you for a GitHub Personal Access Token with")
        _err("write permissions and store it encrypted on this machine.\n")
        _err(f"See: {_README_URL}")
        sys.exit(EXIT_NO_TOKEN)

    key = _derive_machine_key()
    data = path.read_bytes()
    try:
        return _decrypt_blob(data, key)
    except Exception:  # noqa: BLE001
        _err(f"Failed to decrypt token for org '{org}'.")
        _err("Was it stored on a different machine, or did the hostname change?")
        _err(f"Re-run:  ghsudo --setup {org}")
        sys.exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# Org auto-detection
# ---------------------------------------------------------------------------


def _parse_repo_slug(value: str) -> str | None:
    """Validate and normalize an owner/repo slug. Returns None if invalid."""
    value = value.strip().lower()
    parts = value.split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return value
    return None


def _detect_repo_slug_from_args(cmd: list[str]) -> str | None:
    """Extract owner/repo slug from -R/--repo in gh command args."""
    for i, arg in enumerate(cmd):
        if arg in ("-R", "--repo") and i + 1 < len(cmd):
            slug = _parse_repo_slug(cmd[i + 1])
            if slug:
                return slug
        # Handle --repo=owner/repo
        if arg.startswith("--repo="):
            slug = _parse_repo_slug(arg.split("=", 1)[1])
            if slug:
                return slug
        if arg.startswith("-R") and len(arg) > 2:
            slug = _parse_repo_slug(arg[2:])
            if slug:
                return slug
    return None


def _detect_repo_slug_from_git_remote() -> str | None:
    """Extract owner/repo slug from the current repo's origin remote."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2).lower()}"

    # HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2).lower()}"

    return None


def _detect_repo_slug(cmd: list[str]) -> str | None:
    """Auto-detect owner/repo slug from command args, then git remote."""
    slug = _detect_repo_slug_from_args(cmd)
    if slug:
        return slug
    return _detect_repo_slug_from_git_remote()


def _detect_org_from_args(cmd: list[str]) -> str | None:
    """Extract org from -R/--repo owner/repo in gh command args."""
    slug = _detect_repo_slug_from_args(cmd)
    if slug and "/" in slug:
        return slug.split("/")[0]
    return None


def _detect_org_from_git_remote() -> str | None:
    """Extract org from the current repo's origin remote."""
    slug = _detect_repo_slug_from_git_remote()
    if slug and "/" in slug:
        return slug.split("/")[0]
    return None


def _detect_org(cmd: list[str]) -> str | None:
    """Auto-detect org from command args, then git remote."""
    org = _detect_org_from_args(cmd)
    if org:
        return org
    return _detect_org_from_git_remote()


# ---------------------------------------------------------------------------
# ntfy notification channel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NtfyConfig:
    """Settings for the ntfy channel, stored encrypted at rest."""

    topic: str
    server: str = _NTFY_DEFAULT_SERVER
    mode: str = _MODE_NOTIFY
    auth_token: str | None = None
    timeout: int = _NTFY_DEFAULT_TIMEOUT
    enabled: bool = True


def _normalize_server(server: str) -> str | None:
    """Return *server* without a trailing slash, or None if it is not http(s)."""
    from urllib.parse import urlsplit  # noqa: PLC0415

    server = server.strip().rstrip("/")
    parts = urlsplit(server)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.query or parts.fragment:
        # Reply action URLs are built by appending "/{topic}" to the whole
        # server string — a query or fragment component (e.g. a "#..."
        # anchor) would silently break that, and a fragment in particular
        # is never transmitted to the server at all. A path is fine (e.g.
        # a self-hosted instance behind a reverse-proxy subpath).
        return None
    return server


def _generate_ntfy_topic() -> str:
    """Return a fresh, unguessable topic name."""
    import secrets  # noqa: PLC0415

    return f"ghsudo-{secrets.token_hex(8)}"


def _build_ntfy_config(data: dict) -> _NtfyConfig | None:
    """Validate a raw settings mapping into a config, or None if unusable.

    Anything unrecognised is coerced towards the safe end: an unknown mode
    becomes ``notify``.
    """
    topic = str(data.get("topic") or "").strip()
    if not _NTFY_TOPIC_RE.match(topic):
        _debug(f"ntfy: invalid topic {topic!r} — ignoring configuration")
        return None

    server = _normalize_server(str(data.get("server") or _NTFY_DEFAULT_SERVER))
    if server is None:
        _debug("ntfy: server must be an http(s) URL — ignoring configuration")
        return None

    mode = str(data.get("mode") or _MODE_NOTIFY).strip()
    if mode not in _NTFY_MODES:
        _debug(f"ntfy: unknown mode {mode!r} — falling back to {_MODE_NOTIFY}")
        mode = _MODE_NOTIFY

    try:
        timeout = int(data.get("timeout", _NTFY_DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _NTFY_DEFAULT_TIMEOUT
    if not 1 <= timeout <= _NTFY_MAX_TIMEOUT:
        timeout = _NTFY_DEFAULT_TIMEOUT

    auth_token = data.get("auth_token") or None
    return _NtfyConfig(
        topic=topic,
        server=server,
        mode=mode,
        auth_token=str(auth_token) if auth_token else None,
        timeout=timeout,
        enabled=bool(data.get("enabled", True)),
    )


def _load_ntfy_config() -> _NtfyConfig | None:
    """Return the effective ntfy config, or None when not configured.

    Read only from the on-disk encrypted config — never from the process
    environment. The agent invoking ghsudo controls its own child
    environment, so an env-settable channel (topic, server, or mode) would
    let it redirect or approve notifications itself.
    """
    import json  # noqa: PLC0415

    if not _NOTIFY_PATH.exists():
        return None
    try:
        raw = _decrypt_blob(_NOTIFY_PATH.read_bytes(), _derive_machine_key())
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — never break a run over notifications
        _err(f"Failed to read {_NOTIFY_PATH} ({exc}).")
        _err("Re-run:  ghsudo --setup-ntfy")
        return None
    if not isinstance(data, dict):
        _debug("ntfy: config is not a JSON object — ignoring")
        return None

    cfg = _build_ntfy_config(data)
    if cfg is None or not cfg.enabled:
        return None
    return cfg


def _save_ntfy_config(cfg: _NtfyConfig) -> None:
    """Write the config encrypted, readable only by this user.

    Created with mode 0600 from the first open (not chmod'd afterwards) and
    atomically renamed into place, so no other local user can ever observe a
    window where the blob exists at the umask's default (often world/group
    readable) permissions.
    """
    import json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    blob = _encrypt_blob(json.dumps(asdict(cfg)), _derive_machine_key())
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=_CONFIG_DIR, prefix=".notify-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        try:
            tmp_path.chmod(0o600)
        except OSError:
            pass  # Windows — rely on user-profile ACLs
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        tmp_path.replace(_NOTIFY_PATH)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _ntfy_headers(cfg: _NtfyConfig) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if cfg.auth_token:
        headers["Authorization"] = f"Bearer {cfg.auth_token}"
    return headers


def _ntfy_publish(
    cfg: _NtfyConfig,
    *,
    topic: str,
    title: str,
    message: str,
    actions: list[dict] | None = None,
    tags: str | None = None,
    priority: int | None = None,
    timeout: float = _NTFY_PUBLISH_TIMEOUT,
    id_out: list[str] | None = None,
) -> bool:
    """Publish one notification. Returns False on any delivery failure.

    Uses ntfy's JSON API rather than headers so that command text with newlines
    or non-ASCII characters survives intact. ``tags`` is a comma-separated
    string of one or more ntfy emoji-short-code tags (e.g. "closed_lock_with_key"
    or "tag_one,tag_two") — ntfy's JSON API requires the wire value to be an
    array, not a bare string, so it is split and wrapped here.

    If *id_out* is given and the publish succeeds, the server-assigned
    message id is appended to it (best-effort — left empty if the response
    body doesn't parse or carry one). Only reads the response body when
    *id_out* is passed, to avoid the extra work for callers that don't need it.
    """
    import json  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    payload: dict = {"topic": topic, "title": title, "message": message}
    if actions:
        payload["actions"] = actions
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if priority is not None:
        payload["priority"] = priority

    try:
        req = Request(  # noqa: S310 — scheme validated by _normalize_server
            cfg.server,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **_ntfy_headers(cfg)},
        )
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            body = resp.read() if id_out is not None else b""
    except (OSError, ValueError) as exc:  # URLError/HTTPError are OSError subclasses
        _debug(f"ntfy: publish to '{topic}' failed: {exc}")
        return False
    if not 200 <= status < 300:
        _debug(f"ntfy: publish to '{topic}' returned HTTP {status}")
        return False
    if id_out is not None:
        try:
            msg_id = json.loads(body).get("id")
        except (json.JSONDecodeError, AttributeError):
            msg_id = None
        if msg_id:
            id_out.append(msg_id)
    _debug(f"ntfy: published to '{topic}'")
    return True


def _ntfy_delete(
    cfg: _NtfyConfig,
    topic: str,
    message_id: str,
    *,
    timeout: float = _NTFY_PUBLISH_TIMEOUT,
) -> bool:
    """Delete a previously published message from ntfy.

    Removes it from both the OS notification drawer and the app's own
    history — verified live against ntfy.sh; the documented ``/clear``
    endpoint only clears the drawer and leaves it in the app's history.
    """
    from urllib.parse import quote  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    try:
        req = Request(  # noqa: S310 — scheme validated by _normalize_server
            f"{cfg.server}/{quote(topic, safe='')}/{quote(message_id, safe='')}",
            method="DELETE",
            headers=_ntfy_headers(cfg),
        )
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
    except (OSError, ValueError) as exc:
        _debug(f"ntfy: delete of message '{message_id}' failed: {exc}")
        return False
    if not 200 <= status < 300:
        _debug(f"ntfy: delete of message '{message_id}' returned HTTP {status}")
        return False
    _debug(f"ntfy: deleted message '{message_id}'")
    return True


def _format_ntfy_message(cmd_str: str, org: str, repo: str | None) -> str:
    target = f"Repository: {repo}" if repo else f"Organization: {org}"
    return f"{target}\nCommand: {cmd_str}"


def _notify_async(
    cfg: _NtfyConfig, cmd_str: str, org: str, repo: str | None
) -> threading.Thread:
    """Send a heads-up push in the background, never blocking the approval flow."""

    def _send() -> None:
        try:
            _ntfy_publish(
                cfg,
                topic=cfg.topic,
                title="ghsudo: elevated GitHub access requested",
                message=_format_ntfy_message(cmd_str, org, repo),
                tags="key",
                priority=4,
            )
        except Exception as exc:  # noqa: BLE001 — a heads-up must never break a run
            _debug(f"ntfy: notification failed: {exc}")

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


def _ntfy_reply_actions(cfg: _NtfyConfig, reply_topic: str) -> list[dict]:
    """Build the Allow/Deny notification buttons that post to *reply_topic*."""
    url = f"{cfg.server}/{reply_topic}"

    def _action(label: str, body: str) -> dict:
        action = {
            "action": "http",
            "label": label,
            "url": url,
            "method": "POST",
            "body": body,
            "clear": True,
        }
        if cfg.auth_token:
            action["headers"] = {"Authorization": f"Bearer {cfg.auth_token}"}
        return action

    return [_action("Allow", _REPLY_ALLOW), _action("Deny", _REPLY_DENY)]


def _parse_ntfy_reply(raw: bytes) -> bool | None:
    """Decode one line of the reply stream: True/False for allow/deny, else None."""
    import json  # noqa: PLC0415

    if not raw.strip():
        return None
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _debug("ntfy: ignoring malformed line in reply stream")
        return None
    if not isinstance(event, dict) or event.get("event") != "message":
        return None

    body = str(event.get("message", "")).strip().lower()
    if body == _REPLY_ALLOW:
        return True
    if body == _REPLY_DENY:
        return False
    _debug(f"ntfy: ignoring unrecognised reply {body!r}")
    return None


class _NtfyChannel:
    """Approval over ntfy: push Allow/Deny buttons, then await the tapped reply.

    ``run()`` returns True/False for an explicit answer, or None when the
    channel could not reach the user at all; an expired timeout counts as a
    denial, matching the GUI dialog.
    """

    def __init__(
        self, cfg: _NtfyConfig, cmd_str: str, org: str, repo: str | None
    ) -> None:
        self._cfg = cfg
        self._cmd_str = cmd_str
        self._org = org
        self._repo = repo
        self._lock = threading.Lock()
        self._stream = None
        self._cancelled = False
        self._published = False
        self._message_id: str | None = None

    def cancel(self) -> None:
        """Abandon the wait.

        Does NOT synchronously close the reply stream. Closing a socket
        response object from a different thread while a reader thread is
        blocked inside it does not reliably interrupt that blocked read —
        it's a well-known cross-thread hazard, and closing here empirically
        blocks for as long as the reader's own remaining timeout (measured:
        ~5s wait on a 5s-timeout channel, i.e. close() effectively just
        waited for the reader to finish on its own). Calling it synchronously
        from ``_race_approval``'s cancel loop would make *ghsudo itself*
        block for up to the full ntfy timeout after the GUI already
        answered — precisely the "ghsudo still waits for ntfy" symptom this
        cancellation exists to prevent. The reader thread is a daemon
        thread: if it stays blocked, the OS reclaims it on process exit,
        same as any other abandoned socket — no user-visible cost. A
        best-effort close is still attempted, just off the calling thread
        so it can never block cancel()'s own return.
        """
        with self._lock:
            self._cancelled = True
            stream = self._stream
            published = self._published
            message_id = self._message_id
        if stream is not None:
            threading.Thread(
                target=self._close_stream, args=(stream,), daemon=True
            ).start()
        if published:
            self._discard_original_request(message_id)

    @staticmethod
    def _close_stream(stream) -> None:
        _debug("ntfy: cancelling reply stream")
        try:
            stream.close()
        except OSError:
            pass

    def _discard_original_request(self, message_id: str | None) -> None:
        """Get rid of the now-moot Allow/Deny notification on the phone.

        Prefers deleting the original message outright — verified live to
        remove it from both the OS notification drawer and the app's own
        history, unlike ntfy's ``/clear`` endpoint (drawer only). Falls back
        to a "no longer active" follow-up notice only when there's no
        message id to delete (e.g. a non-standard server that doesn't
        return one) or the delete itself fails.
        """
        if message_id and _ntfy_delete(
            self._cfg,
            self._cfg.topic,
            message_id,
            timeout=_NTFY_CANCEL_NOTICE_TIMEOUT,
        ):
            return
        self._notify_already_handled()

    def _notify_already_handled(self) -> None:
        """Fallback for when the original message can't be deleted: ntfy
        has no other API to retract a delivered push, so without this the
        original Allow/Deny notification would stay visible on the phone
        even after another channel decided. Send a follow-up so the phone
        reflects that it's no longer actionable, instead of looking like
        ghsudo is still waiting on it.

        Runs synchronously with a short, dedicated timeout — NOT fired off
        on a background daemon thread. A daemon thread has no guarantee of
        getting scheduled before ghsudo's own process exits right after
        cancel() returns (the same race that orphaned the GUI dialog
        subprocess before this same PR fixed that side); a bounded
        synchronous call trades a small, capped delay for actually
        delivering the notice.
        """
        try:
            _ntfy_publish(
                self._cfg,
                topic=self._cfg.topic,
                title="ghsudo: request no longer active",
                message=(
                    "This approval request was already resolved elsewhere "
                    "(or expired) — no action needed."
                ),
                tags="information_source",
                timeout=_NTFY_CANCEL_NOTICE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort only
            _debug(f"ntfy: already-handled notice failed: {exc}")

    def run(self) -> bool | None:
        import secrets  # noqa: PLC0415
        from urllib.request import Request, urlopen  # noqa: PLC0415

        # Started before subscribing/publishing, matching the GUI channel's
        # deadline (which starts at thread launch) — otherwise the time spent
        # opening the reply stream and posting the push would silently extend
        # the phone's reply window past what the GUI dialog actually waits.
        deadline = time.monotonic() + self._cfg.timeout

        reply_topic = f"ghsudo-{secrets.token_urlsafe(_REPLY_TOPIC_BYTES)}"

        # Subscribe before publishing: a reply tapped the instant the push
        # lands would otherwise arrive before anyone is listening.
        try:
            req = Request(  # noqa: S310 — scheme validated by _normalize_server
                f"{self._cfg.server}/{reply_topic}/json",
                headers=_ntfy_headers(self._cfg),
            )
            stream = urlopen(req, timeout=self._cfg.timeout)  # noqa: S310
        except (OSError, ValueError) as exc:
            _debug(f"ntfy: cannot open reply stream: {exc}")
            return None

        with self._lock:
            if self._cancelled:
                stream.close()
                return None
            self._stream = stream

        try:
            return self._await_reply(stream, reply_topic, deadline)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _await_reply(self, stream, reply_topic: str, deadline: float) -> bool | None:
        with self._lock:
            if self._cancelled:
                # Lost the race before we ever got to publish — nothing was
                # sent, so cancel() has nothing to send an "already handled"
                # follow-up for either.
                _debug("ntfy: cancelled before the approval request was sent")
                return None

        id_out: list[str] = []
        if not _ntfy_publish(
            self._cfg,
            topic=self._cfg.topic,
            title="ghsudo: approve elevated GitHub access?",
            message=_format_ntfy_message(self._cmd_str, self._org, self._repo),
            actions=_ntfy_reply_actions(self._cfg, reply_topic),
            tags="closed_lock_with_key",
            priority=5,
            id_out=id_out,
        ):
            _err("ntfy: could not send the approval request.")
            return None
        with self._lock:
            self._published = True
            self._message_id = id_out[0] if id_out else None

        _info(
            f"ntfy: approval request sent to '{self._cfg.topic}' "
            f"(waiting up to {self._cfg.timeout}s)"
        )
        timed_out = False
        try:
            for line in stream:
                if self._cancelled:
                    return None
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                answer = _parse_ntfy_reply(line)
                if answer is not None:
                    _info(f"ntfy: reply received — {'allowed' if answer else 'denied'}")
                    return answer
        except TimeoutError:
            timed_out = True
        except Exception as exc:  # noqa: BLE001 — a broken stream must not crash
            if self._cancelled:
                return None
            _debug(f"ntfy: reply stream failed: {exc}")
            return None

        if self._cancelled:
            return None
        if timed_out or time.monotonic() >= deadline:
            _info(f"ntfy: no reply after {self._cfg.timeout}s — auto-denied.")
            return False
        _debug("ntfy: reply stream closed before an answer arrived")
        return None


# (name, run, cancel) — ``run`` answers True/False or None when it cannot decide.
_Channel = tuple[str, Callable[[], bool | None], Callable[[], None]]


def _race_approval(
    channels: list[_Channel],
    timeout: float,
) -> bool | None:
    """Return the first decisive answer from *channels*, cancelling the losers.

    Each channel is a ``(name, run, cancel)`` triple where ``run()`` yields
    True/False, or None when that channel could not decide.
    """
    import queue  # noqa: PLC0415

    if not channels:
        return None

    results: queue.SimpleQueue = queue.SimpleQueue()

    def _worker(name: str, run: Callable[[], bool | None]) -> None:
        try:
            results.put((name, run()))
        except Exception as exc:  # noqa: BLE001 — one broken channel must not stall
            _debug(f"approval: channel '{name}' failed: {exc}")
            results.put((name, None))

    for name, run, _cancel in channels:
        threading.Thread(target=_worker, args=(name, run), daemon=True).start()

    deadline = time.monotonic() + timeout
    winner: str | None = None
    answer: bool | None = None
    for _ in channels:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            name, result = results.get(timeout=remaining)
        except queue.Empty:
            break
        if result is not None:
            winner, answer = name, result
            _debug(f"approval: '{name}' decided: {answer}")
            break
        _debug(f"approval: channel '{name}' could not reach the user")

    for name, _run, cancel in channels:
        if name != winner:
            cancel()
    return answer


# ---------------------------------------------------------------------------
# GUI approval dialogs
# ---------------------------------------------------------------------------


def _escape_for_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _escape_for_powershell(s: str) -> str:
    return s.replace("`", "``").replace('"', '`"').replace("$", "`$")


class _GuiCancel:
    """Cross-thread cancellation for the GUI dialog channel.

    Unlike a bare ``threading.Event``, ``set()`` kills the currently-running
    dialog subprocess immediately and synchronously, on the calling thread —
    it does not rely on ``_run_gui``'s own polling loop (running in a daemon
    thread) to notice and react. That cannot be guaranteed to happen before
    the whole process exits: a daemon thread is hard-killed on interpreter
    shutdown with no chance to run its cleanup, so if the winning channel
    lets ``ghsudo`` proceed and exit quickly, a loser dialog relying only on
    polling can be orphaned — process gone, subprocess (and its window)
    still alive with nothing left to ever kill it.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def set(self) -> None:
        """Cancel, killing any dialog subprocess currently registered."""
        self._event.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            _kill_gui(proc)

    def register(self, proc: subprocess.Popen) -> bool:
        """Track *proc* as the active dialog.

        Returns False (having already killed *proc*) if cancellation
        happened before registration could complete.
        """
        with self._lock:
            if self._event.is_set():
                already_cancelled = True
            else:
                self._proc = proc
                already_cancelled = False
        if already_cancelled:
            _kill_gui(proc)
        return not already_cancelled

    def unregister(self) -> None:
        with self._lock:
            self._proc = None


def _run_gui(
    cmd: list[str],
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> int | None:
    """Run a GUI command with timeout. Returns exit code, or None if not found.

    Returns 1 (denial) on timeout — the user didn't respond in time.
    Returns None when the tool is not installed, or when *cancel* fires
    because another approval channel answered first.
    """
    _debug(f"gui: launching {cmd[0]}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        _debug(f"gui: {cmd[0]} not found")
        return None

    if cancel is not None and not cancel.register(proc):
        _debug(f"gui: {cmd[0]} dismissed before it could show — already cancelled")
        return None

    try:
        deadline = time.monotonic() + timeout
        while True:
            wait = deadline - time.monotonic()
            if cancel is not None:
                wait = min(wait, _CANCEL_POLL_INTERVAL)
            if wait > 0:
                try:
                    proc.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    if cancel is not None and cancel.is_set():
                        # cancel.set() killed us mid-wait — not a real answer.
                        return None
                    _debug(f"gui: {cmd[0]} exited with {proc.returncode}")
                    return proc.returncode
            if cancel is not None and cancel.is_set():
                _debug(f"gui: {cmd[0]} dismissed — another channel answered")
                return None
            if time.monotonic() >= deadline:
                _debug(f"gui: {cmd[0]} timed out after {timeout}s, auto-denying")
                _kill_gui(proc)
                _info(f"Dialog timed out after {timeout}s — auto-denied.")
                return 1  # treat as denial, not as tool-unavailable
    finally:
        if cancel is not None:
            cancel.unregister()


def _kill_gui(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait()
    except OSError:
        # Process already exited on its own (e.g. the dialog was answered right
        # as we decided to cancel/timeout it) — nothing left to clean up.
        pass


def _format_approval_msg(cmd_str: str, org: str, repo: str | None = None) -> str:
    target = f"Repository: {repo}" if repo else f"Organization: {org}"
    return (
        "A GitHub command requires elevated (write) permissions.\n\n"
        f"{target}\n"
        f"Command to execute:\n  {cmd_str}\n\n"
        "Allow this command to run with elevated GitHub permissions?"
    )


def _ask_xmessage(
    cmd_str: str,
    org: str,
    repo: str | None = None,
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> bool | None:
    """Lightweight X11 dialog. Returns True=approved, False=denied, None=unavailable."""
    msg = _format_approval_msg(cmd_str, org, repo)
    rc = _run_gui(
        [
            "xmessage",
            "-center",
            "-xrm",
            "*international:true",
            "-xrm",
            "*form.message.Scroll:WhenNeeded",
            "-xrm",
            "*form.minimumWidth:500",
            "-buttons",
            "Allow:0,Deny:1",
            "-default",
            "Deny",
            msg,
        ],
        timeout=timeout,
        cancel=cancel,
    )
    if rc is None:
        return None
    return rc == 0


def _ask_zenity(
    cmd_str: str,
    org: str,
    repo: str | None = None,
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> bool | None:
    """Returns True=approved, False=denied, None=unavailable."""
    msg = _format_approval_msg(cmd_str, org, repo)
    rc = _run_gui(
        [
            "zenity",
            "--question",
            "--title=GitHub Elevated Access (ghsudo)",
            f"--text={msg}",
            "--width=500",
            "--ok-label=Allow",
            "--cancel-label=Deny",
        ],
        timeout=timeout,
        cancel=cancel,
    )
    if rc is None:
        return None  # not installed or timed out
    return rc == 0


def _ask_kdialog(
    cmd_str: str,
    org: str,
    repo: str | None = None,
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> bool | None:
    """Returns True=approved, False=denied, None=unavailable."""
    msg = _format_approval_msg(cmd_str, org, repo)
    rc = _run_gui(
        [
            "kdialog",
            "--title",
            "GitHub Elevated Access (ghsudo)",
            "--yesno",
            msg,
            "--yes-label",
            "Allow",
            "--no-label",
            "Deny",
        ],
        timeout=timeout,
        cancel=cancel,
    )
    if rc is None:
        return None
    return rc == 0


def _ask_osascript(
    cmd_str: str,
    org: str,
    repo: str | None = None,
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> bool | None:
    """Returns True=approved, False=denied, None=unavailable."""
    escaped = _escape_for_applescript(
        _format_approval_msg(cmd_str, org, repo).replace("\n", "\\n")
    )
    # "cancel button" makes Deny return exit code 1.
    script = (
        f'display dialog "{escaped}" '
        f'buttons {{"Deny", "Allow"}} cancel button "Deny" '
        f'default button "Deny" '
        f'with title "GitHub Elevated Access (ghsudo)" with icon caution'
    )
    rc = _run_gui(["osascript", "-e", script], timeout=timeout, cancel=cancel)
    if rc is None:
        return None
    return rc == 0


def _ask_powershell(
    cmd_str: str,
    org: str,
    repo: str | None = None,
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> bool | None:
    """Returns True=approved, False=denied, None=unavailable."""
    escaped = _escape_for_powershell(cmd_str)
    raw_target = f"Repository: {repo}" if repo else f"Organization: {org}"
    target = _escape_for_powershell(raw_target)
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$r = [System.Windows.Forms.MessageBox]::Show("
        f'"A GitHub command requires elevated (write) permissions.'
        f"`n`n{target}"
        f"`nCommand to execute:`n  {escaped}`n`n"
        f'Allow this command to run with elevated GitHub permissions?",'
        f'"GitHub Elevated Access (ghsudo)",'
        "[System.Windows.Forms.MessageBoxButtons]::YesNo,"
        "[System.Windows.Forms.MessageBoxIcon]::Warning); "
        'if ($r -eq "Yes") { exit 0 } else { exit 1 }'
    )
    rc = _run_gui(["powershell", "-Command", ps], timeout=timeout, cancel=cancel)
    if rc is None:
        return None
    return rc == 0


def _has_display() -> bool:
    """Check if a graphical display is available."""
    system = platform.system()
    if system == "Darwin":
        return True  # macOS always has a window server when logged in
    if system == "Windows":
        return True
    # Linux/BSD: check DISPLAY or WAYLAND_DISPLAY
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _ask_gui(
    cmd_str: str,
    org: str,
    repo: str | None = None,
    *,
    timeout: int = _GUI_TIMEOUT,
    cancel: _GuiCancel | None = None,
) -> bool | None:
    """Ask via the platform's dialog. None means no toolkit could show it."""
    system = platform.system()
    if system == "Linux":
        # Try lightest first: xmessage → zenity → kdialog
        for ask in (_ask_xmessage, _ask_zenity, _ask_kdialog):
            if cancel is not None and cancel.is_set():
                return None
            result = ask(cmd_str, org, repo, timeout=timeout, cancel=cancel)
            if result is not None:
                return result
        return None
    if system == "Darwin":
        return _ask_osascript(cmd_str, org, repo, timeout=timeout, cancel=cancel)
    if system == "Windows":
        return _ask_powershell(cmd_str, org, repo, timeout=timeout, cancel=cancel)
    return None


def _ask_approval(cmd_str: str, org: str, *, repo: str | None = None) -> bool:
    """Ask the user to approve the command. Returns True if approved."""
    system = platform.system()
    has_display = _has_display()
    cfg = _load_ntfy_config()
    remote = cfg is not None and cfg.mode == _MODE_REMOTE_APPROVE
    _debug(
        f"approval: system={system}, has_display={has_display}, "
        f"ntfy={cfg.mode if cfg else None}"
    )

    if cfg is not None and not remote:
        _notify_async(cfg, cmd_str, org, repo)

    # While the phone can still answer, the dialog must stay up just as long.
    timeout = max(_GUI_TIMEOUT, cfg.timeout) if remote and cfg else _GUI_TIMEOUT
    cancel = _GuiCancel()
    channels: list[_Channel] = []
    if has_display:
        channels.append(
            (
                "gui",
                lambda: _ask_gui(cmd_str, org, repo, timeout=timeout, cancel=cancel),
                cancel.set,
            )
        )
    if remote and cfg is not None:
        ntfy = _NtfyChannel(cfg, cmd_str, org, repo)
        channels.append(("ntfy", ntfy.run, ntfy.cancel))

    if len(channels) == 1:
        # Nothing to race — keep the single channel on the main thread.
        answer = channels[0][1]()
    else:
        answer = _race_approval(channels, timeout=timeout + _RACE_SLACK)
    if answer is not None:
        return answer

    # Cannot get user approval — no channel could reach the user.
    if not has_display:
        _err("Cannot request approval: no graphical display available.")
        _err("Ensure DISPLAY or WAYLAND_DISPLAY is set (e.g. ssh -X).")
        if remote:
            _err("The ntfy approval request could not be delivered either.")
        else:
            _err(
                "Or approve from your phone:  ghsudo --setup-ntfy --mode remote-approve"
            )
    else:
        _err("Cannot request approval: no supported GUI dialog tool found.")
        if system == "Linux":
            _err("Install one of: xmessage, zenity, kdialog.")
        _err("Display is available but no toolkit could show the dialog.")
    sys.exit(EXIT_NO_INTERACTIVE)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def _validate_token(token: str) -> dict | None:
    """Validate a GitHub token by calling /user. Returns user info or None."""
    import json  # noqa: PLC0415
    from urllib.error import URLError  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    req = Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())
            return {"login": data.get("login", "unknown")}
    except (URLError, json.JSONDecodeError, KeyError):
        return None


def _get_token_scopes(token: str) -> str | None:
    """Get the OAuth scopes for a token."""
    from urllib.error import URLError  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    req = Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.headers.get("X-OAuth-Scopes", "")
    except URLError:
        return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_setup(org: str) -> int:
    """Store an encrypted GitHub PAT for a specific org."""
    org = _validate_org_name(org)
    path = _token_path(org)

    _info("GitHub Elevated Access — Token Setup")
    _info("")
    _info(f"Organization: {org}")
    _info("This will store an encrypted GitHub Personal Access Token")
    _info("for use when Claude needs write permissions.")
    _info("")
    _info("The token will be encrypted with a key derived from this")
    _info(f"machine's characteristics and stored in {path}")
    _info("")

    if path.exists():
        _info(f"A token for '{org}' is already stored.")
        if not _confirm("Overwrite?"):
            _info("Aborted.")
            return EXIT_ERROR

    try:
        token = getpass.getpass(f"{_PREFIX} Paste your GitHub PAT (input hidden): ")
    except (EOFError, KeyboardInterrupt):
        _info("\nAborted.")
        return EXIT_ERROR

    if not token.strip():
        _err("Empty token. Aborted.")
        return EXIT_ERROR
    token = token.strip()

    _info("Verifying token...")
    user_info = _validate_token(token)
    if not user_info:
        _err("Token validation failed. Check that the token is valid.")
        return EXIT_ERROR

    scopes = _get_token_scopes(token) or "unknown"
    _info(f"OK (user: {user_info['login']}, scopes: {scopes})")

    _save_token(org, token)
    _info(f"Token for '{org}' encrypted and saved.")
    _info(_MACHINE_KEY_NOTE)
    return EXIT_OK


def _confirm(question: str) -> bool:
    """Ask a yes/no question. Anything but an explicit yes means no."""
    try:
        return input(f"{_PREFIX} {question} (yes/no): ").strip().lower() in ("yes", "y")
    except (EOFError, KeyboardInterrupt, OSError):
        return False


def _prompt(label: str, default: str) -> str | None:
    """Read one line, returning *default* on empty input or None if aborted.

    Without a terminal there is nobody to ask, so *default* is used as-is —
    that keeps `--setup-ntfy` fully scriptable from its flags.
    """
    if not sys.stdin.isatty():
        _debug(f"prompt: stdin is not a terminal, using default for {label!r}")
        return default
    try:
        answer = input(f"{_PREFIX} {label} [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return answer or default


def _parse_ntfy_flags(args: list[str]) -> dict[str, str] | None:
    """Parse --mode/--server/--topic for --setup-ntfy. None on a bad option."""
    known = ("--mode", "--server", "--topic")
    opts: dict[str, str] = {}
    i = 0
    while i < len(args):
        name, sep, inline = args[i].partition("=")
        if name not in known:
            _err(f"Unknown option for --setup-ntfy: {args[i]}")
            _err(f"Expected one of: {', '.join(known)}")
            return None
        if sep:
            value = inline
        elif i + 1 < len(args):
            value = args[i + 1]
            i += 1
        else:
            _err(f"{name} requires a value.")
            return None
        opts[name[2:]] = value
        i += 1
    return opts


def cmd_setup_ntfy(
    *,
    mode: str | None = None,
    server: str | None = None,
    topic: str | None = None,
) -> int:
    """Configure push notifications (and optionally approval) over ntfy."""
    _info("ghsudo — ntfy notification setup")
    _info("")

    if _NOTIFY_PATH.exists():
        _info("An ntfy configuration is already stored.")
        if not _confirm("Overwrite?"):
            _info("Aborted.")
            return EXIT_ERROR

    if server is None:
        server = _prompt("ntfy server", _NTFY_DEFAULT_SERVER)
    if topic is None:
        topic = _prompt("Topic", _generate_ntfy_topic())
    if mode is None:
        mode = _prompt(f"Mode ({' | '.join(_NTFY_MODES)})", _MODE_NOTIFY)
    if server is None or topic is None or mode is None:
        _info("\nAborted.")
        return EXIT_ERROR

    if mode not in _NTFY_MODES:
        _err(f"Invalid mode: {mode!r}. Expected one of: {', '.join(_NTFY_MODES)}")
        return EXIT_ERROR
    normalized_server = _normalize_server(server)
    if normalized_server is None:
        _err(f"Invalid server: {server!r}. Expected an http(s) URL.")
        return EXIT_ERROR
    topic = topic.strip()
    if not _NTFY_TOPIC_RE.match(topic):
        _err(f"Invalid topic: {topic!r}.")
        _err("Use 1-64 letters, digits, hyphens or underscores.")
        return EXIT_ERROR

    auth_token = None
    if sys.stdin.isatty():
        try:
            auth_token = getpass.getpass(
                f"{_PREFIX} Access token for self-hosted servers (blank for none): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            _info("\nAborted.")
            return EXIT_ERROR

    cfg = _NtfyConfig(
        topic=topic,
        server=normalized_server,
        mode=mode,
        auth_token=auth_token or None,
        timeout=_NTFY_DEFAULT_TIMEOUT,
    )

    _info("")
    _info(f"Sending a test notification to {cfg.server}/{cfg.topic} ...")
    if not _ntfy_publish(
        cfg,
        topic=cfg.topic,
        title="ghsudo: setup test",
        message="If you can read this, ghsudo can reach your devices.",
        tags="white_check_mark",
    ):
        _err("Test notification failed — nothing was saved.")
        _err("Check the server URL, topic and access token, then retry.")
        return EXIT_ERROR

    _save_ntfy_config(cfg)
    _info(f"Saved (mode: {cfg.mode}) to {_NOTIFY_PATH}")
    _info("")
    _info(f"Subscribe on your phone: install the ntfy app, add {cfg.server},")
    _info(f"and subscribe to the topic '{cfg.topic}'.")
    if cfg.mode == _MODE_REMOTE_APPROVE:
        _info("")
        _info(f"Approval requests wait up to {cfg.timeout}s for an Allow/Deny tap.")
        _info("Anyone who can publish to your topic can answer for you — prefer a")
        _info("self-hosted ntfy server with topic ACLs over the public instance.")
    _info(_MACHINE_KEY_NOTE)
    return EXIT_OK


def cmd_run(cmd: list[str], *, org: str | None = None) -> int:
    """Show approval dialog, then re-execute command with elevated token."""
    if not cmd:
        _err("No command specified.")
        _err("Usage: ghsudo <command...>")
        return EXIT_ERROR

    # Determine org
    _debug("detecting org")
    if not org:
        org = _detect_org(cmd)
    _debug(f"org={org}")
    if not org:
        orgs = _list_orgs()
        if len(orgs) == 1:
            org = orgs[0]
            _info(f"Auto-selected org: {org} (only one configured)")
        elif orgs:
            _err("Cannot determine target organization.\n")
            _err(f"Available orgs: {', '.join(orgs)}")
            _err("Use --org <name> to specify, e.g.:")
            _err(f"    ghsudo --org {orgs[0]} {shlex.join(cmd)}")
            return EXIT_ERROR
        else:
            _err("No tokens configured.\n")
            _err("To set up a token, run:")
            _err("    ghsudo --setup <org>")
            _err(f"\nSee: {_README_URL}")
            sys.exit(EXIT_NO_TOKEN)

    org = _validate_org_name(org)

    # Verify token exists before asking the user for approval
    _debug("loading token")
    token = _load_token(org)
    _debug("token loaded")

    cmd_str = shlex.join(cmd)

    # Detect full repo slug (owner/repo) for display in approval dialog
    repo_slug = _detect_repo_slug(cmd)
    _debug(f"repo_slug={repo_slug}")

    _debug("requesting approval")
    if not _ask_approval(cmd_str, org, repo=repo_slug):
        _info("Permission denied by user.")
        return EXIT_DENIED
    _debug("approved, executing command")

    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token

    result = subprocess.run(cmd, env=env)  # noqa: S603
    _debug(f"command exited with {result.returncode}")
    return result.returncode


def cmd_verify(org: str | None = None) -> int:
    """Verify stored token(s) and, when configured, the ntfy connection."""
    result = _verify_tokens(org)

    cfg = _load_ntfy_config()
    if cfg is not None and _verify_ntfy(cfg) != EXIT_OK and result == EXIT_OK:
        result = EXIT_ERROR
    return result


def _verify_tokens(org: str | None) -> int:
    """Verify stored token(s) can be decrypted and are valid."""
    if org:
        return _verify_one(_validate_org_name(org))

    # Verify all
    orgs = _list_orgs()
    if not orgs:
        _err("No tokens stored.")
        _err("Run:  ghsudo --setup <org>")
        return EXIT_NO_TOKEN

    failures = 0
    for o in orgs:
        _info(f"--- {o} ---")
        if _verify_one(o) != EXIT_OK:
            failures += 1

    if failures:
        _err(f"\n{failures}/{len(orgs)} token(s) failed verification.")
        return EXIT_ERROR

    _info(f"\nAll {len(orgs)} token(s) verified OK.")
    return EXIT_OK


def _verify_ntfy(cfg: _NtfyConfig) -> int:
    """Publish a test notification to confirm the ntfy channel works."""
    _info(f"--- ntfy ({cfg.mode}) ---")
    _info(f"Publishing a test notification to {cfg.server}/{cfg.topic} ...")
    if not _ntfy_publish(
        cfg,
        topic=cfg.topic,
        title="ghsudo: verification",
        message="ntfy channel verified.",
        tags="white_check_mark",
    ):
        _err("ntfy test notification failed.")
        _err("Re-run:  ghsudo --setup-ntfy")
        return EXIT_ERROR
    _info("ntfy OK.")
    return EXIT_OK


def _verify_one(org: str) -> int:
    """Verify a single org's token."""
    token = _load_token(org)
    _info(f"Token for '{org}' decrypted successfully.")
    _info("Validating against GitHub API...")

    user_info = _validate_token(token)
    if not user_info:
        _err(f"Token for '{org}' rejected by GitHub. It may be expired.")
        _err(f"Re-run:  ghsudo --setup {org}")
        return EXIT_ERROR

    scopes = _get_token_scopes(token) or "unknown"
    _info(f"OK (user: {user_info['login']}, scopes: {scopes})")
    return EXIT_OK


def cmd_revoke(org: str | None = None) -> int:
    """Delete stored encrypted token(s)."""

    if org:
        return _revoke_one(_validate_org_name(org))

    # Revoke all
    orgs = _list_orgs()
    if not orgs:
        _info("No tokens stored. Nothing to revoke.")
        return EXIT_OK

    _info(f"This will revoke tokens for: {', '.join(orgs)}")
    if sys.stdin.isatty():
        try:
            answer = input(f"{_PREFIX} Revoke all? (yes/no): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return EXIT_ERROR
        if answer not in ("yes", "y"):
            _info("Aborted.")
            return EXIT_ERROR

    for o in orgs:
        _revoke_one(o)

    return EXIT_OK


def _revoke_one(org: str) -> int:
    """Delete a single org's token."""
    path = _token_path(org)
    if not path.exists():
        _info(f"No token found for '{org}'. Nothing to revoke.")
        return EXIT_OK

    path.unlink()
    _info(f"Token for '{org}' deleted.")

    # Clean up empty dirs
    try:
        _TOKENS_DIR.rmdir()
    except OSError:
        pass
    try:
        _CONFIG_DIR.rmdir()
    except OSError:
        pass

    return EXIT_OK


def cmd_list() -> int:
    """List organizations with stored tokens, and the ntfy channel if set up."""
    orgs = _list_orgs()
    if orgs:
        _info(f"Stored tokens ({len(orgs)}):")
        for org in orgs:
            _info(f"  {org}")
    else:
        _info("No tokens stored.")
        _info("Run:  ghsudo --setup <org>")

    cfg = _load_ntfy_config()
    if cfg is not None:
        _info(f"ntfy: configured ({cfg.mode}, topic '{cfg.topic}' on {cfg.server})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_USAGE = """\
usage: ghsudo [options] <command...>
       ghsudo --setup <org>
       ghsudo --setup-ntfy [--mode MODE] [--server URL] [--topic NAME]
       ghsudo --list | --verify [org] | --revoke [org]

GitHub Sudo — re-execute commands with per-org elevated tokens.

Anything not prefixed with -- is the command to run:
  ghsudo gh pr merge 123
  ghsudo --org dashpay gh pr list

Options:
  --org ORG       Target org (auto-detected from -R flag or git remote)
  --setup ORG     Store encrypted GitHub PAT for an org
  --setup-ntfy    Configure ntfy push notifications
      --mode MODE     notify (heads-up only, default) or remote-approve
                      (approve by tapping Allow/Deny on the push)
      --server URL    ntfy server (default: https://ntfy.sh)
      --topic NAME    Topic to publish to (default: randomly generated)
  --verify [ORG]  Verify stored token(s) and the ntfy connection
  --revoke [ORG]  Revoke stored token(s)
  --list          List orgs with stored tokens
  -h, --help      Show this help
"""


def main() -> int:
    argv = sys.argv[1:]

    if not argv or "-h" in argv or "--help" in argv:
        print(_USAGE, file=sys.stderr)
        return EXIT_OK if ("-h" in argv or "--help" in argv) else EXIT_ERROR

    # Parse -- flags, collect the rest as the command
    org: str | None = None
    cmd: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--setup":
            if i + 1 >= len(argv):
                _err("--setup requires an org name.")
                return EXIT_ERROR
            return cmd_setup(argv[i + 1])
        elif arg == "--setup-ntfy":
            opts = _parse_ntfy_flags(argv[i + 1 :])
            if opts is None:
                return EXIT_ERROR
            return cmd_setup_ntfy(**opts)
        elif arg == "--list":
            return cmd_list()
        elif arg == "--verify":
            verify_org = (
                argv[i + 1]
                if i + 1 < len(argv) and not argv[i + 1].startswith("--")
                else None
            )
            return cmd_verify(verify_org)
        elif arg == "--revoke":
            revoke_org = (
                argv[i + 1]
                if i + 1 < len(argv) and not argv[i + 1].startswith("--")
                else None
            )
            return cmd_revoke(revoke_org)
        elif arg == "--org":
            if i + 1 >= len(argv):
                _err("--org requires an org name.")
                return EXIT_ERROR
            org = argv[i + 1]
            i += 2
            continue
        else:
            # Everything from here on is the command
            cmd = argv[i:]
            break
        i += 1

    return cmd_run(cmd, org=org)


if __name__ == "__main__":
    sys.exit(main())
