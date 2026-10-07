# Sourcing sweep

A weekly job-board sweep, built by hand: fan-out research agents → structured extraction →
dedup → liveness check → screening against your written criteria → write-back to an inbox tab
in your Google Sheet job tracker.

```
                 ┌─ research:ashby ──── extract ─┐
                 ├─ research:greenhouse ─ extract ┤
python -m sweep ─┼─ research:lever ──── extract ─┼─► dedup ─► liveness ─► screen (Opus) ─► "Sweep inbox" tab
                 ├─ research:hn ─────── extract ─┤                                    + output/<date>/sweep.csv
                 └─ research:builtin ── extract ─┘
                    Sonnet 5 + web_search/web_fetch + job-board API tools, 3 at a time
```

| File | What it teaches |
|---|---|
| `sweep/research.py` | Server-side tools (`web_search`, `web_fetch`) and client tools (`list_company_jobs`, `get_job_posting`) in one agent loop – `pause_turn` resumption, the `tool_use` → `tool_result` round trip, tool schemas and budgets – then `messages.parse()` for schema-guaranteed extraction |
| `sweep/ats.py` | Plain HTTP + JSON client for the Ashby, Greenhouse, Lever, and Workday job-board APIs – no Claude imports, so it tests offline and can become its own MCP server |
| `sweep/models.py` | Pydantic models as structured-output contracts |
| `sweep/liveness.py` | Plain HTTP check that each posting is still open – asks the ATS API for Ashby/Greenhouse/Lever/Workday postings, otherwise drops 404/410s and closed-job text |
| `sweep/dedup.py` | Keeping deterministic work *out* of the model – cheaper, testable |
| `sweep/screen.py` | Orchestrator pass on a stronger model; server-side refusal `fallbacks` (beta) |
| `sweep/costs.py` | Reading `usage` to price every run |
| `sweep/config.py` | Every knob: models, sources, per-worker search/fetch caps; loads `search.toml` |

## Run

One-time setup: copy `.env.example` to `.env` (API key, sheet ID) and `search.example.toml` to
`search.toml` (your tracks, location, and sheet layout). Both copies are git-ignored. The example
works as-is against the sample criteria in `examples/` – replace them with your own (`criteria/`
is git-ignored for that).

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m unittest                              # offline tests, free
python -m sweep --dry-run --sources ashby       # one board, ~$0.40–0.60 – iterate here
python -m sweep --dry-run                       # all five boards, ~$2–3
python -m sweep                                 # same, and appends to the inbox tab
```

## Liveness check

Workers can't reliably spot closed postings: `web_fetch` returns a trimmed, sometimes cached copy
of the page, and some boards keep serving the full description on closed jobs with HTTP 410.
So after dedup, `sweep/liveness.py` requests every lead directly – through the ATS's job-board
API for Ashby, Greenhouse, Lever, and Workday postings (those APIs answer 404 for a closed job),
and as a plain page request for everything else. **Closed** leads are dropped before screening;
**unknown** ones (blocked, or a board API that's down) are kept and their notes start with
`[Still open? Unconfirmed: …]`. Every result is saved to `output/<run>/liveness.json`. If you see
a dead posting slip through, add its wording to `CLOSED_MARKERS` in `sweep/config.py`.

## Job-board API tools

Ashby and Workday posting pages are JavaScript shells, so `web_fetch` often gets back an empty
page and the worker can't verify the lead. Every worker therefore also gets two **client tools**,
backed by `sweep/ats.py`:

- `list_company_jobs(board_url, title_keywords)` – a company's open postings, filtered by title
  (Workday sites are searched server-side), with location, workplace type, and pay when posted.
- `get_job_posting(posting_url)` – one posting's full description, or `status: closed`.

Unlike `web_search`/`web_fetch`, Anthropic doesn't run these. The model stops with
`stop_reason: "tool_use"`, `research.py` runs the HTTP call and replies with a `tool_result`, and
the loop goes on. That's also how a failure reaches the model: an unknown slug comes back as a
`tool_result` with `is_error: true`, and the model moves on instead of the worker crashing. The calls cost nothing,
but their results are input tokens, so `MAX_BOARD_CALLS_PER_SOURCE`, `BOARD_LIST_LIMIT`, and
`POSTING_DESCRIPTION_CHARS` in `config.py` cap them. Each source's summary line reads e.g.
`ashby: 3 searches, 0 fetches, 16 board-API calls`; the full exchange is in
`output/<run>/transcript-<source>.json`.

## Tracks

A track is one requirement set to search and screen against – say, remote senior roles versus
local contract work. Each `[tracks.<name>]` table in `search.toml` gives:

- `criteria_file` – the full write-up screening judges every lead against (hard filters, positive
  signals, notes). Plain markdown; see `examples/criteria-*.md` for the shape.
- `search_profile` – the short version the research workers search for.
- `sources` – which job boards to search (keys of `SOURCES` in `sweep/config.py`).

`--track` picks one; `default_track` (or the first track listed) is used otherwise.

```powershell
python -m sweep --track local --dry-run --sources staffing   # cheapest check of another track
```

Non-default tracks write to `output/<date>-<track>/` and tag the Source column `<track>:<source>`.

Each run writes `output/<date>/findings-<source>.md` (the raw agent write-ups – read these
when tuning prompts) and `output/<date>/sweep.csv`, and prints a cost breakdown.

## Dedup without Google setup

Until the service account exists: open the sheet → your tracker tab → File → Download → CSV, and
save it as `data/existing.csv`. The sweep dedups against it and writes results to CSV only.

## Google Sheets write-back (one-time, ~15 min)

1. console.cloud.google.com → create a project (e.g. `sourcing-sweep`).
2. APIs & Services → Enable APIs → enable **Google Sheets API**.
3. IAM & Admin → Service Accounts → Create → no roles needed → Keys → Add key → JSON.
4. Save the downloaded file here as `google-service-account.json` (git-ignored), and set
   `SPREADSHEET_ID` in `.env` to the ID from the sheet's URL.
5. Open the JSON, copy `client_email`, and **share the sheet** with that address as Editor.

Set the tab names and the company / link / notes column letters under `[sheet]` in
`search.toml`. The sweep only ever *appends* to the inbox tab (created on first run). It reads the
tracker tab for dedup but never writes to it – promoting a lead is still your call.
