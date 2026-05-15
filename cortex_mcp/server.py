#!/usr/bin/env python3
"""
Cortex MCP Server — exposes account intelligence tools for Claude.ai desktop.

The FastAPI server (python ui/server.py) must be running before starting this server.
Config via environment variables (or .env in the project root):
  CORTEX_BASE_URL   Base URL of the running Cortex web server (default: http://localhost:8080)
  CORTEX_USERNAME   Login username (default: admin)
  CORTEX_PASSWORD   Login password (default: admin)
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult

BASE_URL = os.getenv("CORTEX_BASE_URL", "http://localhost:8080")
USERNAME = os.getenv("CORTEX_USERNAME", "admin")
PASSWORD = os.getenv("CORTEX_PASSWORD", "admin")

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE_URL, follow_redirects=True, timeout=30)
        await _login(_client)
    return _client


async def _login(client: httpx.AsyncClient) -> None:
    resp = await client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    if resp.status_code not in (200, 302, 303):
        raise RuntimeError(
            f"Cortex login failed (status {resp.status_code}). "
            f"Check CORTEX_BASE_URL={BASE_URL}, CORTEX_USERNAME, CORTEX_PASSWORD."
        )


async def _call(method: str, path: str, **kwargs) -> dict | list:
    """Authenticated HTTP call with one automatic re-login on 401."""
    client = await _get_client()
    resp = await client.request(method, path, **kwargs)
    if resp.status_code == 401:
        await _login(client)
        resp = await client.request(method, path, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _ok(data) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2, default=str))])


def _text(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)])


def _err(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"Error: {msg}")])


# ── Server ────────────────────────────────────────────────────────────────────

server = Server("cortex-account-intelligence")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_account_intelligence",
            description=(
                "Fetch full account intelligence for a company before a sales call. "
                "Returns company profile, recent signals (funding, AI hiring, news mentions, "
                "outreach replies), campaign fit scores, lead history, and recorded AE feedback. "
                "This is the primary pre-meeting research tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Company domain, e.g. acme.com or bytedance.com",
                    },
                },
                "required": ["domain"],
            },
        ),
        Tool(
            name="search_accounts",
            description=(
                "Search for accounts in the Cortex database by company name or domain fragment. "
                "Returns matching company profiles ordered by how often the account has appeared in pipeline runs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Company name or domain fragment to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 10, max 50)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_person_profile",
            description=(
                "Look up a contact's profile including title, seniority, campaign fit summary, "
                "and lead history. Use linkedin_url for the most precise lookup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "person_name": {
                        "type": "string",
                        "description": "Full name of the contact",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Company domain to narrow the search (e.g. acme.com)",
                    },
                    "linkedin_url": {
                        "type": "string",
                        "description": "LinkedIn profile URL — most precise lookup key",
                    },
                    "campaign_id": {
                        "type": "string",
                        "description": "Campaign to evaluate fit against (seedance or kling)",
                        "default": "seedance",
                    },
                },
                "required": ["person_name"],
            },
        ),
        Tool(
            name="evaluate_fit",
            description=(
                "Score a company and/or contact against a Cortex campaign. "
                "Returns fit score, sub-scores (segment, buyer, pain, workflow, commercial fit), "
                "qualification reasons, disqualification reasons, and open unknowns. "
                "Use when you want a fresh fit assessment rather than relying on cached history."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Company domain",
                    },
                    "company_name": {
                        "type": "string",
                        "description": "Company name — used if domain is not available",
                    },
                    "person_name": {
                        "type": "string",
                        "description": "Contact name (optional, improves buyer fit scoring)",
                    },
                    "linkedin_url": {
                        "type": "string",
                        "description": "Contact LinkedIn URL (optional)",
                    },
                    "campaign_id": {
                        "type": "string",
                        "description": "Campaign to score against (seedance or kling)",
                        "default": "seedance",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_signals",
            description=(
                "Fetch recent signal timeline for a company: funding rounds, AI hiring signals, "
                "product launches, news mentions, and outreach replies. "
                "Useful for spotting trigger events to reference in a call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Company domain",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of signals to return (default 20)",
                        "default": 20,
                    },
                },
                "required": ["domain"],
            },
        ),
        Tool(
            name="record_outcome",
            description=(
                "Record the outcome of a call or email interaction for a lead. "
                "Use this after a meeting to log won, lost, no-fit, or follow-up outcomes. "
                "These outcomes feed the Cortex learning loop and improve future account intelligence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Lead ID from Cortex (visible in the UI or run artifacts)",
                    },
                    "campaign_id": {
                        "type": "string",
                        "description": "Campaign the lead was scored under (seedance or kling)",
                    },
                    "outcome_type": {
                        "type": "string",
                        "description": (
                            "Outcome category. One of: won, lost, no_fit, good_fit_bad_timing, "
                            "meeting_booked, false_positive, false_negative"
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Free-text notes — competitor name, objection raised, next step, etc.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Your name or Slack handle",
                    },
                },
                "required": ["lead_id", "campaign_id", "outcome_type"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    try:
        if name == "get_account_intelligence":
            return await _get_account_intelligence(arguments)
        if name == "search_accounts":
            return await _search_accounts(arguments)
        if name == "get_person_profile":
            return await _get_person_profile(arguments)
        if name == "evaluate_fit":
            return await _evaluate_fit(arguments)
        if name == "get_signals":
            return await _get_signals(arguments)
        if name == "record_outcome":
            return await _record_outcome(arguments)
        return _err(f"Unknown tool: {name}")
    except httpx.HTTPStatusError as exc:
        return _err(f"Backend returned {exc.response.status_code}: {exc.response.text[:300]}")
    except httpx.RequestError as exc:
        return _err(f"Cannot reach Cortex at {BASE_URL}. Is the server running? ({exc})")
    except Exception as exc:
        return _err(f"Unexpected error: {exc}")


async def _get_account_intelligence(args: dict) -> CallToolResult:
    domain = (args.get("domain") or "").strip()
    if not domain:
        return _err("domain is required")
    data = await _call("GET", f"/api/agent/memory/company/{domain}")
    return _ok(data)


async def _search_accounts(args: dict) -> CallToolResult:
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    if not query:
        return _err("query is required")
    data = await _call("GET", "/api/agent/accounts/search", params={"q": query, "limit": limit})
    if not data:
        return _text(f"No accounts found matching '{query}'.")
    return _ok(data)


async def _get_person_profile(args: dict) -> CallToolResult:
    person_name  = (args.get("person_name") or "").strip()
    domain       = (args.get("domain") or "").strip()
    linkedin_url = (args.get("linkedin_url") or "").strip()
    campaign_id  = (args.get("campaign_id") or "seedance").strip()
    if not person_name and not linkedin_url:
        return _err("person_name or linkedin_url is required")
    data = await _call("POST", "/api/agent/profile/person", json={
        "person_name": person_name,
        "company_domain": domain,
        "linkedin_url": linkedin_url,
        "campaign_id": campaign_id,
    })
    return _ok(data)


async def _evaluate_fit(args: dict) -> CallToolResult:
    payload = {
        "domain":       (args.get("domain") or "").strip(),
        "company_name": (args.get("company_name") or "").strip(),
        "person_name":  (args.get("person_name") or "").strip(),
        "linkedin_url": (args.get("linkedin_url") or "").strip(),
        "campaign_id":  (args.get("campaign_id") or "seedance").strip(),
        "use_memory":   True,
    }
    if not payload["domain"] and not payload["company_name"]:
        return _err("domain or company_name is required")
    data = await _call("POST", "/api/agent/profile/fit", json=payload)
    return _ok(data)


async def _get_signals(args: dict) -> CallToolResult:
    domain = (args.get("domain") or "").strip()
    limit  = int(args.get("limit") or 20)
    if not domain:
        return _err("domain is required")
    data    = await _call("GET", f"/api/agent/memory/company/{domain}")
    signals = data.get("signals", [])[:limit]
    if not signals:
        return _text(f"No signals on record for {domain}.")
    return _ok(signals)


async def _record_outcome(args: dict) -> CallToolResult:
    lead_id     = (args.get("lead_id") or "").strip()
    campaign_id = (args.get("campaign_id") or "").strip()
    outcome     = (args.get("outcome_type") or "").strip()
    notes       = (args.get("notes") or "").strip()
    owner       = (args.get("owner") or "").strip()
    if not lead_id or not campaign_id or not outcome:
        return _err("lead_id, campaign_id, and outcome_type are required")
    data = await _call("POST", "/api/agent/review-feedback", json={
        "lead_id":        lead_id,
        "campaign_id":    campaign_id,
        "feedback_type":  outcome,
        "feedback_reason": notes,
        "owner":          owner,
    })
    return _text(f"Outcome recorded. Feedback ID: {data.get('feedback_id')}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
