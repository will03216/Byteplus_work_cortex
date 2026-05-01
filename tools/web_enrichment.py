"""
Web enrichment scrapers for lead qualification pipeline.

Two async functions:
  - scrape_company_website(domain)  → fetch homepage, extract summary text
  - scrape_linkedin_profile(url)    → fetch public profile, extract tenure/title text

Both are best-effort: they return empty string on any failure so the pipeline
never blocks on enrichment. Claude extracts structured facts from the raw text
during Stage 1 evidence extraction.

v2 extension for LinkedIn: replace fetch logic with Proxycurl API call
($0.01/call) for reliable structured data on all profiles.
"""

import os
import re
import httpx
from bs4 import BeautifulSoup

_TIMEOUT = 10  # seconds per request
_SSL_VERIFY = os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
_MAX_CHARS = 1500  # max text returned per source — enough context, won't bloat prompt

# Headers that avoid the most aggressive bot blocks
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


async def scrape_company_website(domain: str) -> str:
    """
    Fetch company homepage and return a clean text summary (max 1500 chars).

    Extraction priority:
      1. <meta name="description"> — usually the most concise company summary
      2. <title> tag
      3. First 3 <p> tags from main/article/body

    Returns empty string on any failure — enrichment is always best-effort.
    """
    if not domain:
        return ""

    domain = domain.strip().lstrip("https://").lstrip("http://").rstrip("/")

    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers=_HEADERS,
                follow_redirects=True,
                verify=_SSL_VERIFY,
            ) as client:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    continue
                html = resp.text
                break
        except Exception:
            continue
    else:
        return ""  # both schemes failed

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        parts: list[str] = []

        # 1. Meta description
        meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        if meta and meta.get("content"):
            parts.append(meta["content"].strip())

        # 2. Title
        title = soup.find("title")
        if title and title.get_text(strip=True):
            parts.append(title.get_text(strip=True))

        # 3. First meaningful paragraphs from content areas
        content_root = (
            soup.find("main")
            or soup.find("article")
            or soup.find("section")
            or soup.find("body")
        )
        if content_root:
            for p in content_root.find_all("p")[:5]:
                text = p.get_text(separator=" ", strip=True)
                if len(text) > 40:  # skip very short fragments
                    parts.append(text)
                    if len(parts) >= 5:
                        break

        combined = " | ".join(dict.fromkeys(parts))  # deduplicate while preserving order
        return combined[:_MAX_CHARS]

    except Exception:
        return ""


async def scrape_linkedin_profile(linkedin_url: str) -> str:
    """
    Attempt to fetch a public LinkedIn profile page and extract whatever
    static text is available before the JS authentication wall.

    LinkedIn aggressively blocks unauthenticated scraping. What we realistically get:
      ✅ Often: name, headline, current title
      ✅ Sometimes: "X years Y months" tenure in static HTML
      ❌ Usually blocked: full work history, connections

    Even partial data is valuable: a "2 months" tenure for a Founder/CEO fires
    the new-executive signal in Stage 1 and significantly boosts buyer_fit.

    Returns empty string on failure (HTTP 999, login redirect, JS-only page, etc.)

    v2 extension point: replace this entire function body with a Proxycurl API
    call for structured, reliable data on all profiles (~$0.01/call).
    """
    if not linkedin_url:
        return ""

    url = linkedin_url.strip()
    if not url.startswith("http"):
        url = f"https://{url}"

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
            verify=_SSL_VERIFY,
        ) as client:
            resp = await client.get(url)

            # LinkedIn returns 999 for bot-detected requests; 200 may still be login page
            if resp.status_code == 999:
                return ""

            html = resp.text

            # Detect login redirect — LinkedIn serves login page for unauthenticated requests
            if "authwall" in resp.url.path or "login" in resp.url.path:
                return ""
            if "Sign in" in html and "authwall" in html:
                return ""

    except Exception:
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")

        parts: list[str] = []

        # Look for structured LinkedIn profile data in meta tags (often present)
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            parts.append(og_title["content"].strip())

        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            parts.append(og_desc["content"].strip())

        # Extract any visible text that mentions tenure patterns
        # LinkedIn sometimes embeds "X years Y months" in static HTML
        body_text = soup.get_text(separator=" ", strip=True)

        tenure_patterns = [
            r"\d+\s+years?\s+\d+\s+months?",
            r"\d+\s+months?\s+in\s+role",
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[–-]\s*(?:Present|\d{4})",
        ]
        for pattern in tenure_patterns:
            matches = re.findall(pattern, body_text, re.IGNORECASE)
            for m in matches[:3]:
                parts.append(f"Tenure signal: {m}")

        # Grab headline / title sections that LinkedIn may include in static HTML
        for selector in ["h1", "h2", ".top-card-layout__title", ".top-card__title"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                if text and len(text) > 3:
                    parts.append(text)
                    break

        if not parts:
            return ""

        combined = " | ".join(dict.fromkeys(parts))
        return combined[:_MAX_CHARS]

    except Exception:
        return ""
