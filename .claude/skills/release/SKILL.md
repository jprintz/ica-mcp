---
name: release
description: How to cut a release of ica-mcp (version bump, tag, GitHub Release). This fork is distributed from git only, not PyPI. Use when asked to release, tag a version, bump the version, or publish release notes.
---

# Releasing ica-mcp

## Situation

- This fork is **not published to PyPI**. The `ica-mcp` package there is the
  upstream project's `0.5.0`. Users install from git:
  `uv tool install git+https://github.com/jprintz/ica-mcp` (optionally
  `@vX.Y.Z`). The PyPI release workflow was removed on purpose; do not
  re-add it or change `name` in `pyproject.toml` unless the user asks.
- `__version__` in `ica_mcp/__init__.py` is the single version source
  (hatch reads it). It was still `0.5.0` when the fork diverged.

## Steps

1. Work in its own worktree (see AGENTS.md). `main` must be green, and
   everything in the release must already be merged.
2. Pick the version: minor for new tools or behaviour, patch for fixes. Bump
   `__version__` in a `chore: release vX.Y.Z` PR into `main`.
3. Check that it builds and installs from git:
   ```bash
   .venv/bin/python -m pip install -q build && .venv/bin/python -m build
   ls dist/   # ica_mcp-X.Y.Z-*.whl and ica_mcp-X.Y.Z.tar.gz
   ```
4. After the PR is merged, tag the merge commit on `main` as `vX.Y.Z` and push
   the tag. Confirm with the user first; a tag is public.
5. Create a GitHub Release from the tag (`gh release create vX.Y.Z --notes-file …`).
   Notes in English: tools added or changed, safety rules, setup changes.
6. Update the README **Status** section if it says the code is unreleased.

## Never

- Rewrite history, force-push `main`, or delete or move tags.
- Publish to PyPI or add a publish workflow without the user's say-so.
