# ica-mcp

[![CI](https://github.com/jprintz/ica-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jprintz/ica-mcp/actions/workflows/ci.yml)

An **MCP server for ICA** — Sweden's largest grocery chain — that lets AI agents
(Claude, or any [Model Context Protocol](https://modelcontextprotocol.io) client)
use your ICA account in natural language. Today that covers shopping lists,
recipes, store offers, your bonus balance and product lookup. Shopping lists
are the most developed part, but the scope is whatever the ICA app can do, not
groceries or recipes in particular (see [Roadmap](#roadmap)).

> *"What's on my shopping list?"* · *"Add coffee and bananas to the Willys list"* ·
> *"Check off milk"* · *"Clear the checked items"*

ICA has no official public API. This server talks to the same private backend the
ICA mobile app uses, authenticating with your **personnummer + password** (no
BankID required for accounts that support password login).

> ⚠️ **Unofficial & unaffiliated.** This project is not affiliated with, endorsed
> by, or supported by ICA. It relies on a private, undocumented API that can change
> or break at any time, and using it may be against ICA's terms of service. Use at
> your own risk, for personal use only.

This repository is a fork of [kanylbullen/ica-mcp](https://github.com/kanylbullen/ica-mcp)
that has since grown well past its last release (see [Status](#status)). Tool
descriptions and replies are in Swedish, matching the ICA app.

## Tools

| Tool | What it does |
|---|---|
| `list_shopping_lists` | All your lists + how many items remain/checked |
| `view_shopping_list` | Contents of a list (by name; defaults to your primary list) |
| `add_items` | Add one or more items to a list, each with a name and an optional quantity and unit (`st`, `förp`, `kg`, `hg`, `g`, `l`, `dl`, `cl`, `ml`, `msk`, `tsk`, `krm`). An item that's already on the list gets its amount increased instead of a second row |
| `check_off` / `uncheck` | Mark an item bought / undo |
| `remove_item` | Remove an item entirely |
| `clear_checked` | Remove all checked items (tidy up after shopping) |
| `set_list_store` | Link an existing list to one of your favourite stores (for categories in the ICA app) |
| `create_shopping_list` / `delete_shopping_list` | Create / delete a list. New lists are linked to your primary favourite store (or `store_name`); the ICA app only shows categories for lists with a store |
| `list_saved_recipes` / `get_recipe` | Your favourite recipes; one recipe's ingredients + steps |
| `random_recipes` | Random recipes for inspiration |
| `add_recipe_to_shopping_list` | Add a recipe's ingredients to a list, with their quantities and units. The app shows the recipe under *Tillagd från recept* on each item |
| `list_stores` / `get_offers` | Your favourite stores; current offers for a store |
| `get_bonus` | Your ICA bonus / Stammis balance |
| `get_product` | Look up a product by barcode (EAN/GTIN) |
| `search_products` | Search ICA's product catalogue, e.g. to find the `product_id` for an item that wasn't linked automatically |
| `link_item` | Sort an item already on a list: link it to a catalogue product or give it a section (category) |
| `add_product_to_shopping_list` | Look up a barcode and add the product's name to a list |
| `offers_on_my_list` | Which items on your list are on sale at a store |
| `add_recipes_to_shopping_list` | Merge several recipes' ingredients onto one list; each item shows how much each recipe needs |
| `plan_dinners` | Random weekly menu → one aggregated shopping list |

Lists, items and stores are referenced **by name**, so an agent can act on
natural language. Omitting a list/store name targets your primary one (the
`Handla` list / your first favourite store).

### Safety around destructive tools

The server can change your real ICA account, so the tools that delete data are
deliberately strict:

- `delete_shopping_list`, `remove_item` and `clear_checked` need the list's
  **exact** name (or id). An empty name, the primary list by default, or a
  partial match is refused instead of guessed. Read-only and additive tools
  keep the forgiving name matching.
- Every tool carries MCP annotations (read-only / write / destructive), so a
  client can ask for confirmation before the destructive ones.
- Barcodes must be 8–14 digits before they are looked up.
- Quantities and units are validated; an unknown recipe unit stays in the item
  name instead of being dropped.

### Product linking and categories

The ICA app sorts a list by section (Mejeri, Frukt & Grönt …) when the list
has a store. An item gets its section from the product it is linked to in
ICA's catalogue (~6,900 generic products such as `mjölk` or `krossad tomat`).
**Free text that doesn't match a product ends up under _Ospecificerad_.** The
server therefore links items, without guessing:

- `add_items` links an item when its name exactly matches a product name or
  plural (case-insensitive; notes in brackets such as `(à ca 400 g)` are
  ignored). Otherwise the item is added as free text, in its `category`
  (section) if the agent gave one, and the reply lists the unsorted ones with
  suggestions.
- `link_item` sorts an item already on the list, by `product_id` (from the
  suggestions or `search_products`) or by `category`.
- Recipe ingredients keep their names and are linked by exact name first,
  then by ICA's own recipe `ingredientId`. The id is only a fallback because
  it is sometimes too coarse (`krossade tomater` → `tomat`, i.e. fresh
  tomatoes); in a sample of 44 unmatched ingredients it gave the right section
  for 41.
- Barcode lookups are linked via the product's own `articleId`.
- `view_shopping_list` shows each item's section, so unsorted items are easy
  to spot.

### One row per item

Adding something that's already on the list (and not checked off) increases
that row instead of creating a duplicate, like the ICA app does when you add
an item by hand. This applies to `add_items`, the recipe tools, `plan_dinners`
and barcodes:

- The same item means the same name (ignoring case, spacing and Unicode form),
  or two different names linked to the same catalogue product. Links that are
  only approximate never merge different names: a recipe's coarse ingredient
  id ("körsbärstomater på burk" → tomat), an amount kept in the name
  ("vitlök (3 klyftor)"), or a row whose name doesn't match its product.
- Amounts are converted within volume (`krm`, `tsk`, `msk`, `ml`, `cl`, `dl`,
  `l`) and weight (`g`, `hg`, `kg`). The sum is shown in the larger unit when
  it is exact there (2 msk + 1 dl olja = 1,3 dl), otherwise in the smaller one
  (1 tsk + 1 msk = 4 tsk). Units that don't convert (`st` and `g`) stay on
  separate rows.
- Pass `merge=False` to `add_items` or the barcode tool to always add a new row.
- Recipe shares are added to the row's *Tillagd från recept*, so the app still
  shows how much each recipe needs.
- The reply lists which rows were increased.

The catalogue (~3 MB) is fetched on first use and cached in memory and on disk
for 24 hours (`ica-mcp` in the per-user cache dir; set `ICA_CACHE_DIR` to move
it). If it can't be fetched, items are added as free text.

## Requirements

- **Python 3.10+**
- **A Swedish egress IP.** ICA's API gateway (`apimgw-pub.ica.se`) returns
  HTTP 451 to non-Swedish IPs. Run this on a machine/network in Sweden — a
  US/DE VPS will not work.
- An **ICA account that logs in with personnummer + password** (accounts locked
  to BankID-only are not supported).

## Setup

Everything is one `ica-mcp` command with four subcommands:

| Command | What it does |
|---|---|
| `ica-mcp login` | Log in once (personnummer + password) and cache the session |
| `ica-mcp register` | Register the server with Claude Code (`--scope user`) |
| `ica-mcp status` | Show the cache location + whether the session is valid (no login) |
| `ica-mcp serve` | Run the MCP server over stdio — what your client launches |

### 1. Install

The quickest path uses [uv](https://docs.astral.sh/uv/) — one command, identical
on Windows, macOS and Linux:

```bash
# (only if you don't have uv yet)
#   Windows:      winget install --id=astral-sh.uv -e
#   macOS/Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh

uv tool install git+https://github.com/jprintz/ica-mcp
uv tool update-shell      # puts `ica-mcp` on PATH — then open a NEW terminal
```

> **Install from the repo, not PyPI.** The `ica-mcp` package on PyPI is the
> original project's `0.5.0`, not this fork. It has none of the work described
> under [Status](#status): no product linking, merging, structured quantities,
> `set_list_store`, destructive-tool safety or auth hardening.

<details>
<summary>Fallback without uv (plain pip + venv)</summary>

```bash
git clone https://github.com/jprintz/ica-mcp.git
cd ica-mcp
python3 -m venv .venv
.venv\Scripts\pip install .      # Windows
.venv/bin/pip install .          # macOS / Linux
```

Then use the venv's `ica-mcp` in place of the bare command below:
`.venv\Scripts\ica-mcp.exe` (Windows) or `.venv/bin/ica-mcp` (macOS/Linux).
</details>

### 2. Log in (once)

```bash
ica-mcp login          # prompts for personnummer + password (hidden)
```

On success it prints your name and lists and caches the session (OAuth client +
tokens) in a **per-user state dir** — `%LOCALAPPDATA%\ica-mcp` (Windows),
`~/.local/state/ica-mcp` (Linux), `~/Library/Application Support/ica-mcp` (macOS).
The short-lived access token (~15 min) is auto-refreshed via the long-lived
refresh token, so you normally log in only once. Verify anytime:

```bash
ica-mcp status         # cache path + session validity, without logging in
```

### 3. Register with your MCP client

**Claude Code** — let the CLI do it (writes at user scope, resolving the right path):

```bash
ica-mcp register
```

Registering at `--scope user` sidesteps a Windows drive-letter project-key quirk.
If the `claude` CLI isn't found, `register` prints a ready-to-paste `mcp.json`.
The equivalent manual command is:

```bash
claude mcp add ica --scope user -- ica-mcp serve
```

**Any other MCP client** (`mcp.json`):

```json
{
  "mcpServers": {
    "ica": {
      "command": "ica-mcp",
      "args": ["serve"]
    }
  }
}
```

If `ica-mcp` isn't on PATH, use the absolute path uv printed at install, or
`python -m ica_mcp serve`.

Restart your MCP client and the tools appear. The server reads the cached session,
so it needs no credentials in its environment. If the refresh token ever expires,
re-run `ica-mcp login` — or set `ICA_USER` / `ICA_PASS` (see `.env.example`) so
`serve` can re-authenticate unattended. Relocate the cache with `ICA_STATE_FILE`.

## How authentication works

ICA migrated (around 2024) from a simple Basic-auth API to an OAuth 2.0 / OIDC
flow backed by a [Curity](https://curity.io) Identity Server at
`ims.icagruppen.se`, with the data API behind an F5 gateway at
`apimgw-pub.ica.se`. The flow is:

1. bootstrap client-credentials token (`scope=dcr`)
2. dynamic client registration (`POST /register`) → per-install client
3. PKCE authorize → HTML login form
4. `POST /authn/authenticate/IcaCustomers` with personnummer + password
5. exchange the resulting code for a Bearer access/refresh token

The auth flow is a standalone port of the excellent
[**LazyTarget/ha-ica-todo**](https://github.com/LazyTarget/ha-ica-todo) Home
Assistant integration — full credit for reverse-engineering the current flow.
The hardcoded DCR bootstrap client id/secret are ICA **app** constants (also
public in that project), not user secrets.

## Security & privacy

- Tokens are cached in a per-user state dir (see *Log in*), outside the repo,
  `chmod 0600` on POSIX. On **Windows** that only toggles the read-only bit, so
  the file is not OS-ACL-protected there — treat the machine account as the
  trust boundary. Set `ICA_STATE_FILE` to relocate the cache.
- The product catalogue cache (see *Product linking and categories*) holds
  only ICA's public product list, no personal data.
- **No `keyring` dependency by design.** The Swedish-egress requirement pushes
  many users onto headless homelab/VPS boxes that lack a Secret Service /
  Credential Manager; a portable `0600` file is the deliberate choice.
- **The newest login wins.** A running server re-reads the cache before it
  refreshes, so if you run `ica-mcp login` with a different ICA account, the
  server switches to that account (and logs a warning) instead of overwriting
  your new login. Use separate `ICA_STATE_FILE`s to run two accounts side by side.
- Nothing is logged to stdout (stdout is reserved for the MCP protocol — logs go
  to stderr).
- This server can **modify your real ICA account** (add/remove items, delete
  lists). Write operations were validated against throwaway lists during
  development.

## Status

**Shipped**

| Phase | What |
|---|---|
| 1 | Shopping lists: view, add, check off, remove, clear, create, delete |
| 2 | Recipes, store offers, bonus balance |
| 3 | Product / barcode lookup |
| 4 | Smart flows: `offers_on_my_list`, `add_recipes_to_shopping_list`, `plan_dinners` |
| 5 | Hardening and list quality: exact-name safety for destructive tools, structured quantities, per-list store (`set_list_store`), product linking and `search_products`, recipe attribution, merging duplicate rows, auth-state fixes (stale or rotated tokens), tests and CI |

This fork is distributed from this repository only. It is not published to
PyPI (the `ica-mcp` package there is the original project's `0.5.0`).

## Roadmap

The scope is anything the ICA app does for your account. It is not tied to
food, recipes or any one feature. What can be built depends on the endpoints
we can reach: the server only talks to ICA endpoints the mobile app already
uses, and we only know the ones listed under *Known services* below.

### Known services

All under the `sverige/digx/mobile/` gateway path:
`shoppinglistservice`, `recipeservice`, `storeservice`, `offerservice`,
`bonusservice`, `productservice`. Each tool in this server maps onto one of
them. Anything outside this list is unmapped, not known to be unavailable.

### Doable now (known endpoints)

- **Clear a list's store** in `set_list_store`.
- **Better `search_products` suggestions for Swedish compound words**
  (`basmatiris` should suggest `ris`). Suggestions only; nothing is linked
  from a search.
- **Cross-process lock around token refresh**, so two servers sharing one
  login cannot rotate the refresh token over each other.

### Needs a prototype first

- **Offers-aware flows** — use a store's offers to guide what gets added to a
  list or planned. The data is already reachable (`get_offers`), but this
  would be a heuristic and it is untested. A check against one live store
  (259 offers) showed:
  - Offers cover the whole store, not just food: about half were home,
    health and beauty items. That makes it a general feature, not a grocery one.
  - Offer names are generic ("Pasta", "Citroner", "Kronljus"), so matching
    them against anything else (list items, recipe ingredients) is fuzzy.
    Plain word matching misses plurals and compounds ("paprikor" vs "Röd
    spetspaprika"). `offers_on_my_list` already has this weakness.
  - Offers change weekly.

  The first step would be to measure how good the matching is on real lists
  before building a tool around it.

### Needs API discovery first

Beyond the six services above, the app very likely calls more. Nobody has
mapped them, so this is the biggest unknown. Anything here needs the endpoint
found and its request and response shapes captured before any code can be
written. Capturing needs a Swedish connection and an ICA login. Ways to find
them, roughly cheapest first:

1. **Read what others have mapped** (ha-ica-todo, svendahlstrand/ica-api).
   Costs nothing, but the second one covers the old, defunct backend.
2. **Static analysis of the Android app** (decompile the APK and list the
   `digx/mobile` service paths and models). No pinning problems, and it works
   without a Swedish IP. It gives paths and shapes but not live behaviour.
3. **Capture live traffic** (mitmproxy against an emulator or phone). This
   confirms real request and response shapes. Certificate pinning in the app
   may block it.
4. **Probe the gateway** with an authenticated read-only request per candidate
   path. This is the most intrusive option and is the last resort.

Known open items:

- **Recipe search by phrase.** `recipes/search` and `searchwithfilters`
  return HTTP 500 for every GET parameter shape tried (and 405 on POST), so
  the real request shape is unknown.
- **Personal offers across stores.** We only read per-store offers
  (`offersdiscounts/{store}`). Those already include some offers flagged
  personal, shown as `personal` in `get_offers`. Whether the app has a separate
  endpoint covering all stores is unknown.

### Not planned

- **Portion scaling and a pantry / "already have" list.** Out of scope: this
  server edits lists, it doesn't manage recipes or a household inventory.
- **BankID login.** Only accounts that log in with personnummer + password
  work.
- **Running outside Sweden.** ICA's gateway answers HTTP 451 to other IPs.
- **Ordering / checkout / ICA online shopping.** A different system that has
  not been explored, and one that would spend real money.
- **A hosted multi-user service.** It would mean holding other people's ICA
  passwords. The server is meant to run on your own machine, for your own
  account.

## Credits

- Forked from [kanylbullen/ica-mcp](https://github.com/kanylbullen/ica-mcp)
- Auth flow ported from [LazyTarget/ha-ica-todo](https://github.com/LazyTarget/ha-ica-todo)
- Historical API reference: [svendahlstrand/ica-api](https://github.com/svendahlstrand/ica-api)
  (documents the now-defunct `handla.api.ica.se` backend)

## Development

```bash
pip install -e ".[dev]"          # pytest + ruff
pytest -q
ruff check ica_mcp tests         # the lint gate CI runs
```

Conventions, hard rules and the definition of done for contributors and AI
agents are in [AGENTS.md](AGENTS.md) (`CLAUDE.md` imports it). Repo-specific
Claude Code skills live in `.claude/skills/` (`/live-check`, `/review`,
`/release`).

The suite (`tests/`, ~300 tests) runs offline against a mocked HTTP layer and
fake clients: auth-state handling, ingredient aggregation and merging, product
linking, quantity parsing, recipe attribution, the safety rules and tool
annotations, and the pure formatting helpers. The live ICA API can't run in CI
(it needs a Swedish IP and a login), so end-to-end behaviour is checked by
hand, against throwaway lists.

CI runs the suite on Python 3.10–3.13, plus once against the lowest supported
`mcp` version.

## License

MIT — see [LICENSE](LICENSE).
