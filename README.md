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

## Installation

```bash
pip install ghsudo
```

Or install from source:

```bash
git clone https://github.com/lklimek/ghsudo
cd ghsudo
pip install .
```

> **Note:** For `git push`/`pull` to work with `ghsudo`'s elevated token, your remotes need `https://` URLs (not SSH). `ghsudo` injects `GH_TOKEN`/`GITHUB_TOKEN` which the `gh` credential helper uses for HTTPS Git operations. (`ghsudo gh ...` commands work regardless of remote URL scheme.)
> To configure `gh` as the Git credential helper, run:
> ```bash
> gh auth setup-git
> ```

**Requirement:** Python 3.10+

> **Note:** Only **Linux** is actively tested. macOS and Windows have basic support (GUI dialogs, path handling) but are **not tested** — contributions welcome.

## Quick Start

```bash
# 1. Install
pip install ghsudo

# 2. Create a write-access GitHub PAT at https://github.com/settings/tokens
#    (classic PAT with 'repo' scope, or fine-grained with the permissions you need)

# 3. Store the write token — <org> is the GitHub organization or user account
#    that owns the repo (e.g. 'mycompany' for mycompany/myapp, or your username)
ghsudo --setup <org>

# 4. Give the agent a read-only token — log in with a separate read-only PAT
#    so the agent's gh commands are restricted by default
echo "<your-read-only-token>" | gh auth login --hostname github.com --with-token
# Alternatively, use an environment variable (session-scoped):
# export GH_TOKEN=<your-read-only-token>

# 5. Add CLAUDE.md / AGENTS.md to your repo (see below)
```

> **⚠️ Important:** Run the agent in a **dedicated terminal** (or subshell) where
> your `gh` is authenticated with the read-only token above. Do **not** launch the agent
> in a session where your real, writable `gh auth login` is active — this would give
> the agent full write access and bypass ghsudo's read-only restriction.

When the agent needs to perform a write operation, it calls:

```bash
ghsudo gh pr merge 123 --merge
ghsudo gh issue comment 42 --body "Done!"
ghsudo gh pr review 7 --approve
```

A dialog appears asking you to approve. Only after you click **Allow** does the command run.

See [Setting up with your agent](#setting-up-with-your-agent) for a detailed walk-through.

## Setting up with your agent

The key idea: give the agent a read-only token, and instruct it to use `ghsudo` for write operations. A `CLAUDE.md` / `AGENTS.md` file in the target repository carries those instructions into the agent's context automatically.

### Step-by-step

#### 1. Install ghsudo on your machine

```bash
pip install ghsudo
```

#### 2. Create a write-access GitHub PAT and store it

Go to [GitHub Settings → Developer Settings → Personal access tokens](https://github.com/settings/tokens) and generate a new token with the write scopes you need (e.g. the `repo` scope for a classic PAT, or the relevant fine-grained permissions).

Then store it with `ghsudo`:

```bash
ghsudo --setup <org>
```

`<org>` is the GitHub organization or personal user account name that owns the repositories you work with — the owner part of an `owner/repo` pair. For example, for `mycompany/myapp` use `mycompany`; for your own repos use your GitHub username.

You will be prompted to paste your [GitHub Personal Access Token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens). The token is validated and stored encrypted under `~/.config/ghsudo/tokens/<org>.enc`.

#### 3. Give the agent a read-only token

Create a separate PAT with **only read scopes** (fine-grained PAT with read-only permissions, or classic PAT with only `read:org` / `public_repo`), then configure it for the agent.

**Recommended — `gh auth login` (persistent across all gh commands in the session):**

```bash
echo "<your-read-only-token>" | gh auth login --hostname github.com --with-token
```

This stores the token as the active GitHub credential for the `gh` CLI. All `gh` read operations in that session will use it, and write operations will fail (prompting the agent to use `ghsudo`).

**Alternative — environment variable (session-scoped):**

```bash
export GH_TOKEN=<your-read-only-token>
```

`GH_TOKEN` takes precedence over `gh auth login` credentials, so setting it achieves the same restriction for the duration of the shell session.

> **⚠️ Warning:** Do **not** launch the agent in a terminal where your real, writable
> `gh auth login` is active without setting `GH_TOKEN`. If no `GH_TOKEN` or
> `GITHUB_TOKEN` is set, the agent inherits your personal GitHub credentials (which
> may have full write access), bypassing ghsudo's read-only restriction. Use a dedicated
> terminal or subshell for the agent session.

#### 4. Add agent instructions to your repository

Copy the provided template files into the root of each repository where the agent will work:

**For Claude Code** — copy [`CLAUDE-example.md`](CLAUDE-example.md) as `CLAUDE.md` in your repo (or append its contents to an existing `CLAUDE.md`):

```markdown
# GitHub access — two-token model

You have been given a **read-only** GitHub token via `GH_TOKEN` / `GITHUB_TOKEN`.
Use it freely for all read operations.

For any GitHub operation that **modifies** state (merge, push, comment, label,
close, approve, request review, etc.), prefix the command with `ghsudo`:

    ghsudo gh pr merge 123 --merge
    ghsudo gh issue comment 42 --body "Done!"
    ghsudo gh pr review 7 --approve

`ghsudo` will show the user a dialog and wait for explicit approval before running
the command with elevated permissions. If denied (exit code 2), stop and report
to the user. Never bypass ghsudo or ask the user for the write token directly.
```

**For OpenAI Codex** — copy [`AGENTS-example.md`](AGENTS-example.md) as `AGENTS.md` in your repo (the file name `AGENTS.md` is the convention Codex uses).

The [`CLAUDE-example.md`](CLAUDE-example.md) and [`AGENTS-example.md`](AGENTS-example.md) files in *this* repository serve as ready-to-copy templates.

#### 5. Verify the setup

```bash
ghsudo --verify <org>   # confirms the token decrypts and is accepted by GitHub
ghsudo --list           # shows all orgs with stored tokens
```

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

**Environment overrides** — `GHSUDO_NTFY_SERVER`, `GHSUDO_NTFY_TOPIC` and `GHSUDO_NTFY_MODE` override the stored settings, but if *any* of them is set the mode is forced to `notify`. Approval by push always requires the stored, encrypted configuration: the agent that invokes `ghsudo` controls its environment, so an env-selectable approval channel would let it point `ghsudo` at a topic it owns and approve itself.

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
