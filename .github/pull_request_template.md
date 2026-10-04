## What

<!-- One or two sentences. Which tool / module, and what changes for the user. -->

## Why

<!-- Problem or need. Link the README Roadmap item if there is one. -->

## Checklist

- [ ] `pytest -q` and `ruff check ica_mcp tests` pass
- [ ] Tests added or updated (offline, `FakeSession`, no real credentials)
- [ ] Tool annotations correct (`READ` / `WRITE` / `DESTRUCTIVE`); destructive paths use exact list names
- [ ] Tool docstrings and replies in Swedish; nothing new prints to stdout in `server.py` / `client.py`
- [ ] README tool table / docs updated if the tool surface, flags, env vars or setup changed
- [ ] Live-checked against a throwaway list if anything writes to ICA (say what you observed below)
- [ ] New API facts recorded in the module docstring with a date

## Live check

<!-- "Not needed (no write path)" or what you saw, one line per behaviour. -->
