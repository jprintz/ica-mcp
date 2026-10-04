---
name: review
description: Repo-specific review checklist for ica-mcp pull requests and diffs (MCP tool annotations, destructive-tool safety, Swedish tool text, stdout discipline, offline tests, README sync). Use when asked to review a PR, a branch or the current diff in this repo; complements the generic /code-review.
---

# Reviewing a change in ica-mcp

Run the generic correctness review first (`/code-review`), then walk this
checklist. Anything that fails a **Safety** item blocks the merge.

## Safety

- [ ] Every new or changed `@mcp.tool` has the right annotation: `READ` for
      pure reads, `WRITE` for additive changes, `DESTRUCTIVE` for anything that
      deletes rows or lists. Tests in `tests/test_safety.py` assert these.
- [ ] A destructive tool resolves its list with `exact=True` (via
      `_resolve_destructive` or `resolve_list(..., exact=True)`). No partial or
      fuzzy matching on delete paths.
- [ ] Nothing new writes to stdout in `server.py` or `client.py`. Logging goes
      through `logging`. `cmd_serve` still does not call `_enable_utf8_console`.
- [ ] No token, personnummer, password or state-file contents can end up in a
      log line, an error message, a test or a fixture.
- [ ] Items are linked to products only on exact name/plural, explicit
      `product_id`, barcode `articleId`, or the recipe `ingredientId`
      fallback. Search results are never auto-applied.
- [ ] Read-modify-write on rows (`change_rows`) re-fetches the list first
      (`get_list_raw`) rather than trusting a cached copy.

## Correctness and scope

- [ ] The feature is ICA data or an ICA action. Planning/orchestration
      (pantry, portion scaling, undo, preferences) is out of scope for the
      server; say so and point to the agent side.
- [ ] Pure logic lives in `client.py`/`products.py` as testable functions;
      `server.py` only resolves names, calls, and formats the reply.
- [ ] Python 3.10 compatible (no 3.11+ syntax or stdlib). Nothing requires an
      `mcp` newer than the floor in `pyproject.toml` unless the floor is bumped
      and CI's `test-min-mcp` still passes.
- [ ] Unit handling: new units go through `normalize_unit`/`_UNIT_FACTORS`;
      merging keeps the documented rules (volume and weight convert, `st` and
      `g` do not cross).

## Tests

- [ ] New behaviour has offline tests using `FakeSession`/`make_client`. No
      network, no sleeps, no real credentials.
- [ ] Tests assert the Swedish reply text or structured result that the
      calling LLM will actually see, not just that no exception was raised.
- [ ] `pytest -q` and `ruff check ica_mcp tests` pass.

## Text and docs

- [ ] Tool docstrings and parameter descriptions are in Swedish, state the
      default when an argument is omitted, and describe the reply. They are the
      LLM's prompt; read them as such.
- [ ] Replies name the list that was touched and list what the agent should
      follow up on (unsorted items + suggestions, merged rows).
- [ ] README tool table and relevant sections updated if the tool surface,
      flags, env vars or setup changed. Commit messages and PR text in English.
- [ ] New empirically verified API facts are recorded in the module docstring
      with a date.
- [ ] Anything that writes to ICA was live-checked (see `/live-check`) and the
      PR says what was observed.

## Posting the review

Summarise blocking items first, then suggestions. On GitHub, post as a PR
comment (the repo owner authors the PRs, so approve/request-changes from the
same account is refused). Anything deferred goes to the owner's local
follow-ups file, not into the PR.
