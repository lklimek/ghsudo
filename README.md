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
- Shows you a **GUI popup** listing the exact command to be executed.
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
       ghsudo --list | --verify [org] | --revoke [org]

GitHub Sudo — re-execute commands with per-org elevated tokens.

Options:
  --org ORG       Target org (auto-detected from -R flag or git remote)
  --setup ORG     Store encrypted GitHub PAT for an org
  --verify [ORG]  Verify stored token(s)
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

A graphical display is **required** — `ghsudo` will refuse to run without one, because a terminal prompt can be trivially auto-approved by an AI agent, defeating the purpose. If no GUI toolkit is found, `ghsudo` exits with code 3.

> **Tip:** If you run your agent on a remote machine via SSH, use `ssh -X` (X11 forwarding) so that `ghsudo` GUI dialogs appear on your local display.

The dialog auto-denies after **60 seconds** of no response to prevent the agent from hanging indefinitely.

## Token management

| Command | Description |
|---|---|
| `ghsudo --setup <org>` | Store (or replace) the write token for an org |
| `ghsudo --list` | List all orgs with stored tokens |
| `ghsudo --verify [org]` | Decrypt and validate token(s) against the GitHub API |
| `ghsudo --revoke [org]` | Delete stored token(s) |

## Security

- Tokens are encrypted with **AES-256-GCM**.
- The encryption key is derived via **PBKDF2-SHA256** (600,000 iterations) from stable machine identifiers (machine ID, hostname, username).
- Token files are stored with permissions `0600`.
- The write token is **never** passed to the agent or written to logs — it is injected into the subprocess environment only after approval.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error |
| 2 | User denied the request |
| 3 | No graphical display available, or no supported GUI dialog tool found |
| 4 | No token stored for the target org |

## Debugging

Set `GHSUDO_DEBUG=1` to enable verbose timing output on stderr:

```bash
GHSUDO_DEBUG=1 ghsudo gh pr list
```
