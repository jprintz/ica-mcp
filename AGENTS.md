# ica-mcp — working guide for agents and contributors

This file is the single source of truth for how to work in this repository.
`CLAUDE.md` imports it; other agent tools read it directly. Keep it short and
keep it true: when a rule here stops matching the code, fix one of them.

## What this is

An MCP server (Python, FastMCP) that gives AI agents access to a real ICA
account: shopping lists, recipes, store offers, bonus and the product
catalogue. ICA has no public API; the server talks to the private backend the
ICA mobile app uses. This repo (`jprintz/ica-mcp`) is a fork of
`kanylbullen/ica-mcp` and is well ahead of upstream and of the PyPI package.

The server **modifies a real account**. Treat every write path as production.

## Layout

| Path | Role |
|---|---|
| `ica_mcp/client.py` | `IcaClient`: OAuth/PKCE login against Curity, token cache (0600 state file, atomic writes, re-read before refresh), all HTTP to `apimgw-pub.ica.se`, list and row operations (`/sync`), item parsing (`to_item`, units), merging (`plan_additions`, `merge_quantities`), recipe aggregation. The only module that knows HTTP. |
| `ica_mcp/products.py` | ICA's product catalogue (~6 900 generic products): exact match, ranked search, memory + disk cache, section ids (`CATEGORY_IDS`). |
| `ica_mcp/server.py` | The MCP tools. Thin: resolve names → call the client → return a short Swedish reply. Every tool carries a `READ`, `WRITE` or `DESTRUCTIVE` annotation. |
| `ica_mcp/cli.py` | `ica-mcp serve \| login \| status \| register`. `login` is separate because a password prompt cannot run over the stdio transport. |
| `tests/` | Offline only. `tests/helpers.py` has `FakeSession`/`FakeResponse`; `conftest.py` has the `make_client` fixture (explicit state file in `tmp_path`, env cleared). |
| `.github/workflows/` | `ci.yml` (lint + tests on 3.10–3.13 + lowest `mcp`), `release.yml` (PyPI on GitHub Release, see Releases). |

Module docstrings hold facts verified against ICA, with a date (see the top of
`products.py` and `client.py`). When you learn something about the API
empirically, record it there, not in a chat or a PR comment.

## Commands

```bash
uv pip install --python .venv/bin/python -e ".[dev]"   # or: pip install -e ".[dev]"
.venv/bin/python -m pytest -q                            # ~300 tests, ~1 s, no network
.venv/bin/ruff check ica_mcp tests                       # lint gate used in CI
.venv/bin/ruff check --fix ica_mcp tests                 # safe auto-fixes (import order etc.)
```

The live API cannot be exercised in CI (Swedish IP + login). See **Live
verification** below.

## Hard rules

1. **stdout is the MCP transport.** Nothing in `server.py` or `client.py` may
   print to stdout. Log through `logging` (goes to stderr). The CLI's
   human-readable output is the only exception, and `serve` must not use it.
2. **Destructive tools never guess.** `remove_item`, `clear_checked` and
   `delete_shopping_list` take the list's exact name (or id); empty, partial or
   fuzzy matches are refused. Keep the forgiving matching for read-only and
   additive tools. A new tool that deletes anything gets `DESTRUCTIVE` and the
   same exact-name rule.
3. **Linking never guesses either.** An item is linked to a catalogue product
   only on an exact name/plural match, an explicit `product_id`, a barcode's
   `articleId`, or (recipes only, as a fallback) ICA's `ingredientId`. Search
   results are suggestions, never auto-applied.
4. **Tests stay offline.** No test may reach ICA. Mock HTTP with `FakeSession`.
   Never put a real token, personnummer or password in a test, fixture or
   example file.
5. **Secrets are off limits.** Do not read, print or copy the auth state file
   (`~/Library/Application Support/ica-mcp/`, `~/.local/state/ica-mcp/`,
   `%LOCALAPPDATA%\ica-mcp`, or `ICA_STATE_FILE`), `.env`, or `ICA_USER` /
   `ICA_PASS`. Never run `ica-mcp login`; the user does that.
6. **Live checks only on throwaway lists.** Never run write tools against the
   user's real lists (`Handla` and friends). Follow the live-check protocol in
   `.claude/skills/live-check/SKILL.md`.
7. **Scope: ICA data and actions, nothing more.** The server exposes what ICA
   has: lists, items, recipes, stores, offers, bonus, products. Planning or
   orchestration (pantry tracking, portion scaling, undo history, meal
   preferences) belongs in the agent that uses the server. Test: does the
   feature need ICA's API or ICA data the agent cannot get through existing
   tools? If the agent could compose existing tools, do not add it here.
   Offers-aware recipe picking passed this test; pantry, scaling and undo did not.

## Conventions

- **Language.** Code comments, docstrings, tool descriptions, tool parameter
  descriptions and tool replies are in **Swedish** (they match the ICA app and
  are the LLM-facing surface). README, this file, commit messages, PR titles
  and bodies are in **English**.
- **Tool docstrings are prompts.** The docstring of an `@mcp.tool` is what the
  calling LLM reads. Say what the tool does, what each argument means, what
  the default is when omitted, and what the reply contains. Keep replies
  short, name the list that was touched, and list anything the agent should
  follow up on (unsorted items with suggestions, merged rows).
- **Errors are `IcaError` with an actionable message** (what matched, what the
  valid options are). Invalid input that the agent can fix (bad barcode,
  missing argument) returns a string; API failures raise.
- **Style.** `ruff check` with `E, F, W, I, B, UP` is the gate. Lines ≤ 110.
  The formatter is not enforced; match the surrounding hand-wrapped, compact
  style rather than reformatting a file. `from __future__ import annotations`,
  type hints, `dict`/`list` builtins.
- **Compatibility.** Python 3.10+ (CI matrix 3.10–3.13). `mcp>=1.14,<2`, and CI
  tests the floor, so do not use newer FastMCP features without bumping it.
- **Pure logic goes in `client.py`/`products.py` as plain functions** so it is
  testable without a client (`to_item`, `plan_additions`, `merge_quantities`,
  `ProductCatalog.search`). `server.py` stays thin.
- **No TODOs in code.** Deferred work goes to the README Roadmap (public) or the
  owner's local notes.

## Workflow

- One branch per change: `feat/`, `fix/`, `chore/`, `test/`. Conventional
  commit messages (`feat: …`, `fix: …`). Small PRs into `main`; CI must be
  green. Keep the README tool table in sync when the tool surface changes.
- **Definition of done** for a change:
  - tests added or updated, `pytest` and `ruff check` pass locally;
  - annotations (`READ`/`WRITE`/`DESTRUCTIVE`) correct for any new or changed tool;
  - README updated if a tool, flag, env var or setup step changed;
  - anything that writes to ICA was live-checked against a throwaway list and
    the PR says so;
  - new API facts recorded in the relevant module docstring.
- **Reviews.** Use the checklist in `.claude/skills/review/SKILL.md`.
- **Releases.** See `.claude/skills/release/SKILL.md`. Short version: bump
  `__version__` in `ica_mcp/__init__.py`, merge to `main`, tag `vX.Y.Z`,
  publish a GitHub Release. Note the PyPI caveat in that skill before tagging.

## Runtime locations

| What | Where |
|---|---|
| Auth state (tokens) | `platformdirs.user_state_dir("ica-mcp")/auth_state.json`, override `ICA_STATE_FILE` |
| Product catalogue cache | `platformdirs.user_cache_dir("ica-mcp")/products.json`, override `ICA_CACHE_DIR` |
| Unattended re-login | `ICA_USER` / `ICA_PASS` (see `.env.example`), optional |

## Agent tooling in this repo

- `.claude/settings.json`: allows test, lint and read-only git/gh commands
  without prompts; denies reading the token cache and `.env`; runs `ruff` on
  every Python file an agent edits and feeds violations back.
- `.claude/skills/`: `live-check` (manual end-to-end protocol), `review`
  (repo-specific review checklist), `release` (release steps and the PyPI
  situation).
- `CLAUDE.local.md` (not committed): machine- and owner-specific notes such as
  which GitHub account to use. Create your own if you need one.
