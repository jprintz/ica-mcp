---
name: release
description: How to cut a release of ica-mcp (version bump, tag, GitHub Release, the PyPI situation for this fork). Use when asked to release, publish, tag a version, bump the version, or when the user asks why the Release workflow failed.
---

# Releasing ica-mcp

## Current situation (read first)

- `__version__` in `ica_mcp/__init__.py` is still `0.5.0`, the upstream
  version. Everything since (phase 5 in the README Status table) is
  unreleased.
- The PyPI project `ica-mcp` belongs to the upstream author (`kanylbullen`).
  `release.yml` publishes with Trusted Publishing, which PyPI ties to a
  specific GitHub repo. A Release from `jprintz/ica-mcp` will therefore fail
  at the `publish` job unless one of these is done first:
  1. upstream adds `jprintz/ica-mcp` as a trusted publisher for `ica-mcp`
     (or merges this fork back), or
  2. this fork publishes under a different distribution name (`name =` in
     `pyproject.toml`, e.g. `ica-mcp-jprintz`) with its own pending publisher
     on PyPI, or
  3. the fork does not publish to PyPI at all and users install from git
     (`uv tool install git+https://github.com/jprintz/ica-mcp`, which is what
     the README already recommends). In that case either remove the `publish`
     job or keep it and accept that a Release only produces build artifacts.
  Ask the user which path they want before tagging. Do not change
  `pyproject.toml`'s `name` on your own.
- If `.github/workflows/release.yml` no longer exists, path 3 has been chosen:
  a release is then just the version bump, the tag and a GitHub Release with
  notes; skip the PyPI steps below.

## Steps

1. Make sure `main` is green and the working tree is clean. Everything in the
   release must already be merged; `release.yml` refuses a tag that is not an
   ancestor of `origin/main`.
2. Pick the version. Bump the minor for new tools or behaviour, the patch for
   fixes. Update `__version__` in `ica_mcp/__init__.py` in its own
   `chore: release vX.Y.Z` commit (PR into `main` like any other change).
3. Verify the build locally:
   ```bash
   .venv/bin/python -m pip install -q build && .venv/bin/python -m build
   ls dist/   # must contain ica_mcp-X.Y.Z-*.whl and ica_mcp-X.Y.Z.tar.gz
   ```
   `release.yml` checks that the tag matches these file names.
4. Tag the merge commit on `main` as `vX.Y.Z` and push the tag. Confirm with
   the user before pushing; a tag is public.
5. Create the GitHub Release from the tag. The body should list the user-facing
   changes (tools added/changed, safety rules, setup changes) in English.
   Publishing the Release triggers `release.yml`: tests → build → publish.
6. Watch the run (`gh run list --workflow release.yml`). If `publish` fails
   with a Trusted Publishing error, that is the PyPI situation above, not a
   code problem.
7. Afterwards: update the README **Status** section (it currently says the
   code is unreleased beyond phase 5) and remove the "install from the repo,
   not PyPI" note only if PyPI actually received the release.

## Not part of a release

- Rewriting history, force-pushing `main`, or deleting tags.
- Changing the GitHub Actions pins or the Trusted Publishing environment
  (`environment: pypi`) without the user's say-so.
