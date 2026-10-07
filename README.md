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
                    Sonnet 5 + web_search/web_fetch, 3 at a time
```

| File | What it teaches |
|---|---|
| `sweep/research.py` | Server-side tools (`web_search`, `web_fetch`), the manual agent loop, `pause_turn` resumption, then `messages.parse()` for schema-guaranteed extraction |
| `sweep/models.py` | Pydantic models as structured-output contracts |
| `sweep/liveness.py` | Plain HTTP check that each posting is still open – drops 404/410s, closed-job text, Greenhouse error redirects, and Ashby jobs missing from the board API |
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
of the page, and Dice (for one) keeps serving the full description on closed jobs with HTTP 410.
So after dedup, `sweep/liveness.py` requests every lead directly. **Closed** leads are dropped
before screening; **unknown** ones (blocked, or JavaScript-rendered like Workday) are kept and
their notes start with `[Still open? Unconfirmed: …]`. Every result is saved to
`output/<run>/liveness.json`. If you see a dead posting slip through, add its wording to
`CLOSED_MARKERS` in `sweep/config.py`.

## Tracks

A track is one requirement set to search and screen against – say, remote senior roles versus
local contract work. Each `[tracks.<name>]` table in `search.toml` gives:

- `criteria_file` – the full write-up screening judges every lead against (hard filters, positive
  signals, notes). Plain markdown; see `examples/criteria-*.md` for the shape.
- `search_profile` – the short version the research workers search for.
- `sources` – which job boards to search (keys of `SOURCES` in `sweep/config.py`).

`--track` picks one; `default_track` (or the first track listed) is used otherwise.

```powershell
python -m sweep --track local --dry-run --sources dice   # cheapest check of another track
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
