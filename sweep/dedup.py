"""Deterministic dedup – plain code, no model calls. Cheap, testable, predictable."""

import re
from urllib.parse import urlsplit

from .models import JobLead

_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|technologies|labs|hq)\b\.?", re.I)


def normalize_url(url: str) -> str:
    """Lowercase host, drop scheme/query/fragment/trailing slash (ATS links carry ?gh_src= etc.)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    if host == "boards.greenhouse.io":  # old and new Greenhouse hosts serve the same postings
        host = "job-boards.greenhouse.io"
    return f"{host}{path}"


def normalize_company(name: str) -> str:
    name = _COMPANY_SUFFIXES.sub("", name.lower())
    return re.sub(r"[^a-z0-9]", "", name)


def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"\bsr\b\.?", "senior", title)
    title = re.sub(r"front[\s-]?end", "frontend", title)
    title = re.sub(r"full[\s-]?stack", "fullstack", title)
    return re.sub(r"[^a-z0-9]", "", title)


def dedup_leads(
    leads: list[JobLead], known_urls: set[str]
) -> tuple[list[JobLead], list[tuple[JobLead, str]]]:
    """Return (kept, dropped-with-reason).

    Drops: exact posting already in the sheet, or the same company+title seen twice this run.
    A company that's already tracked with a *different* role is kept – screening gets the
    existing status (see tracked_status) so it can flag it.
    """
    kept: list[JobLead] = []
    dropped: list[tuple[JobLead, str]] = []
    seen_urls: set[str] = set()
    seen_roles: set[tuple[str, str]] = set()

    for lead in leads:
        url_key = normalize_url(lead.url)
        role_key = (normalize_company(lead.company), normalize_title(lead.title))
        if url_key in known_urls:
            dropped.append((lead, "already in sheet"))
        elif url_key in seen_urls or role_key in seen_roles:
            dropped.append((lead, "duplicate within this sweep"))
        else:
            seen_urls.add(url_key)
            seen_roles.add(role_key)
            kept.append(lead)
    return kept, dropped


def tracked_status(lead: JobLead, known_companies: dict[str, str]) -> str | None:
    return known_companies.get(normalize_company(lead.company))
