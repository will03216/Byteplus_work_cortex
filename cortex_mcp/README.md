# Cortex MCP Server

Exposes Cortex account intelligence as tools for Claude.ai desktop. When configured, AEs can ask Claude to look up any account, contact, or signal before a sales call — without opening the web UI or running any commands.

## Prerequisites

- Cortex web server running: `python ui/server.py`
- `DATABASE_URL` set in `.env` (so entity memory is populated from pipeline runs)
- Python 3.11+ (the `mcp` package requires 3.10+): use `/opt/homebrew/bin/python3.11` on Mac
- `mcp` installed: `/opt/homebrew/bin/python3.11 -m pip install mcp`

## Setup — Claude.ai Desktop

1. Find the Claude desktop config file:
   - **Mac**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

2. Add the `cortex` MCP server entry:

```json
{
  "mcpServers": {
    "cortex": {
      "command": "python",
      "args": ["/absolute/path/to/repo/cortex_mcp/server.py"],
      "env": {
        "CORTEX_BASE_URL": "http://localhost:8080",
        "CORTEX_USERNAME": "admin",
        "CORTEX_PASSWORD": "admin"
      }
    }
  }
}
```

Replace `/absolute/path/to/repo` with the full path to this repository.

3. Restart Claude.ai desktop. The **cortex** server will appear in the tools panel.

## Available Tools

| Tool | What it does |
|---|---|
| `get_account_intelligence` | Full pre-meeting bundle: profile + signals + lead history + AE feedback |
| `search_accounts` | Find accounts in Cortex by company name or domain fragment |
| `get_person_profile` | Look up a contact: title, seniority, fit scores, lead history |
| `evaluate_fit` | Score a company/person fresh against a campaign (seedance or kling) |
| `get_signals` | Signal timeline only: funding, AI hiring, news, outreach replies |
| `record_outcome` | Log a call outcome (won, lost, no_fit, meeting_booked, etc.) |

## Example Conversations

**Pre-meeting research:**
> "Get account intelligence for acme.com — I have a Seedance demo with their VP of Content in an hour."

**Account search:**
> "Search Cortex for any accounts matching 'Midjourney'."

**Contact lookup:**
> "Look up Sarah Chen at runway.ml in Cortex."

**Signal check:**
> "What signals do we have on bytedance.com from the last 30 days?"

**Record outcome:**
> "Record that lead ID abc-123 for the seedance campaign was a win. Notes: closed on enterprise plan, champion was the Head of Creative."

## Configuration

All config comes from environment variables. The `.env` file in the project root is loaded automatically.

| Variable | Default | Description |
|---|---|---|
| `CORTEX_BASE_URL` | `http://localhost:8080` | URL of the running Cortex web server |
| `CORTEX_USERNAME` | `admin` | Login username (`APP_USERNAME` in server config) |
| `CORTEX_PASSWORD` | `admin` | Login password (`APP_PASSWORD` in server config) |

## Troubleshooting

**"Cannot reach Cortex"** — The FastAPI server is not running. Start it with `python ui/server.py`.

**"Login failed"** — Check `CORTEX_BASE_URL`, `CORTEX_USERNAME`, and `CORTEX_PASSWORD` match the server's `APP_USERNAME` / `APP_PASSWORD` env vars.

**"not found" on account lookup** — The account hasn't been processed through a Cortex pipeline run yet. Run a batch with the company's leads first, then the entity store will have data.

**Tools not showing in Claude** — Restart Claude.ai desktop after editing `claude_desktop_config.json`.
