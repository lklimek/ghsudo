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
