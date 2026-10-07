"""Per-source funnel for one run – found → dedup → liveness → screening – printed as a small
table and appended to output/source-stats.csv, so source quality can be compared across runs."""

import csv
from pathlib import Path

from .models import JobLead

COLUMNS = ["found", "duplicate", "not_posting", "closed", "unconfirmed", "live",
           "priority", "candidate", "exclude"]
LOG_HEADER = ["date", "track", "source", "status", *COLUMNS]


def source_funnel(results: dict[str, list[JobLead] | None], dropped: list[JobLead],
                  liveness_states: dict[str, str], verdicts: dict[str, str]) -> dict[str, dict]:
    """Counts per source. `results` maps source key → its leads (None if the source failed);
    `liveness_states` and `verdicts` are keyed by URL. Leads are matched by identity, so a URL
    two sources both found is counted once for each."""
    dropped_ids = {id(lead) for lead in dropped}
    funnel = {}
    for key, leads in results.items():
        row = dict.fromkeys(COLUMNS, 0)
        for lead in leads or []:
            row["found"] += 1
            if id(lead) in dropped_ids:
                row["duplicate"] += 1
                continue
            row[liveness_states.get(lead.url, "unconfirmed").replace("unknown", "unconfirmed")] += 1
            if verdicts.get(lead.url) in ("priority", "candidate", "exclude"):
                row[verdicts[lead.url]] += 1
        funnel[key] = {"status": "failed" if leads is None else "ok", **row}
    return funnel


def format_table(funnel: dict[str, dict]) -> str:
    labels = [c.replace("_", " ") for c in COLUMNS]
    width = max([len("source"), *map(len, funnel)])
    lines = ["source".ljust(width) + "".join(f"  {label}" for label in labels)]
    for key, row in funnel.items():
        cells = ("  failed" if row["status"] == "failed"
                 else "".join(f"  {row[c]:>{len(label)}}" for c, label in zip(COLUMNS, labels)))
        lines.append(key.ljust(width) + cells)
    return "\n".join(lines)


def append_log(funnel: dict[str, dict], date: str, track: str, path: Path) -> None:
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(LOG_HEADER)
        for key, row in funnel.items():
            writer.writerow([date, track, key, row["status"], *(row[c] for c in COLUMNS)])
