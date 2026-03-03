# GitHub access — two-token model

You have been given a **read-only** GitHub token via `GH_TOKEN` / `GITHUB_TOKEN`.
Use it freely for all read operations (`gh pr list`, `gh issue view`, `gh api` GET calls, etc.).

## Write operations require explicit user approval

For any GitHub operation that **modifies** state (merge, push, comment, label, close, approve, request review, etc.), you must prefix the command with `ghsu`:

```
ghsu gh pr merge 123 --merge
ghsu gh issue comment 42 --body "Done!"
ghsu gh pr review 7 --approve
ghsu gh pr edit 5 --add-label bug
```

`ghsu` will:
1. Show the user a dialog with the exact command.
2. Wait for explicit **Allow** / **Deny**.
3. Re-run the command with an elevated write token if approved.
4. Return a non-zero exit code if denied — report this to the user and stop.

## Rules

- **Never** use `sudo`, `su`, or any other mechanism to bypass `ghsu`.
- **Never** ask the user to paste or provide the write token directly.
- If `ghsu` is not installed or exits with code 4 (no token stored), tell the user to follow the setup instructions at <https://github.com/lklimek/ghsu#readme>.
- If `ghsu` exits with code 2 (denied), respect the decision — do not retry automatically.
- If `ghsu` exits with code 3 (no interactive session), inform the user that approval is not possible in the current environment.
