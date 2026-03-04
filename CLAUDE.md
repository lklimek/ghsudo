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

```bash
python -m pytest tests/ -v
```

Only run formatting/linting right before committing:

```bash
python -m ruff check src/ tests/ && python -m ruff format src/ tests/
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
