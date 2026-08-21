# ghsudo — GitHub Sudo

**Give your coding agent a read-only GitHub token, and let it ask for permission before executing write operations.**

---

## The Problem

AI coding agents (like Claude) need access to GitHub to do useful work: reading issues, pull requests, code, and CI results. But unrestricted write access is risky — an agent could accidentally (or adversarially) merge PRs, delete branches, push code, or modify repository settings without human oversight.

The naive solutions both have drawbacks:

- **No write access**: The agent can't do useful write operations at all (post comments, request reviews, merge PRs when instructed).
- **Full write access**: The agent operates with no checks and you lose visibility into what it's doing.

## The Solution

`ghsudo` implements a **two-token model**:

1. **Read-only token** — given directly to the agent via `GH_TOKEN` / `GITHUB_TOKEN`. Used for all read operations.
2. **Write token** — stored encrypted on your machine. When the agent needs to perform a write operation (a `gh` command that would otherwise fail with HTTP 403), it calls `ghsudo` instead.

`ghsudo` then:
- Shows you a **GUI popup** listing the exact command to be executed — or pushes it to your phone, see [push notifications and remote approval](#push-notifications-and-remote-approval).
- **Waits for your explicit approval** before proceeding.
- If approved, re-runs the command with the elevated write token injected into the environment.
- If denied (or timed out after 60 s), exits with a non-zero code so the agent knows it was blocked.

The write token never appears in agent context or logs — it is encrypted at rest using AES-256-GCM with a key derived from machine-specific identifiers (machine ID, hostname, username).

## Installation and agent setup

**Requirement:** Python 3.10+

Install with `pipx` (recommended), `pip`, or from source:

```bash
pipx install ghsudo
# or: pip install ghsudo
# or:
#   git clone https://github.com/lklimek/ghsudo
#   cd ghsudo
#   pip install .
```

> **Note:** For `git push`/`pull` to work with `ghsudo`'s elevated token, use `https://` remotes (not SSH), then configure `gh` as the Git credential helper:
> ```bash
> gh auth setup-git
> ```
> `ghsudo gh ...` commands work regardless of remote URL scheme.
>
> **Platform note:** Only **Linux** is actively tested. macOS and Windows have basic support but are untested.

Set up once per GitHub owner (`<org>` = the owner in `owner/repo`):

1. Create a write PAT at [GitHub token settings](https://github.com/settings/tokens) and store it:
   ```bash
   ghsudo --setup <org>
   ```
2. Configure your coding agent to use a separate read-only token:
   ```bash
   echo "<your-read-only-token>" | gh auth login --hostname github.com --with-token
   # or (session-scoped): export GH_TOKEN=<your-read-only-token>
   ```
3. Add agent instructions in each target repository:
   - Claude Code: copy [`CLAUDE-example.md`](CLAUDE-example.md) to `CLAUDE.md`
   - OpenAI Codex: copy [`AGENTS-example.md`](AGENTS-example.md) to `AGENTS.md`
4. Verify:
   ```bash
   ghsudo --verify <org>
   ghsudo --list
   ```

> **⚠️ Important:** Run the agent in a dedicated terminal/subshell where `gh` is authenticated with the read-only token. Otherwise the agent may inherit your writable `gh` credentials and bypass `ghsudo`.

For write operations, the agent must use:

```bash
ghsudo gh pr merge 123 --merge
ghsudo gh issue comment 42 --body "Done!"
ghsudo gh pr review 7 --approve
```

`ghsudo` shows a GUI approval dialog and only runs the command after you click **Allow**.

## Usage

```
usage: ghsudo [options] <command...>
       ghsudo --setup <org>
       ghsudo --setup-ntfy [--mode MODE] [--server URL] [--topic NAME]
       ghsudo --list | --verify [org] | --revoke [org]

GitHub Sudo — re-execute commands with per-org elevated tokens.

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
```

### What is an org?

In `ghsudo`, *org* refers to the GitHub organization or personal user account that owns the repositories you work with — the owner part of an `owner/repo` pair. For example, for `microsoft/vscode` the org is `microsoft`; for a personal repo like `alice/project` the org is `alice`.

Each org can have its own stored write token, allowing you to work across multiple organizations with separate credentials.

### Org auto-detection

`ghsudo` tries to determine the target org automatically:

1. From `-R` / `--repo owner/repo` in the command arguments.
2. From the `origin` remote of the current git repository.
3. If only one org has a stored token, it is used automatically.

Use `--org <name>` to override.

### GUI dialogs

On **Linux**, `ghsudo` tries (in order): `xmessage`, `zenity`, `kdialog`.  
On **macOS**, it uses `osascript` (the built-in AppleScript runner).  
On **Windows**, it uses PowerShell's `MessageBox`.

A graphical display is **required** unless you configure [remote approval over ntfy](#push-notifications-and-remote-approval) — `ghsudo` will otherwise refuse to run, because a terminal prompt can be trivially auto-approved by an AI agent, defeating the purpose. If no approval channel is available, `ghsudo` exits with code 3.

> **Tip:** If you run your agent on a remote machine via SSH, use `ssh -X` (X11 forwarding) so that `ghsudo` GUI dialogs appear on your local display — or set up `--setup-ntfy --mode remote-approve` and approve from your phone.

The dialog auto-denies after **60 seconds** of no response to prevent the agent from hanging indefinitely — except in `remote-approve` mode, where the GUI timeout is extended to match the ntfy timeout (default 300s), so the desktop dialog doesn't deny while your phone can still answer.

## Token management

| Command | Description |
|---|---|
| `ghsudo --setup <org>` | Store (or replace) the write token for an org |
| `ghsudo --setup-ntfy` | Configure [push notifications / remote approval](#push-notifications-and-remote-approval) |
| `ghsudo --list` | List all orgs with stored tokens, and the ntfy mode if configured |
| `ghsudo --verify [org]` | Decrypt and validate token(s) against the GitHub API, and test ntfy |
| `ghsudo --revoke [org]` | Delete stored token(s) |

## Push notifications and remote approval

`ghsudo` can push approval requests to your phone via [ntfy](https://ntfy.sh). There are two modes:

| Mode | What it does | Trust boundary |
|---|---|---|
| `notify` (default) | Sends a heads-up push when an approval is pending. The GUI dialog still decides. | Unchanged |
| `remote-approve` | The push carries **Allow** / **Deny** buttons; tapping one approves or denies the command. | Weakened — see below |

```bash
# Heads-up notifications only
ghsudo --setup-ntfy

# Approve from your phone (works with no display at all)
ghsudo --setup-ntfy --mode remote-approve

# Self-hosted server with a specific topic
ghsudo --setup-ntfy --mode remote-approve --server https://ntfy.example.com --topic my-topic
```

Setup sends a test notification and stores the settings encrypted at `~/.config/ghsudo/notify.enc` (same AES-256-GCM scheme as the tokens, permissions `0600`). Subscribe to the topic in the ntfy mobile app to receive requests. `ghsudo --verify` re-tests the connection, and `ghsudo --list` shows the configured mode.

**How remote-approve works:** each request publishes a notification whose Allow/Deny buttons post to a **freshly generated, single-use reply topic** that is never written to disk. `ghsudo` waits up to **300 seconds** for the reply; no reply means denial. If a display is also available, the desktop dialog and the push race each other — the first decisive answer wins and the other is dismissed.

**No environment-variable configuration.** ntfy settings come only from the stored, encrypted config (`ghsudo --setup-ntfy`) — never from the process environment. The agent invoking `ghsudo` controls its own child environment, so an env-settable channel would let it redirect notifications, or point `ghsudo` at a topic it owns and approve itself.

### Security trade-offs of remote-approve

- **Anyone who can publish to your reply topic can answer for you.** On the public `ntfy.sh` instance topics are unauthenticated; the reply topic is random and single-use, but the notification announcing it goes to your long-lived topic. **Prefer a self-hosted ntfy server with topic ACLs** if that matters to you. Both are supported.
- **A co-resident process running as your user can read the same config.** That is the same, pre-existing property as the stored GitHub token (see the note below) — remote-approve does not lower a bar `ghsudo` already accepts, but it does not raise it either.
- **If you configure an access token**, it is embedded in the notification's action buttons so your phone can post the reply. Use a token scoped to just these topics.
- **The command line and repository name are sent to the ntfy server.** Self-host if that is sensitive.
- **Not implemented:** a signed-challenge scheme (your phone holding a private key, Duo/Okta style) would be the only way to actually close the co-resident gap. That is a known future direction, not something this version does.

## Security

- Tokens and the ntfy config are encrypted with **AES-256-GCM**.
- The encryption key is derived via **PBKDF2-SHA256** (600,000 iterations) from stable machine identifiers (machine ID, hostname, username).
- Encrypted files are stored with permissions `0600`.
- The write token is **never** passed to the agent or written to logs — it is injected into the subprocess environment only after approval.

> **Note on the encryption key:** it is derived from this machine's identifiers, so any code running as your user can re-derive it and decrypt the files directly. Encryption at rest protects stolen disks and stray backups — it is not a defence against processes already running as you on this machine.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error |
| 2 | User denied the request (or no answer before the timeout) |
| 3 | No approval channel available (no display and no supported GUI dialog tool, with no ntfy remote-approve configured) |
| 4 | No token stored for the target org |

## Debugging

Set `GHSUDO_DEBUG=1` to enable verbose timing output on stderr:

```bash
GHSUDO_DEBUG=1 ghsudo gh pr list
```
