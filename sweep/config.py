"""Knobs for the sweep. Everything tunable lives here."""

import functools
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Personal settings – tracks, location, sheet layout. Git-ignored; copy search.example.toml to start.
SEARCH_SETTINGS_FILE = PROJECT_ROOT / "search.toml"

# Google Sheet – its ID comes from SPREADSHEET_ID in .env; tab names and columns from [sheet].
SERVICE_ACCOUNT_FILE = PROJECT_ROOT / "google-service-account.json"
EXISTING_CSV = PROJECT_ROOT / "data" / "existing.csv"  # fallback: File > Download > CSV of the tracker tab

OUTPUT_DIR = PROJECT_ROOT / "output"

# Models – workers do the reading, the orchestrator does the judging.
WORKER_MODEL = "claude-sonnet-5-5"
SCREEN_MODEL = "claude-opus-5-5"

# Per-worker cost guards. These are the tools' `max_uses` – a hard per-turn cap enforced by the
# API (error code max_uses_exceeded), not an account limit. Searches are 1¢ each; tokens are the
# real cost driver, so don't starve the search budget to save money.
MAX_SEARCHES_PER_SOURCE = 15
MAX_FETCHES_PER_SOURCE = 15
MAX_FETCH_TOKENS = 10_000        # truncate any single fetched page
MAX_PAUSE_CONTINUATIONS = 4      # server-tool turns can pause; cap how often we resume
MAX_PARALLEL_WORKERS = 3

# Job-board API tools (sweep/ats.py, run by our code, not Anthropic's servers). Calls are free, but
# every result is input tokens on the next turn – these caps keep a worker's context in check.
MAX_BOARD_CALLS_PER_SOURCE = 25  # list_company_jobs + get_job_posting combined
MAX_AGENT_TURNS = 30             # each client tool round trip is one more API request
BOARD_LIST_LIMIT = 40            # postings returned per list_company_jobs call
POSTING_DESCRIPTION_CHARS = 6000 # ~1.5K tokens; web_fetch pages are capped at MAX_FETCH_TOKENS

# Liveness check (sweep/liveness.py) – plain HTTP requests between dedup and screening; free.
# The request timeout lives in sweep/ats.py (TIMEOUT_SECONDS), which does all the HTTP.
LIVENESS_PARALLEL = 8
# Lower-case phrases that mean a posting is closed. Add any new wording you see on a dead posting.
CLOSED_MARKERS = [
    "no longer available",
    "no longer accepting applications",
    "no longer accepting applicants",
    "this job has expired",
    "this job has been closed",
    "this position has been filled",
    "this position is no longer",
    "job you are looking for is no longer",
    "posting has been closed",
    "this posting is closed",
]
# Sites whose posting HTML is a JavaScript shell, so a 200 with no closed text proves nothing.
JS_RENDERED_HOSTS = ["myworkdayjobs.com"]

@dataclass(frozen=True)
class Source:
    key: str
    name: str
    search_domains: list[str] = field(default_factory=list)
    hint: str = ""


SOURCES: dict[str, Source] = {
    s.key: s
    for s in [
        Source("ashby", "Ashby job boards", ["jobs.ashbyhq.com"],
               "Company boards live at jobs.ashbyhq.com/<company>."),
        Source("greenhouse", "Greenhouse job boards", ["job-boards.greenhouse.io", "boards.greenhouse.io"],
               "Company boards live at job-boards.greenhouse.io/<company>."),
        Source("lever", "Lever job boards", ["jobs.lever.co"],
               "Company boards live at jobs.lever.co/<company>."),
        Source("hn", "Hacker News 'Who is hiring?'", ["news.ycombinator.com"],
               "Use this month's 'Ask HN: Who is hiring?' thread; follow links to the company's real posting."),
        Source("builtin", "Built In (remote)", ["builtin.com"],
               "Prefer the company's own ATS link over the Built In listing when one is given."),
        # Contract and local-employer sources, rather than remote startup boards.
        Source("staffing", "IT staffing agency job boards",
               ["teksystems.com", "roberthalf.com", "insightglobal.com", "kforce.com",
                "apexsystems.com", "randstadusa.com", "motionrecruitment.com"],
               "The agency's own posting is the posting – there is usually no client ATS link. Note "
               "W-2 vs 1099/C2C, the hourly rate if given, contract length, and the client industry."),
        Source("dice", "Dice", ["dice.com"],
               "Dice is heavy on contract roles. Record employment type (W-2, C2C, C2H) and rate as written."),
        Source("workday_local", "Local employers on Workday", ["myworkdayjobs.com"],
               "Large local employers post on <company>.wd*.myworkdayjobs.com/<site>. Find their "
               "career sites with web search (role plus company or city), then search each site "
               "with list_company_jobs – the pages themselves are JavaScript and fetch as empty."),
    ]
}


@functools.cache
def search_settings() -> dict:
    """Read search.toml once. Loaded on first use, so offline tests don't need the file."""
    if not SEARCH_SETTINGS_FILE.exists():
        raise SystemExit(f"Missing {SEARCH_SETTINGS_FILE.name} – copy search.example.toml "
                         "to search.toml and fill in your own tracks.")
    with open(SEARCH_SETTINGS_FILE, "rb") as f:
        return tomllib.load(f)


def search_location() -> dict:
    """The [location] table: city, region, country – biases web search and local sources."""
    return search_settings()["location"]


@dataclass(frozen=True)
class Track:
    """One requirement set: its criteria file (used by screening), the shorter profile the
    research workers search for, and the sources that suit it."""
    key: str
    criteria_file: str
    search_profile: str
    default_sources: list[str]

    @property
    def criteria_path(self) -> Path:
        return PROJECT_ROOT / self.criteria_file  # absolute paths pass through unchanged


def tracks() -> dict[str, Track]:
    """Every [tracks.<key>] table in search.toml, in file order, checked against SOURCES."""
    result: dict[str, Track] = {}
    for key, t in search_settings().get("tracks", {}).items():
        missing = [f for f in ("criteria_file", "search_profile", "sources") if f not in t]
        if missing:
            raise SystemExit(f"[tracks.{key}] in {SEARCH_SETTINGS_FILE.name} is missing: {', '.join(missing)}")
        unknown = [s for s in t["sources"] if s not in SOURCES]
        if unknown or not t["sources"]:
            raise SystemExit(f"[tracks.{key}] sources must be a non-empty subset of {list(SOURCES)}; "
                             f"unknown: {unknown}")
        result[key] = Track(key, t["criteria_file"], t["search_profile"].strip(), list(t["sources"]))
    if not result:
        raise SystemExit(f"{SEARCH_SETTINGS_FILE.name} defines no [tracks.<name>] tables")
    return result


def default_track() -> str:
    """`default_track` from search.toml, else the first track listed."""
    all_tracks = tracks()
    key = search_settings().get("default_track", next(iter(all_tracks)))
    if key not in all_tracks:
        raise SystemExit(f"default_track = {key!r} is not one of {list(all_tracks)}")
    return key


@dataclass(frozen=True)
class SheetSettings:
    tracker_tab: str     # your hand-curated tab – read only, used for dedup
    inbox_tab: str       # the sweep appends here; you promote rows to the tracker yourself
    company_col: int     # 0-based column indexes in the tracker tab (and its CSV export)
    link_col: int
    notes_col: int


def _column_index(letter: str) -> int:
    """'A' -> 0, 'D' -> 3, 'AA' -> 26."""
    index = 0
    for ch in letter.strip().upper():
        if not "A" <= ch <= "Z":
            raise SystemExit(f"[sheet] column {letter!r} should be a column letter like 'A'")
        index = index * 26 + ord(ch) - ord("A") + 1
    return index - 1


def sheet_settings() -> SheetSettings:
    """The [sheet] table, with defaults for anything left out."""
    s = search_settings().get("sheet", {})
    return SheetSettings(
        tracker_tab=s.get("tracker_tab", "Tracker"),
        inbox_tab=s.get("inbox_tab", "Sweep inbox"),
        company_col=_column_index(s.get("company_column", "A")),
        link_col=_column_index(s.get("link_column", "B")),
        notes_col=_column_index(s.get("notes_column", "C")),
    )
