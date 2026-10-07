"""Monday sourcing sweep.

    python -m sweep --dry-run                 # all sources, results to output/*.csv only
    python -m sweep --dry-run --sources ashby # one source – cheapest way to iterate
    python -m sweep                           # full run, appends to the inbox tab
    python -m sweep --track NAME --dry-run    # another requirement set from search.toml
"""

import argparse
import asyncio
import datetime as dt
import json

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from . import config, sheet
from .costs import CostTracker
from .dedup import dedup_leads
from .liveness import check_all
from .models import JobLead
from .research import extract_leads, research_source
from .screen import screen_leads
from .stats import append_log, format_table, source_funnel

VERDICT_ORDER = {"priority": 0, "candidate": 1, "unscreened": 2, "exclude": 3}


async def run_source(client, source, track, costs, sem, run_dir) -> list[JobLead]:
    async with sem:
        print(f"  → {source.name}: researching…")
        findings = await research_source(
            client, source, track.search_profile, costs,
            transcript_path=run_dir / f"transcript-{source.key}.json",
        )
        (run_dir / f"findings-{source.key}.md").write_text(findings, encoding="utf-8")
        leads = await extract_leads(client, source, findings, costs)
        print(f"  ✓ {source.name}: {len(leads)} leads")
        return leads


def _salary(lead: JobLead) -> str:
    if lead.salary_min and lead.salary_max:
        return f"${lead.salary_min / 1000:.0f}K–${lead.salary_max / 1000:.0f}K"
    return lead.salary_text or ""


async def main(args) -> None:
    load_dotenv(config.PROJECT_ROOT / ".env")
    track = config.tracks()[args.track]
    default_track = config.default_track()
    criteria = track.criteria_path.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    # Non-default tracks get their own folder so two tracks run on the same day don't overwrite each other.
    run_dir = config.OUTPUT_DIR / (today if track.key == default_track else f"{today}-{track.key}")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Track: {track.key} (criteria: {track.criteria_file})")

    source_keys = args.sources.split(",") if args.sources else track.default_sources
    unknown = [k for k in source_keys if k not in config.SOURCES]
    if unknown:
        raise SystemExit(f"Unknown source(s): {unknown}. Options: {list(config.SOURCES)}")

    known_urls, known_companies, mode = sheet.load_existing()
    print(f"Dedup against: {mode} ({len(known_urls)} known postings)")

    client = AsyncAnthropic()
    costs = CostTracker()
    sem = asyncio.Semaphore(config.MAX_PARALLEL_WORKERS)

    # Fan out – one failing source shouldn't sink the others.
    results = await asyncio.gather(
        *(run_source(client, config.SOURCES[k], track, costs, sem, run_dir) for k in source_keys),
        return_exceptions=True,
    )
    all_leads: list[JobLead] = []
    sources_by_url: dict[str, str] = {}
    leads_by_source: dict[str, list[JobLead] | None] = {}
    for key, result in zip(source_keys, results):
        if isinstance(result, BaseException):
            print(f"  ✗ {key}: {type(result).__name__}: {result}")
            leads_by_source[key] = None
            continue
        leads_by_source[key] = result
        all_leads.extend(result)
        # The Source column carries the track too, so non-default-track leads are easy to filter in the inbox.
        label = key if track.key == default_track else f"{track.key}:{key}"
        sources_by_url.update({lead.url: label for lead in result})

    kept, dropped = dedup_leads(all_leads, known_urls)
    print(f"\n{len(all_leads)} leads found, {len(dropped)} dropped as duplicates")

    # Workers can't reliably tell a closed posting from an open one (see liveness.py), so check
    # every lead directly and drop the closed ones before paying to screen them.
    liveness = await check_all([lead.url for lead in kept])
    (run_dir / "liveness.json").write_text(json.dumps(
        {url: {"state": l.state, "reason": l.reason} for url, l in liveness.items()}, indent=2),
        encoding="utf-8")
    dropped_states = ("not_a_posting", "closed")
    gone = [lead for lead in kept if liveness[lead.url].state in dropped_states]
    kept = [lead for lead in kept if liveness[lead.url].state not in dropped_states]
    for lead in gone:
        state = liveness[lead.url].state.replace("_", " ")
        print(f"  ✗ {state}: {lead.company} – {lead.title} ({liveness[lead.url].reason})")
    unconfirmed = {lead.url: liveness[lead.url].reason for lead in kept
                   if liveness[lead.url].state == "unknown"}
    not_postings = sum(1 for lead in gone if liveness[lead.url].state == "not_a_posting")
    print(f"Liveness: {not_postings} not a posting, {len(gone) - not_postings} closed, "
          f"{len(unconfirmed)} unconfirmed, {len(kept) - len(unconfirmed)} live – {len(kept)} to screen")

    # Research is the expensive part, so a screening failure must not lose its results:
    # log it and fall through with no verdicts, which writes every lead as "unscreened".
    verdicts = {}
    if kept:
        try:
            verdicts = await screen_leads(client, kept, known_companies, criteria, costs,
                                          raw_output_path=run_dir / "screening.json",
                                          unconfirmed=unconfirmed)
        except Exception as err:
            print(f"  ✗ screening failed ({type(err).__name__}: {err}) – "
                  "writing all leads as 'unscreened'")

    rows = []
    for lead in kept:
        v = verdicts.get(lead.url)
        label = v.verdict if v else "unscreened"
        notes = v.notes if v else lead.summary
        if lead.url in unconfirmed:
            notes = f"[Still open? Unconfirmed: {unconfirmed[lead.url]}] {notes}"
        rows.append([today, label, lead.company, lead.title, lead.url, lead.location,
                     _salary(lead), notes, sources_by_url.get(lead.url, "")])
    rows.sort(key=lambda r: VERDICT_ORDER.get(r[1], 9))

    csv_path = run_dir / "sweep.csv"
    sheet.write_csv(rows, csv_path)
    if args.dry_run or not config.SERVICE_ACCOUNT_FILE.exists():
        print(f"\nWrote {len(rows)} rows to {csv_path} (sheet not touched)")
    else:
        sheet.append_inbox(rows)
        print(f"\nAppended {len(rows)} rows to '{config.sheet_settings().inbox_tab}' (copy also at {csv_path})")

    counts = {k: sum(1 for r in rows if r[1] == k) for k in VERDICT_ORDER}
    print("Verdicts: " + ", ".join(f"{k} {n}" for k, n in counts.items() if n))

    funnel = source_funnel(leads_by_source, [lead for lead, _ in dropped],
                           {url: l.state for url, l in liveness.items()},
                           {url: v.verdict for url, v in verdicts.items()})
    stats_path = config.OUTPUT_DIR / "source-stats.csv"
    append_log(funnel, today, track.key, stats_path)
    print(f"\nBy source (appended to {stats_path.name}):\n" + format_table(funnel))
    print("\nCost this run:\n" + costs.report())


def cli() -> None:
    parser = argparse.ArgumentParser(description="Agentic job-board sourcing sweep")
    parser.add_argument("--dry-run", action="store_true", help="write CSV only, never touch the sheet")
    parser.add_argument("--track", choices=list(config.tracks()), default=config.default_track(),
                        help="which requirement set to search and screen against (default: %(default)s)")
    parser.add_argument("--sources", help=f"comma-separated subset of: {','.join(config.SOURCES)} "
                                          "(default: the track's own sources)")
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cli()
