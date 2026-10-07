"""Public job-board APIs for Ashby, Greenhouse, Lever, and Workday – plain HTTP + JSON.

Why this exists: Ashby and Workday posting pages are JavaScript shells, so web_fetch often gets an
empty "Jobs" page back and the research worker can't verify the lead. Every ATS behind those pages
serves the same data as JSON – full description, location, often the pay range – so asking the API
is more reliable and cheaper in tokens than fetching rendered HTML.

This module knows nothing about Claude or prompts. research.py wraps two functions here as tools
(list_board, get_posting); liveness.py uses posting_state(). Keeping it dependency-free means it
unit-tests offline and can later move into its own job-boards MCP server unchanged.

Endpoints (all unauthenticated, all answer 404 for an unknown board or a closed job):
  Ashby       GET  api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true  (whole board)
  Greenhouse  GET  boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true
              GET  boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}?pay_transparency=true
  Lever       GET  api.lever.co/v0/postings/{org}?mode=json
              GET  api.lever.co/v0/postings/{org}/{id}
  Workday     POST {tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs  (unofficial, paged)
              GET  {tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/...
"""

import datetime as dt
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

TIMEOUT_SECONDS = 20
MAX_BODY_BYTES = 20_000_000  # Ashby's board JSON carries every description – Ramp's is ~2.7 MB
WORKDAY_PAGE_SIZE = 20       # the CXS API rejects larger pages
WORKDAY_MAX_PAGES = 5

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


# --- HTTP ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Response:
    status: int
    final_url: str
    body: str


def request(url: str, json_body: dict | None = None) -> Response:
    """Blocking GET (or JSON POST when json_body is given). Error statuses come back as a
    Response instead of raising, because a 404 is an answer here, not a failure."""
    headers = dict(_HEADERS)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return Response(resp.status, resp.geturl(), resp.read(MAX_BODY_BYTES).decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        body = err.read(MAX_BODY_BYTES).decode("utf-8", "replace") if err.fp else ""
        return Response(err.code, err.geturl() or url, body)


class BoardError(Exception):
    """A board or posting couldn't be read – unknown board, bad URL, or an unexpected response.
    The message is written to be shown to the model as-is."""


# --- URLs ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BoardRef:
    """Where a board lives. `site` is Workday-only (the career site name); `job` is the posting
    id – or, for Workday, the posting's /job/... path – when the URL pointed at one posting."""
    ats: str      # "ashby" | "greenhouse" | "lever" | "workday"
    org: str      # board slug; the tenant for Workday
    host: str = ""
    site: str = ""
    job: str = ""


_LOCALE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.I)


def parse_url(url: str) -> BoardRef:
    """Recognize a board or posting URL on one of the four ATSes. Raises BoardError otherwise."""
    parsed = urlparse(url.strip() if "://" in url else f"https://{url.strip()}")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if host in ("jobs.ashbyhq.com", "api.ashbyhq.com"):
        if host == "api.ashbyhq.com":
            parts = parts[2:]  # /posting-api/job-board/{org}
        if parts:
            return BoardRef("ashby", parts[0], job=parts[1] if len(parts) > 1 else "")
    elif host.endswith("greenhouse.io"):
        if "for" in query:  # embed: boards.greenhouse.io/embed/job_board?for=acme&token=123
            return BoardRef("greenhouse", query["for"][0], job=query.get("token", [""])[0])
        if host == "boards-api.greenhouse.io":
            parts = parts[2:]  # /v1/boards/{org}/jobs/{id}
        if parts:
            job = parts[2] if len(parts) > 2 and parts[1] == "jobs" else ""
            return BoardRef("greenhouse", parts[0], job=job)
    elif host in ("jobs.lever.co", "api.lever.co"):
        if host == "api.lever.co":
            parts = parts[2:]  # /v0/postings/{org}/{id}
        if parts:
            return BoardRef("lever", parts[0], job=parts[1] if len(parts) > 1 else "")
    elif host.endswith(".myworkdayjobs.com"):
        if parts[:1] == ["wday"]:  # already an API URL: /wday/cxs/{tenant}/{site}/...
            parts = parts[3:]
        if parts and _LOCALE.match(parts[0]):
            parts = parts[1:]
        if parts:
            job = "/" + "/".join(parts[1:]) if len(parts) > 2 and parts[1] == "job" else ""
            return BoardRef("workday", host.split(".")[0], host=host, site=parts[0], job=job)
        raise BoardError(f"Workday URL needs the career-site name, e.g. https://{host}/External")
    raise BoardError(f"Not a recognized Ashby, Greenhouse, Lever, or Workday board URL: {url}")


def _workday_api(ref: BoardRef) -> str:
    return f"https://{ref.host}/wday/cxs/{ref.org}/{ref.site}"


# --- Postings -----------------------------------------------------------------------------------

@dataclass
class Posting:
    """One job, normalized across ATSes. Fields are None when the board doesn't say."""
    ats: str
    company: str
    title: str
    url: str
    location: str
    workplace: str | None = None   # "remote" | "hybrid" | "onsite" when the ATS states it
    employment_type: str | None = None
    salary: str | None = None      # as posted, e.g. "$160K – $200K"
    salary_min: int | None = None  # annual, in the posted currency
    salary_max: int | None = None
    posted: str | None = None      # ISO date when known, else the board's own wording
    description: str = ""          # plain text; empty in board listings

    def to_dict(self, description_chars: int | None = None) -> dict:
        """Drop empty fields and trim the description – this goes into a prompt."""
        d = {k: v for k, v in asdict(self).items() if v not in (None, "")}
        if description_chars is not None and self.description:
            d["description"] = _clip(self.description, description_chars)
        return d


class _TextExtractor(HTMLParser):
    _BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.chunks.append("\n- ")
        elif tag in self._BLOCKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCKS:
            self.chunks.append("\n")

    def handle_data(self, data):
        self.chunks.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup or "")
    text = "".join(parser.chunks).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*(\n\s*)+", "\n\n", text).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …[truncated]"


def _money(n: float) -> str:
    return f"${n / 1000:.0f}K" if n >= 1000 else f"${n:g}"


def _salary_text(lo, hi, currency: str | None, interval: str | None) -> str | None:
    if lo is None and hi is None:
        return None
    sym = (lambda n: _money(n)) if currency in (None, "USD") else (lambda n: f"{n:,.0f} {currency}")
    text = " – ".join(sym(n) for n in (lo, hi) if n is not None)
    if interval and "year" not in interval.lower():
        text += f" per {interval.lower().replace('1 ', '').replace('per-', '')}"
    return text


def _date(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
    return str(value)[:10] if re.match(r"\d{4}-\d{2}-\d{2}", str(value)) else str(value)


def _workplace(value: str | None) -> str | None:
    v = (value or "").lower().replace("-", "").replace("_", "")
    return {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "inoffice": "onsite"}.get(v)


def _from_ashby(org: str, j: dict) -> Posting:
    locations = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
    comp = j.get("compensation") or {}
    salary = next((c for c in comp.get("summaryComponents") or [] if c.get("compensationType") == "Salary"), {})
    annual = "year" in (salary.get("interval") or "").lower()
    return Posting(
        ats="ashby", company=org, title=(j.get("title") or "").strip(), url=j.get("jobUrl", ""),
        location=" / ".join(filter(None, locations)),
        workplace=_workplace(j.get("workplaceType")) or ("remote" if j.get("isRemote") else None),
        employment_type=j.get("employmentType"),
        salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary"),
        salary_min=salary.get("minValue") if annual else None,
        salary_max=salary.get("maxValue") if annual else None,
        posted=_date(j.get("publishedAt")),
        description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
    )


def _from_greenhouse(org: str, j: dict) -> Posting:
    pay = (j.get("pay_input_ranges") or [{}])[0]
    lo = pay["min_cents"] / 100 if pay.get("min_cents") else None
    hi = pay["max_cents"] / 100 if pay.get("max_cents") else None
    return Posting(
        ats="greenhouse", company=j.get("company_name") or org, title=j.get("title", "").strip(),
        url=j.get("absolute_url", ""), location=(j.get("location") or {}).get("name", ""),
        salary=_salary_text(lo, hi, pay.get("currency_type"), None),
        salary_min=int(lo) if lo else None, salary_max=int(hi) if hi else None,
        posted=_date(j.get("first_published") or j.get("updated_at")),
        description=html_to_text(html.unescape(j.get("content") or "")),  # content is escaped HTML
    )


def _from_lever(org: str, j: dict) -> Posting:
    cats = j.get("categories") or {}
    pay = j.get("salaryRange") or {}
    annual = (pay.get("interval") or "").lower() in ("per-year-salary", "")
    sections = [j.get("descriptionPlain", "")]
    sections += [f"{s.get('text', '')}\n{html_to_text(s.get('content', ''))}" for s in j.get("lists") or []]
    sections.append(j.get("additionalPlain", ""))
    return Posting(
        ats="lever", company=org, title=j.get("text", "").strip(), url=j.get("hostedUrl", ""),
        location=" / ".join(cats.get("allLocations") or [cats.get("location", "")]),
        workplace=_workplace(j.get("workplaceType")),
        employment_type=cats.get("commitment"),
        salary=_salary_text(pay.get("min"), pay.get("max"), pay.get("currency"),
                            None if annual else pay.get("interval")),
        salary_min=pay.get("min") if annual else None, salary_max=pay.get("max") if annual else None,
        posted=_date(j.get("createdAt")),
        description="\n\n".join(s.strip() for s in sections if s and s.strip()),
    )


def _from_workday(ref: BoardRef, j: dict, detail: dict | None = None) -> Posting:
    """`j` is a list-page row; `detail` the jobPostingInfo object when we fetched the posting."""
    d = detail or {}
    path = j.get("externalPath") or ref.job
    return Posting(
        ats="workday", company=ref.org, title=(d.get("title") or j.get("title") or "").strip(),
        url=d.get("externalUrl") or f"https://{ref.host}/{ref.site}{path}",
        location=d.get("location") or j.get("locationsText", ""),
        workplace=_workplace(d.get("remoteType")),
        employment_type=d.get("timeType"),
        posted=_date(d.get("startDate")) or j.get("postedOn"),
        description=html_to_text(d.get("jobDescription", "")),
    )


# --- Public API ---------------------------------------------------------------------------------

def _json(resp: Response, what: str):
    if resp.status == 404:
        raise BoardError(f"{what}: not found (HTTP 404) – check the company slug / URL")
    if resp.status != 200:
        raise BoardError(f"{what}: HTTP {resp.status}")
    try:
        return json.loads(resp.body)
    except ValueError:
        raise BoardError(f"{what}: response was not JSON") from None


def _matches(title: str, keywords: list[str]) -> bool:
    """Any keyword appears in the title. Hyphens and spaces are ignored, so 'frontend' matches
    'Front-End' and 'Front End'."""
    if not keywords:
        return True
    squash = lambda s: re.sub(r"[\s\-_./]", "", s.lower())
    return any(squash(k) in squash(title) for k in keywords if k.strip())


def list_board(url: str, keywords: list[str] | None = None, fetch=request) -> tuple[list[Posting], int | None]:
    """Open postings on a company's board whose titles match any keyword (all when none given).
    Returns (postings, total_on_board) – postings carry no description, to keep listings small.
    Workday searches server-side, so its board total is unknown (None).
    `fetch` is injectable so tests run offline."""
    ref = parse_url(url)
    keywords = keywords or []
    what = f"{ref.ats} board '{ref.org}'"
    if ref.ats == "ashby":
        data = _json(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{ref.org}?includeCompensation=true"), what)
        rows = [_from_ashby(ref.org, j) for j in data.get("jobs", []) if j.get("isListed", True)]
    elif ref.ats == "greenhouse":
        data = _json(fetch(f"https://boards-api.greenhouse.io/v1/boards/{ref.org}/jobs"), what)
        rows = [_from_greenhouse(ref.org, j) for j in data.get("jobs", [])]
    elif ref.ats == "lever":
        data = _json(fetch(f"https://api.lever.co/v0/postings/{ref.org}?mode=json"), what)
        rows = [_from_lever(ref.org, j) for j in data]
    else:
        return _list_workday(ref, keywords, fetch, what)
    for p in rows:
        p.description = ""
    return [p for p in rows if _matches(p.title, keywords)], len(rows)


def _list_workday(ref: BoardRef, keywords: list[str], fetch, what: str) -> tuple[list[Posting], None]:
    """Workday boards can hold thousands of jobs, so search server-side: one paged query per
    keyword (or one unfiltered query), merged by posting path."""
    found: dict[str, Posting] = {}
    for term in keywords or [""]:
        for page in range(WORKDAY_MAX_PAGES):
            body = {"appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE, "offset": page * WORKDAY_PAGE_SIZE,
                    "searchText": term}
            data = _json(fetch(f"{_workday_api(ref)}/jobs", body), what)
            rows = data.get("jobPostings") or []
            for j in rows:
                found.setdefault(j.get("externalPath", ""), _from_workday(ref, j))
            if len(rows) < WORKDAY_PAGE_SIZE:
                break
    # Workday's search matches descriptions too; keep title matches first, the rest after.
    postings = sorted(found.values(), key=lambda p: not _matches(p.title, keywords))
    return postings, None


def get_posting(url: str, fetch=request) -> Posting | None:
    """One posting with its full description, or None if the board no longer lists it (closed).
    Raises BoardError when the URL isn't a posting or the board can't be read."""
    ref = parse_url(url)
    if not ref.job:
        raise BoardError(f"{url} is a board, not a single posting – use the posting's own URL")
    if ref.ats == "ashby":  # no single-job endpoint: read the board and pick the job out
        what = f"ashby board '{ref.org}'"
        data = _json(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{ref.org}?includeCompensation=true"), what)
        job = next((j for j in data.get("jobs", []) if j.get("id") == ref.job), None)
        return _from_ashby(ref.org, job) if job else None

    endpoint = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{ref.org}/jobs/{ref.job}?pay_transparency=true",
        "lever": f"https://api.lever.co/v0/postings/{ref.org}/{ref.job}",
        "workday": f"{_workday_api(ref)}{ref.job}",
    }[ref.ats]
    resp = fetch(endpoint)
    if resp.status in (404, 410):
        return None  # these APIs answer 404 for a closed posting – checked against live boards
    data = _json(resp, f"{ref.ats} posting")
    if ref.ats == "greenhouse":
        return _from_greenhouse(ref.org, data)
    if ref.ats == "lever":
        return _from_lever(ref.org, data)
    info = data.get("jobPostingInfo") or {}
    if info.get("posted") is False:
        return None
    return _from_workday(ref, {"externalPath": ref.job}, info)


def posting_state(url: str, fetch=request) -> tuple[str, str] | None:
    """("live" | "closed", reason) for a posting URL on a supported ATS; None when the URL isn't
    one (so the caller falls back to other checks). Network or API trouble raises BoardError."""
    try:
        ref = parse_url(url)
    except BoardError:
        return None
    if not ref.job:
        return None
    posting = get_posting(url, fetch)
    if posting is None:
        return "closed", f"no longer listed by the {ref.ats.capitalize()} job-board API"
    return "live", f"listed by the {ref.ats.capitalize()} job-board API"
