# ghsu — GitHub Sudo

**Give your coding agent a read-only GitHub token, and let it ask for permission before executing write operations.**

---

## The Problem

AI coding agents (like Claude) need access to GitHub to do useful work: reading issues, pull requests, code, and CI results. But unrestricted write access is risky — an agent could accidentally (or adversarially) merge PRs, delete branches, push code, or modify repository settings without human oversight.

The naive solutions both have drawbacks:

- **No write access**: The agent can't do useful write operations at all (post comments, request reviews, merge PRs when instructed).
- **Full write access**: The agent operates with no checks and you lose visibility into what it's doing.

## The Solution

`ghsu` implements a **two-token model**:

1. **Read-only token** — given directly to the agent via `GH_TOKEN` / `GITHUB_TOKEN`. Used for all read operations.
2. **Write token** — stored encrypted on your machine. When the agent needs to perform a write operation (a `gh` command that would otherwise fail with HTTP 403), it calls `ghsu` instead.

`ghsu` then:
- Shows you a **GUI popup** (or terminal prompt) listing the exact command to be executed.
- **Waits for your explicit approval** before proceeding.
- If approved, re-runs the command with the elevated write token injected into the environment.
- If denied (or timed out after 60 s), exits with a non-zero code so the agent knows it was blocked.

The write token never appears in agent context or logs — it is encrypted at rest using AES-256-GCM with a key derived from machine-specific identifiers (machine ID, hostname, username).

## Installation

```bash
pip install ghsu
```

Or install from source:

```bash
git clone https://github.com/lklimek/ghsu
cd ghsu
pip install .
```

**Requirement:** Python 3.10+

## Quick Start

### 1. Store your write token

```bash
ghsu --setup <org>
```

Replace `<org>` with the GitHub organization or user whose repositories the token covers (e.g. `myorg`). You will be prompted to paste a [GitHub Personal Access Token](https://docs.github.com/en/authentication/keeping-your-account-and-data-safe/managing-your-personal-access-tokens) with the write scopes you need (e.g. `repo`).

The token is validated against the GitHub API and then stored encrypted under `~/.config/ghsu/tokens/<org>.enc`.

### 2. Configure your agent

Give the agent a **read-only** token via the environment:

```bash
export GH_TOKEN=<your-read-only-token>
```

In your agent's tool/permission configuration, allow it to call `ghsu` for write operations.

### 3. Use ghsu for write operations

When the agent needs to perform a write operation, it calls:

```bash
ghsu gh pr merge 123 --merge
ghsu gh issue comment 42 --body "Done!"
ghsu gh pr review 7 --approve
```

A dialog appears asking you to approve. Only after you click **Allow** does the command run.

## Usage

```
usage: ghsu [options] <command...>
       ghsu --setup <org>
       ghsu --list | --verify [org] | --revoke [org]

GitHub Sudo — re-execute commands with per-org elevated tokens.

Options:
  --org ORG       Target org (auto-detected from -R flag or git remote)
  --no-gui        Skip GUI dialog, use terminal prompt only
  --setup ORG     Store encrypted GitHub PAT for an org
  --verify [ORG]  Verify stored token(s)
  --revoke [ORG]  Revoke stored token(s)
  --list          List orgs with stored tokens
  -h, --help      Show this help
```

### Org auto-detection

`ghsu` tries to determine the target org automatically:

1. From `-R` / `--repo owner/repo` in the command arguments.
2. From the `origin` remote of the current git repository.
3. If only one org has a stored token, it is used automatically.

Use `--org <name>` to override.

### GUI dialogs

On **Linux**, `ghsu` tries (in order): `xmessage`, `zenity`, `kdialog`.  
On **macOS**, it uses `osascript` (the built-in AppleScript runner).  
On **Windows**, it uses PowerShell's `MessageBox`.

If no GUI is available (e.g. headless server), it falls back to a terminal prompt. Use `--no-gui` to force terminal-only mode.

The dialog auto-denies after **60 seconds** of no response to prevent the agent from hanging indefinitely.

## Token management

| Command | Description |
|---|---|
| `ghsu --setup <org>` | Store (or replace) the write token for an org |
| `ghsu --list` | List all orgs with stored tokens |
| `ghsu --verify [org]` | Decrypt and validate token(s) against the GitHub API |
| `ghsu --revoke [org]` | Delete stored token(s) |

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
| 3 | No interactive session available to ask for approval |
| 4 | No token stored for the target org |

## Debugging

Set `GHSU_DEBUG=1` to enable verbose timing output on stderr:

```bash
GHSU_DEBUG=1 ghsu gh pr list
```
