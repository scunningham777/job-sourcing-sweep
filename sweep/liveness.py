"""Liveness check: is each posting still open? Runs in code, between dedup and screening.

The research workers are told to verify postings, but web_fetch returns a trimmed (sometimes
cached) copy of the page – Dice, for one, keeps serving the full description on closed jobs with
HTTP 410 and a small "no longer available" note that the trimmed copy drops. A plain HTTP request
sees the status code and the whole page, so this is deterministic, free, and testable.

Results:
  not_a_posting – the URL can't be one specific job: empty, or a search/listing page (e.g. Dice's
            /jobs/q-angular-l-nevada-jobs, which always loads fine). Checked from the URL alone,
            no request. Dropped before screening.
  closed  – a clear signal the job is gone (404/410, closed-job text, ATS redirect, missing from
            the Ashby board API). Dropped before screening.
  live    – page loaded and no closed signal was found.
  unknown – blocked, timed out, or a JavaScript-rendered page whose HTML can't show its status.
            Kept, and screening is told to flag it.
"""

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import config

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
_MAX_BODY_BYTES = 20_000_000  # Ashby board JSON carries every description – Ramp's is ~2.7 MB


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


@dataclass(frozen=True)
class _Response:
    status: int
    final_url: str
    body: str


def _get(url: str) -> _Response:
    """Blocking GET that returns error responses (404, 410…) instead of raising, since the
    status code is the signal we want."""
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=config.LIVENESS_TIMEOUT_SECONDS) as resp:
            return _Response(resp.status, resp.geturl(),
                             resp.read(_MAX_BODY_BYTES).decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        body = err.read(_MAX_BODY_BYTES).decode("utf-8", "replace") if err.fp else ""
        return _Response(err.code, err.geturl() or url, body)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _ashby_check(url: str, fetch) -> Liveness:
    """Ashby pages are a JavaScript shell that loads for any id, so ask the public board API
    whether the job id is still listed."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return Liveness("unknown", "Ashby URL has no job id")
    org, job_id = parts[0], parts[1]
    resp = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    if resp.status != 200:
        return Liveness("unknown", f"Ashby board API returned HTTP {resp.status}")
    try:
        ids = {job.get("id") for job in json.loads(resp.body).get("jobs", [])}
    except (ValueError, AttributeError):
        return Liveness("unknown", "Ashby board API returned unreadable JSON")
    if job_id in ids:
        return Liveness("live", "listed on the Ashby board API")
    return Liveness("closed", "not listed on the company's Ashby board")


def classify(url: str, fetch=_get) -> Liveness:
    """Decide one posting's state. `fetch` is injectable so tests run offline."""
    problem = posting_url_problem(url)
    if problem:
        return Liveness("not_a_posting", problem)
    host = _host(url)
    try:
        if host.endswith("ashbyhq.com"):
            return _ashby_check(url, fetch)
        resp = fetch(url)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        return Liveness("unknown", f"request failed: {type(err).__name__}")

    if resp.status in (404, 410):
        return Liveness("closed", f"HTTP {resp.status}")
    if resp.status >= 400:
        return Liveness("unknown", f"HTTP {resp.status} (blocked or server error) – open it to confirm")

    # Greenhouse sends closed jobs back to the company board with ?error=true.
    if "greenhouse.io" in host and "error=true" in resp.final_url:
        return Liveness("closed", "Greenhouse redirected to the board (job closed)")

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
