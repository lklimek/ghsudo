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
- Exit code 3 (no GUI display, no supported dialog tool, or the dialog lost its display): inform the user approval is not possible.
- Exit code 4 (not set up): tell the user to follow <https://github.com/lklimek/ghsudo#readme>.
- Don't judge the exit code through a pipe: `ghsudo … | tail` reports `tail`'s status. Run it unpiped, or check `${PIPESTATUS[0]}`.

# ghsudo — Developer Guide

## Project structure

- `src/ghsudo/__main__.py` — CLI entry point and all logic (single-file tool)
- `src/ghsudo/__init__.py` — package init, reads version from package metadata
- `pyproject.toml` — build config and **single source of truth for version**
- `tests/` — pytest tests

## Version management

Version is defined **only** in `pyproject.toml`. The `__init__.py` reads it at
runtime via `importlib.metadata.version("ghsudo")`.

When bumping the version, edit **only** `pyproject.toml`.

## Testing

The package is not installed in the dev environment, so point Python at `src/`
(or run `pip install -e .` first, as CI does):

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Only run formatting/linting right before committing (`ruff` is a standalone
binary, not a Python module):

```bash
ruff check src/ tests/ && ruff format src/ tests/
```

## Release process

1. **Bump version** in `pyproject.toml` (semver: major.minor.patch)
2. **Commit**: `git commit -m "build: bump version to X.Y.Z"`
3. **Push**: `git push`
4. **Create GitHub release** (triggers PyPI publish via CI):
   ```bash
   ghsudo gh release create vX.Y.Z --generate-notes
   ```
5. **Verify** the publish workflow completes on GitHub Actions.

The `publish.yml` workflow builds and publishes to PyPI automatically when a
GitHub release is created.

## Platform support

Only Linux is actively tested. macOS/Windows have basic support but are untested.

## Security design

GUI-only approval is intentional — terminal prompts are trivially auto-approvable
by AI agents. A graphical display (`DISPLAY`/`WAYLAND_DISPLAY`) is required.
