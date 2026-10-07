"""Liveness check: is each posting still open? Runs in code, between dedup and screening.

The research workers are told to verify postings, but web_fetch returns a trimmed (sometimes
cached) copy of the page – Dice, for one, keeps serving the full description on closed jobs with
HTTP 410 and a small "no longer available" note that the trimmed copy drops. A plain HTTP request
sees the status code and the whole page, so this is deterministic, free, and testable.

Postings on Ashby, Greenhouse, Lever, and Workday are asked about through the ATS's job-board API
(sweep/ats.py) instead – those APIs answer 404 for a closed job, which beats reading HTML, and
it's the only way to check Ashby and Workday, whose pages are JavaScript shells.

Results:
  not_a_posting – the URL can't be one specific job: empty, or a search/listing page (e.g. Dice's
            /jobs/q-angular-l-nevada-jobs, which always loads fine). Checked from the URL alone,
            no request. Dropped before screening.
  closed  – a clear signal the job is gone (404/410, closed-job text, no longer listed by the
            ATS API). Dropped before screening.
  live    – page loaded and no closed signal was found, or the ATS API lists it.
  unknown – blocked, timed out, or a JavaScript-rendered page whose HTML can't show its status.
            Kept, and screening is told to flag it.
"""

import asyncio
import urllib.error
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import ats, config


@dataclass(frozen=True)
class Liveness:
    state: str  # "live" | "closed" | "unknown" | "not_a_posting"
    reason: str


_SEARCH_QUERY_KEYS = {"q", "query", "keyword", "keywords", "search", "kw"}


def _segment_after(parts: list[str], name: str) -> bool:
    """True if path segment `name` appears and is followed by at least one more segment."""
    return name in parts and parts.index(name) < len(parts) - 1


def posting_url_problem(url: str) -> str | None:
    """Why this URL can't be a single job posting, or None if its shape looks right.

    Search and listing pages always load with HTTP 200, so the request-based checks below
    would call them "live". Known job boards get an exact posting-URL shape; anything else
    is only rejected when it's plainly a search."""
    parsed = urlparse(url.strip()) if url else None
    if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "no usable URL"
    host = parsed.hostname.lower()
    parts = [p.lower() for p in parsed.path.split("/") if p]

    if host.endswith("dice.com"):
        ok = _segment_after(parts, "job-detail")         # /job-detail/<id>
    elif host.endswith("myworkdayjobs.com"):
        ok = _segment_after(parts, "job")                # /<site>/job/<location?>/<title>_<req id>
    elif host.endswith("greenhouse.io"):
        ok = _segment_after(parts, "jobs")               # /<company>/jobs/<id>
    elif host.endswith("ashbyhq.com") or host.endswith("lever.co"):
        ok = len(parts) >= 2                             # /<company>/<id>
    else:
        ok = "search" not in parts and not (set(parse_qs(parsed.query)) & _SEARCH_QUERY_KEYS)
    return None if ok else f"search or listing page on {host}, not a single posting"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def classify(url: str, fetch=ats.request) -> Liveness:
    """Decide one posting's state. `fetch` is injectable so tests run offline."""
    problem = posting_url_problem(url)
    if problem:
        return Liveness("not_a_posting", problem)
    host = _host(url)
    try:
        api_state = ats.posting_state(url, fetch)
        if api_state:
            return Liveness(*api_state)
        resp = fetch(url)
    except ats.BoardError as err:
        return Liveness("unknown", f"job-board API problem: {err}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        return Liveness("unknown", f"request failed: {type(err).__name__}")

    if resp.status in (404, 410):
        return Liveness("closed", f"HTTP {resp.status}")
    if resp.status >= 400:
        return Liveness("unknown", f"HTTP {resp.status} (blocked or server error) – open it to confirm")

    body = resp.body.lower()
    for marker in config.CLOSED_MARKERS:
        if marker in body:
            return Liveness("closed", f'page says "{marker}"')

    if any(host.endswith(h) for h in config.JS_RENDERED_HOSTS):
        return Liveness("unknown", "page is rendered by JavaScript, so its HTML can't show whether it's open")
    return Liveness("live", f"HTTP {resp.status}, no closed signal")


async def check_all(urls: list[str]) -> dict[str, Liveness]:
    """Classify every URL concurrently (blocking requests run in threads)."""
    sem = asyncio.Semaphore(config.LIVENESS_PARALLEL)

    async def one(url: str) -> tuple[str, Liveness]:
        async with sem:
            return url, await asyncio.to_thread(classify, url)

    return dict(await asyncio.gather(*(one(u) for u in urls)))
