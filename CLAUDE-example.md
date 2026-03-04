# GitHub access — two-token model

You have a **read-only** `GH_TOKEN`. For write operations, use `ghsudo`:

```
ghsudo gh pr merge 123 --merge
ghsudo gh issue comment 42 --body "Done!"
```

`ghsudo` shows a dialog and runs the command with elevated permissions only after user approval.

## Rules

- **Always** use `https://` URLs for git remotes so that `git push`/`pull` can use the `gh` credential helper. `ghsudo` itself injects `GH_TOKEN`/`GITHUB_TOKEN` for the `gh` CLI and does not depend on the remote URL scheme. If git push/pull fails over HTTPS, remind the user to run `gh auth setup-git`.
- **Never** bypass `ghsudo` or ask the user for the write token directly.
- Exit code 2 (denied or timed out): stop and report to the user.
- Exit code 3 (no GUI display or no supported dialog tool): inform the user approval is not possible.
- Exit code 4 (not set up): tell the user to follow <https://github.com/lklimek/ghsudo#readme>.
