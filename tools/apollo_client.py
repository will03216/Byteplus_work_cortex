"""
Apollo.io Organization Enrich client with PostgreSQL cache.

Single public function: fetch_company(domain, company_name) -> dict

Cache strategy:
- Keyed by domain (primary) or company_name (fallback)
- Hits the DB first; on miss, calls Apollo API and writes result back
- Even empty results are cached to avoid re-querying unknown companies
- Cache pool is lazy-initialized from DATABASE_URL env var; if DB is
  unavailable the cache is simply skipped and Apollo is called directly
- If APOLLO_API_KEY is not set, returns {} immediately (no-op)
"""

import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"
_APOLLO_PEOPLE_URL = "https://api.apollo.io/api/v1/people/match"

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS apollo_cache (
    domain     TEXT        PRIMARY KEY,
    data       JSONB       NOT NULL DEFAULT '{}',
    cached_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_PEOPLE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS apollo_people_cache (
    cache_key  TEXT        PRIMARY KEY,
    data       JSONB       NOT NULL DEFAULT '{}',
    cached_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# ── Lazy cache pool ───────────────────────────────────────────────────────────

_pool = None
_pool_attempted = False


async def _ensure_pool():
    global _pool, _pool_attempted
    if _pool_attempted:
        return _pool
    _pool_attempted = True

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return None

    try:
        import asyncpg

        async def _init(conn):
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

        _pool = await asyncpg.create_pool(db_url, init=_init, min_size=1, max_size=3)
        await _pool.execute(_CACHE_DDL)
        await _pool.execute(_PEOPLE_CACHE_DDL)
        logger.info("Apollo cache: DB connection established")
    except Exception as exc:
        logger.warning("Apollo cache: DB unavailable (%s) — cache disabled", exc)
        _pool = None

    return _pool


async def _cache_get(key: str) -> Optional[dict]:
    pool = await _ensure_pool()
    if not pool:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT data FROM apollo_cache WHERE domain = $1", key
        )
        return row["data"] if row else None
    except Exception as exc:
        logger.debug("Apollo cache read error: %s", exc)
        return None


async def _cache_set(key: str, data: dict) -> None:
    pool = await _ensure_pool()
    if not pool:
        return
    try:
        await pool.execute(
            """INSERT INTO apollo_cache (domain, data)
               VALUES ($1, $2)
               ON CONFLICT (domain) DO UPDATE SET data = $2, cached_at = now()""",
            key,
            data,
        )
    except Exception as exc:
        logger.debug("Apollo cache write error: %s", exc)


# ── API call ──────────────────────────────────────────────────────────────────

def _ssl_verify() -> bool:
    return os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")


async def _call_apollo(api_key: str, timeout: int, **params) -> dict:
    """Single Apollo enrich call. Returns organization dict or {}."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=_ssl_verify(),
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        ) as client:
            resp = await client.post(_APOLLO_ENRICH_URL, json=params)
            if resp.status_code == 200:
                return resp.json().get("organization") or {}
            if resp.status_code not in (404, 422):
                logger.warning(
                    "Apollo API returned %d for params=%s", resp.status_code, params
                )
    except Exception as exc:
        logger.warning("Apollo API call failed (%s): %s", params, exc)
    return {}


async def _people_cache_get(key: str) -> Optional[dict]:
    pool = await _ensure_pool()
    if not pool:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT data FROM apollo_people_cache WHERE cache_key = $1", key
        )
        return row["data"] if row else None
    except Exception as exc:
        logger.debug("Apollo people cache read error: %s", exc)
        return None


async def _people_cache_set(key: str, data: dict) -> None:
    pool = await _ensure_pool()
    if not pool:
        return
    try:
        await pool.execute(
            """INSERT INTO apollo_people_cache (cache_key, data)
               VALUES ($1, $2)
               ON CONFLICT (cache_key) DO UPDATE SET data = $2, cached_at = now()""",
            key,
            data,
        )
    except Exception as exc:
        logger.debug("Apollo people cache write error: %s", exc)


# ── Public interface ──────────────────────────────────────────────────────────

async def fetch_person(
    linkedin_url: str = "",
    person_name: str = "",
    domain: str = "",
) -> dict:
    """
    Return Apollo person data (email, email_status, etc.) for a contact.

    Lookup priority:
      1. linkedin_url alone — most reliable, globally unique
      2. person_name + domain — fallback when no LinkedIn URL

    Returns {} when APOLLO_ENABLED is not set, no API key, or no usable inputs.
    """
    if os.getenv("APOLLO_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return {}

    api_key = os.getenv("APOLLO_API_KEY")
    if not api_key:
        return {}

    # Internal LinkedIn member IDs (/in/ACoAA...) are not indexed by Apollo — fall back to name+domain
    if linkedin_url and "/ACoAA" in linkedin_url:
        linkedin_url = ""

    if not linkedin_url and not (person_name and domain):
        return {}

    cache_key = linkedin_url or f"{person_name}@{domain}"
    timeout = int(os.getenv("APOLLO_TIMEOUT_SECONDS", "10"))

    cached = await _people_cache_get(cache_key)
    if cached is not None:
        logger.debug("Apollo people cache HIT: %s", cache_key)
        return cached

    logger.debug("Apollo people cache MISS: %s — calling API", cache_key)

    params: dict = {}
    if linkedin_url:
        params["linkedin_url"] = linkedin_url
    else:
        params["name"] = person_name
        params["domain"] = domain

    result: dict = {}
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=_ssl_verify(),
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        ) as client:
            resp = await client.post(_APOLLO_PEOPLE_URL, json=params)
            if resp.status_code == 200:
                result = resp.json().get("person") or {}
            elif resp.status_code not in (404, 422):
                logger.warning(
                    "Apollo people API returned %d for params=%s", resp.status_code, params
                )
    except Exception as exc:
        logger.warning("Apollo people API call failed (%s): %s", params, exc)

    await _people_cache_set(cache_key, result)
    return result


async def fetch_company(domain: str = "", company_name: str = "") -> dict:
    """
    Return Apollo organization data for a company.

    Disabled by default. Set APOLLO_ENABLED=true in .env to activate.

    Lookup order:
      1. DB cache (keyed by domain, or company_name if no domain)
      2. Apollo API with domain
      3. Apollo API with company_name if domain lookup returned nothing
      4. Write result (even if empty) back to cache
    """
    if os.getenv("APOLLO_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return {}

    api_key = os.getenv("APOLLO_API_KEY")
    if not api_key:
        return {}

    if not domain and not company_name:
        return {}

    cache_key = domain or company_name
    timeout = int(os.getenv("APOLLO_TIMEOUT_SECONDS", "10"))

    # 1. Cache check
    cached = await _cache_get(cache_key)
    if cached is not None:
        logger.debug("Apollo cache HIT: %s", cache_key)
        return cached

    logger.debug("Apollo cache MISS: %s — calling API", cache_key)

    # 2. Domain lookup
    result: dict = {}
    if domain:
        result = await _call_apollo(api_key, timeout, domain=domain)

    # 3. Name fallback
    if not result and company_name:
        result = await _call_apollo(api_key, timeout, name=company_name)

    # 4. Cache result (even empty — stops repeated API calls for unknown companies)
    await _cache_set(cache_key, result)

    return result
