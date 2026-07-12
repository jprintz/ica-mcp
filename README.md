# ica-mcp

An **MCP server for ICA** — Sweden's largest grocery chain — that lets AI agents
(Claude, or any [Model Context Protocol](https://modelcontextprotocol.io) client)
read and edit your ICA shopping lists in natural language:

> *"What's on my shopping list?"* · *"Add coffee and bananas to the Willys list"* ·
> *"Check off milk"* · *"Clear the checked items"*

ICA has no official public API. This server talks to the same private backend the
ICA mobile app uses, authenticating with your **personnummer + password** (no
BankID required for accounts that support password login).

> ⚠️ **Unofficial & unaffiliated.** This project is not affiliated with, endorsed
> by, or supported by ICA. It relies on a private, undocumented API that can change
> or break at any time, and using it may be against ICA's terms of service. Use at
> your own risk, for personal use only.

## Tools

| Tool | What it does |
|---|---|
| `list_shopping_lists` | All your lists + how many items remain/checked |
| `view_shopping_list` | Contents of a list (by name; defaults to your primary list) |
| `add_items` | Add one or more free-text items to a list |
| `check_off` / `uncheck` | Mark an item bought / undo |
| `remove_item` | Remove an item entirely |
| `clear_checked` | Remove all checked items (tidy up after shopping) |
| `create_shopping_list` / `delete_shopping_list` | Create / delete a list |

Lists and items are referenced **by name**, so an agent can act on natural
language. Omitting a list name targets your primary list (`Handla`).

## Requirements

- **Python 3.10+**
- **A Swedish egress IP.** ICA's API gateway (`apimgw-pub.ica.se`) returns
  HTTP 451 to non-Swedish IPs. Run this on a machine/network in Sweden — a
  US/DE VPS will not work.
- An **ICA account that logs in with personnummer + password** (accounts locked
  to BankID-only are not supported).

## Setup

```bash
git clone https://github.com/kanylbullen/ica-mcp.git
cd ica-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 1. First login (establishes a cached session)

```bash
export ICA_USER=YYMMDDXXXX          # your personnummer
python auth_spike.py                # prompts for your password
```

A successful run prints your shopping lists and writes `.ica_auth_state.json`
(mode `0600`, git-ignored) containing the OAuth client + tokens. The short-lived
access token (~15 min) is auto-refreshed via the long-lived refresh token, so you
normally only log in once.

### 2. Register with your MCP client

**Claude Code:**

```bash
claude mcp add ica -- /absolute/path/to/ica-mcp/.venv/bin/python /absolute/path/to/ica-mcp/server.py
```

**Or any MCP client** (`mcp.json`):

```json
{
  "mcpServers": {
    "ica": {
      "command": "/absolute/path/to/ica-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/ica-mcp/server.py"]
    }
  }
}
```

The server reads its cached session from `.ica_auth_state.json` next to
`server.py`, so it works without credentials in the environment. If the refresh
token ever expires, set `ICA_USER` / `ICA_PASS` (see `.env.example`) so the server
can re-authenticate on its own.

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

- Your `.ica_auth_state.json` (tokens) and any exported data are git-ignored and
  never leave your machine.
- Credentials are read from the environment; nothing is logged to stdout (stdout
  is reserved for the MCP protocol — logs go to stderr).
- This server can **modify your real ICA account** (add/remove items, delete
  lists). Write operations were validated against throwaway lists during
  development.

## Roadmap

Phase 1 (shopping lists) is done. Planned next, on the same auth:

- **Saved recipes** (`recipeservice/v1/favorites`, recipe search, random)
- **Offers** (`offerservice/v1/offers/search`) and product/EAN lookup
- Composite tool: *"add a recipe's ingredients to my shopping list"*

## Credits

- Auth flow ported from [LazyTarget/ha-ica-todo](https://github.com/LazyTarget/ha-ica-todo)
- Historical API reference: [svendahlstrand/ica-api](https://github.com/svendahlstrand/ica-api)
  (documents the now-defunct `handla.api.ica.se` backend)

## License

MIT — see [LICENSE](LICENSE).
