# Design: ntfy.sh notification channel

Status: IMPLEMENTED in v0.3.0 — see "As implemented" at the end.

## Goal

Let a user approve/deny `ghsudo` elevated-command requests via [ntfy.sh](https://ntfy.sh)
(push to phone/desktop), instead of or in addition to the existing local GUI
dialog (`xmessage`/`zenity`/`kdialog`/`osascript`/PowerShell).

**Primary motivating scenario**: `ghsudo` running on a headless/remote Linux
box (SSH'd into from a Windows client with no X forwarding configured) has
no local display at all — GUI dialogs are never reachable there. Mobile push
is the only viable approval surface in that case, so remote-approve is a
first-class mode, not a nice-to-have.

Two capabilities, with different security properties:

1. **Notify** — ping the user that an approval is pending (heads-up only,
   safe, no change to trust boundary).
2. **Remote-approve** — let the user tap Allow/Deny on a push notification
   and have that count as the approval (weaker guarantee than the GUI
   dialog — see Threat model below; **shipping regardless, per explicit
   user decision** — see Decision below).

## Why this needs care

`README`/`CLAUDE.md` states the existing design constraint explicitly:

> GUI-only approval is intentional — terminal prompts are trivially
> auto-approvable by AI agents. A graphical display is required.

The GUI dialog's security property isn't really "it's graphical" — it's that
**the approval action happens on a channel the AI agent invoking `ghsudo`
does not control**: the agent has shell access, but (assumed) not the
ability to synthesize clicks on the user's display.

ntfy is just an HTTP pub/sub topic. Anyone who knows the topic name can
publish to it and subscribe to it — there's no per-device identity. The
agent already runs as the same OS user as `ghsudo`, so it can read whatever
config file tells `ghsudo` which topic to use. That breaks the "channel the
agent doesn't control" property unless we're careful about *what* is
knowable from local state alone.

### Threat model

- **In scope**: the coding agent (Claude, etc.) invoking `ghsudo` as a
  subprocess, with full shell/file access as the local user, attempting to
  get its own command auto-approved without genuine human action.
- **Not in scope**: a fully compromised machine (attacker with root, or
  attacker who can also read the phone's screen) — no local design fixes
  that.

**Key finding**: if the *reply* topic (the one whose message counts as
"approved") is derivable from anything the agent can read on disk — the
configured ntfy topic name, in particular — the agent can subscribe to that
same topic itself and race the human, or just publish `allow` first. ntfy
has no way to show an action button to one subscriber and hide it from
another; every subscriber of a topic sees the same message, including the
reply-topic URL embedded in an `Actions` header.

Generating the reply topic freshly per-request in memory (never written to
disk) does **not** fix this on its own: that fresh topic still gets embedded
in the message body/headers published to the long-lived, config-file-known
topic — and the agent can subscribe to *that* topic and read it out.

There is no way to fully close this gap with plain ntfy topics between two
processes running as the same OS user — the topic name is the only secret,
and both `ghsudo` and the agent invoking it can read the same config. This
is a real, structural difference from the GUI dialog, not an implementation
bug to fix later.

**Important scoping note**: this is not actually a new hole. The exact same
reasoning already applies to the existing GitHub write-token storage — an
agent with local shell access could in principle reproduce
`_derive_machine_key()` (public algorithm, hardcoded salt, inputs are all
locally-readable machine identifiers) and decrypt `tokens/<org>.enc`
directly, obtaining the write token without ever going through `ghsudo`'s
approval flow at all. Decoding the token is a strict superset of what
remote-approve exposes — it grants every op, not just the one pending
approval. So ntfy's remote-approve doesn't lower the bar `ghsudo` already
implicitly accepts; it's consistent with it.

### Decision (2026-08-20)

Discussed with the user directly:

- Remote-approve is a **hard requirement** — the headless/no-X-forwarding
  scenario above has no other viable approval path.
- The residual risk (a same-OS-user agent that deliberately reproduces the
  local crypto to self-approve, rather than just naturally answering a
  prompt) is **accepted**, on the same terms as the pre-existing GH-token
  storage: `ghsudo` defends against an agent naively/accidentally
  auto-answering an approval channel it naturally has access to (e.g. a
  terminal y/n prompt), not against an agent that deliberately writes and
  runs custom code to reverse-engineer local secrets. If an agent starts
  doing the latter, that's a different, worse problem than notifications —
  "we will worry about it then."
- Ship remote-approve as a normal, documented mode — no scare-gating beyond
  the same honest limitation note the GH-token section already carries.

### Mitigations / design response (still worth doing, defense-in-depth)

1. **Notify mode is the default and is always safe** — it doesn't touch the
   trust boundary at all. The GUI dialog stays authoritative when a display
   is present.
2. **Encrypt the ntfy config at rest**, reusing the existing token-encryption
   scheme (AES-256-GCM, `_derive_machine_key()`) — see Config below. Same
   value it provides for GH tokens: protects against disk theft, backup
   leaks, or the file ending up on the wrong machine. Not a defense against
   the co-resident agent (see Decision above) — documented as such,
   consistent with the existing GH-token docstring.
3. **Reduce the exposure window regardless**: reply topic is a fresh random
   value per request (`secrets.token_urlsafe(24)`), never persisted, single
   use, and the long-poll subscription is closed the instant a message
   arrives or the timeout expires — so a passive on-disk secret dump doesn't
   let anyone approve *future* requests, only requires racing a live one.
4. **Strongly recommend self-hosted ntfy with topic ACLs** (username/password
   or bearer token) over the public `ntfy.sh` instance for remote-approve
   mode — this doesn't touch the same-machine-same-user problem, but it does
   stop a *different* threat (randoms on the public internet guessing the
   topic name), worth having independently.
5. Note (future, not in this pass): a signed-challenge scheme (phone holds a
   private key, like Duo/Okta push) would be the only way to actually close
   the co-resident-agent gap rather than just documenting it as accepted
   risk. Out of scope for v1 — call it out in the README as a known
   limitation alongside the GH-token one.

## Config

New file, `~/.config/ghsudo/notify.enc` — same at-rest treatment as the GH
tokens: plaintext is a small JSON/TOML blob, encrypted with
`_encrypt_token()`/`_derive_machine_key()` (AES-256-GCM, machine-bound key),
written with `chmod 0600`, decrypted with `_decrypt_token()` at load time.
Reuses the existing version byte + nonce framing so it can share the helper
functions with token storage (rename `_encrypt_token`/`_decrypt_token` to
`_encrypt_blob`/`_decrypt_blob` internally if that reads better once both
call sites exist).

Plaintext shape once decrypted:

```toml
[ntfy]
enabled = true
mode = "notify"        # "notify" | "remote-approve"
server = "https://ntfy.sh"
topic = "ghsudo-a1b2c3d4e5f6"   # user-chosen or auto-generated on setup
# auth_token = "..."   # optional, for self-hosted access-controlled topics
timeout = 300           # seconds; independent of _GUI_TIMEOUT (phone replies are slower)
```

Env var overrides for CI/ephemeral use, mirroring existing `GHSUDO_DEBUG`
convention: `GHSUDO_NTFY_TOPIC`, `GHSUDO_NTFY_SERVER`, `GHSUDO_NTFY_MODE`.
(Env overrides necessarily bypass the encryption-at-rest benefit — same
trade-off as any env-var secret; documented, not a regression.)

New subcommand: `ghsudo --setup-ntfy` — prompts for server/topic (or
generates a random topic), sends a test notification, and prints the
same-terms limitation note from the Decision section above (once, not a
scare dialog — parallel to how `--setup` doesn't lecture about the
GH-token's own machine-key-reproducibility limitation today, so consider
adding a one-line mention there too for consistency).

## Runtime flow

### Notify mode

In `_ask_approval`, after resolving GUI availability as today, if ntfy is
configured, fire-and-forget a `PUT`/`POST` to the topic with title,
command, org/repo, and **no action buttons** — informational only. Doesn't
block or change the return value. Runs on a best-effort basis (network
failure is logged at `_debug` level and ignored — the GUI/terminal fallback
already handles the "can't reach the user" case today).

### Remote-approve mode

Two channels can now produce a decisive answer: local GUI and ntfy. Race
them:

```
_ask_approval(cmd_str, org, repo):
    channels = []
    if _has_display(): channels.append(_ask_gui_thread)
    if ntfy_configured and mode == "remote-approve": channels.append(_ask_ntfy_thread)
    if not channels: <existing "cannot request approval" error>

    result = first_decisive_result(channels, overall_timeout=max(GUI_TIMEOUT, ntfy.timeout))
    # cancel/kill the losing channel (kill GUI subprocess / close ntfy connection)
    return result
```

- `_ask_ntfy` publishes the message with two `http` actions pointing at a
  freshly generated reply topic (`body=allow` / `body=deny`), then opens a
  streaming `GET {server}/{reply_topic}/json` connection and blocks reading
  lines until a `message` event with body `allow`/`deny` arrives, ignoring
  `open`/`keepalive` events, up to `timeout` seconds.
- Implemented with `urllib.request` (already a dependency for the GitHub API
  calls — no new third-party package) plus the `threading` stdlib module for
  the race; no need for `cryptography` or async frameworks.
- On timeout: same policy as the existing GUI timeout — treat as denial, not
  as "channel unavailable" (`_info` logs it, matching `_run_gui`'s existing
  wording).
- Both mechanisms already write to stderr/`_info`/`_debug`, so parity is
  simple: log `"ntfy: sent, waiting for reply (timeout Ns)"` etc.

### "Instead of" case (headless / no display)

If `_has_display()` is false and ntfy remote-approve is configured, skip
straight to it — no change needed beyond the loop above naturally doing the
right thing (empty GUI channel list when no display).

## CLI/UX surface

- `ghsudo --setup-ntfy [--mode notify|remote-approve] [--server URL] [--topic NAME]`
- `ghsudo --verify` extended to also test the ntfy connection if configured
  (publish a test ping, confirm 2xx).
- `ghsudo --list` unaffected (still lists GitHub org tokens); maybe add a
  one-line "ntfy: configured (remote-approve)" footer.
- No change to `cmd_run`'s public behavior/exit codes — `_ask_approval`'s
  contract (`bool`) is unchanged, only its internals gain a second channel.

## Testing

- Unit test `_ask_ntfy` against a stubbed `urlopen`/socket (no real network
  in CI) covering: approve, deny, timeout, malformed JSON lines,
  keepalive-only stream.
- Unit test the race logic with two fake channels (fast-approve vs
  slow-timeout, slow-deny vs fast-approve, both-unavailable).
- Existing GUI tests are unaffected; add a regression test that notify-mode
  never blocks `cmd_run` (mock network failure, confirm command still runs
  after GUI approval, no exception bubbles up from the notify call).

## Rollout

Ship both modes together — no reason to stage them now that remote-approve
is a settled decision rather than an open question:

1. `ghsudo --setup-ntfy` (defaults to `mode = "notify"`, `--mode
   remote-approve` to opt into approval-by-push).
2. README Security section gets a new subsection alongside the existing
   GUI-only note, describing the accepted risk in the same tone as the
   Decision above (factual, not alarmist — this is a documented trade-off,
   not a bug).
3. Bump `pyproject.toml` version and add a CHANGELOG/README entry per repo
   convention (docs stay in sync with `--help` text, mirroring existing
   `_USAGE`).

## Decisions (2026-08-20, wizard follow-up)

1. **Server**: allow both public `ntfy.sh` and self-hosted for remote-approve
   — no code-level restriction. Docs recommend self-hosted with topic ACLs
   for the security-conscious, but public `ntfy.sh` works out of the box.
2. **Timeout**: default remote-approve timeout is **300 seconds** (vs. the
   GUI dialog's 60s `_GUI_TIMEOUT`) — generous window for push delivery +
   noticing + tapping. Update the `notify.toml` example and `_ask_ntfy`
   default accordingly.
3. **GH-token docs**: add the same one-line machine-key-reproducibility note
   to `ghsudo --setup`'s output / the README GH-token section, not just the
   new ntfy section — this is a pre-existing property, now documented
   consistently in both places.

## As implemented (v0.3.0)

Deltas from the sketch above, all in the same spirit:

1. **Config plaintext is JSON, not TOML** — `tomllib` is read-only, so writing
   TOML would need a third-party writer for no benefit. File location,
   encryption and permissions are as designed (`~/.config/ghsudo/notify.enc`,
   AES-256-GCM via the shared `_encrypt_blob`/`_decrypt_blob`, `0600`).
2. **Env overrides can never select remote-approve.** If any of
   `GHSUDO_NTFY_SERVER` / `GHSUDO_NTFY_TOPIC` / `GHSUDO_NTFY_MODE` is set, the
   effective mode is forced to `notify`. The agent invoking `ghsudo` controls
   its child environment, so an env-selectable approval channel would let it
   point `ghsudo` at a topic it owns and approve itself — cheaper than the
   crypto-reproduction attack the Decision section accepts, so it is closed.
3. **Subscribe before publish.** The reply-topic stream is opened *before* the
   notification goes out, so a reply tapped the instant the push lands cannot
   arrive before anyone is listening.
4. **Publishing uses ntfy's JSON API** rather than headers: command strings can
   contain newlines and non-ASCII, which HTTP headers cannot carry.
5. **When both channels race, the GUI dialog's timeout is raised** to the ntfy
   timeout, so the 60s dialog deadline cannot auto-deny while the phone can
   still answer.
6. Channel contract, symmetric for both channels: `None` = could not reach the
   user, `False` = denied *including its own timeout*, `True` = approved. With
   no channel able to answer, `EXIT_NO_INTERACTIVE` is preserved unchanged.
