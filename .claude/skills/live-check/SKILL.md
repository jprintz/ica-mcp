---
name: live-check
description: Manual end-to-end verification of ica-mcp against the real ICA API using a throwaway shopping list. Use after changing anything that writes to ICA (add/merge/link/check/remove/store/create/delete), or when asked to "test it live", "verify against ICA", "run a live check". Never touches the user's real lists.
---

# Live check against ICA

CI cannot reach ICA (Swedish IP + login required), so every change to a write
path is verified by hand against a **throwaway list**. This skill is the
protocol. It changes the user's real account, so follow it exactly.

## Before you start

1. Confirm the user wants a live check in this session. Do not run one
   unprompted after a code change; propose it.
2. Check the session without logging in (never run `ica-mcp login` yourself):
   ```bash
   ica-mcp status
   ```
   If it says the session cannot be renewed, stop and ask the user to run
   `ica-mcp login`.
3. Decide **which code** you are testing:
   - The `ICA` MCP tools available in the client are the *installed* server
     (`uv tool`), not necessarily this checkout. Use them to test behaviour
     that is already installed, or after the user reinstalls from the checkout
     (`uv tool install --force --reinstall .` + client restart).
   - To test **uncommitted checkout code** without a reinstall, call the client
     directly from the repo venv:
     ```bash
     .venv/bin/python - <<'PY'
     from ica_mcp.client import IcaClient
     c = IcaClient()
     L = c.resolve_or_create_list("ica-mcp test 2026-10-04 1430")
     print(c.add_or_merge(L["offlineId"], [{"name": "mjölk", "quantity": 1, "unit": "l"}]))
     print([r["productName"] for r in c.get_list_raw(L["offlineId"])["rows"]])
     PY
     ```

## Protocol

1. **Create** a list named `ica-mcp test <YYYY-MM-DD HHMM>`. Every write in the
   check targets this list by its exact name. Nothing else.
2. **Exercise** the changed path with a small, deterministic set of items, e.g.
   `mjölk 1 l`, `krossade tomater 2 förp`, a free-text item like `batterier`
   with a `category`, and whatever the change is about (a recipe id, a
   barcode, a store name). Prefer one operation per step so a failure is
   attributable.
3. **Observe** with `view_shopping_list` (or `get_list_raw`) after each write:
   row names, quantity/unit, `product_id`/section, checked state, recipe
   attribution. If the user has the ICA app open, ask them to confirm what the
   app shows (sections, "Tillagd från recept", merged rows) because the app is
   the source of truth for presentation.
4. **Check the negative cases** that matter for safety: a partial list name
   on a destructive tool is refused; a bad barcode is rejected before any HTTP;
   an unknown `product_id` fails with a clear message.
5. **Clean up**: delete the test list by its exact name. List all lists and
   confirm only the expected ones remain. If a previous run left lists behind,
   delete only those whose names start with `ica-mcp test`.
6. **Record**: write what you observed in the PR description (one line per
   behaviour), and put any newly learned API fact into the relevant module
   docstring (`client.py` or `products.py`) with today's date.

## Never

- Add, check, remove or clear anything on a list that is not the test list.
- Run `delete_shopping_list` or `clear_checked` with a name you have not just
  created.
- Print, copy or paste tokens, the state file, personnummer or passwords.
- Leave the test list behind.
