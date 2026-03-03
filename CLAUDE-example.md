# GitHub access — two-token model

You have a **read-only** `GH_TOKEN`. For write operations, use `ghsudo`:

```
ghsudo gh pr merge 123 --merge
ghsudo gh issue comment 42 --body "Done!"
```

`ghsudo` shows a dialog and runs the command with elevated permissions only after user approval.

## Rules

- **Never** bypass `ghsudo` or ask the user for the write token directly.
- Exit code 2 (denied): stop and report to the user.
- Exit code 3 (no interactive session): inform the user approval is not possible.
- Exit code 4 (not set up): tell the user to follow <https://github.com/lklimek/ghsudo#readme>.
