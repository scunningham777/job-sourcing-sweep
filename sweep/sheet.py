"""Google Sheet I/O. Reads the tracker for dedup; appends results to the inbox tab.

Works in three modes, best available first:
  1. Service account JSON present  -> read the tracker + inbox tabs live, append to the inbox
  2. data/existing.csv present      -> dedup against that export; results go to CSV only
  3. Neither                        -> no sheet dedup (warns); results go to CSV only
"""

import csv
import os
from pathlib import Path

from . import config
from .dedup import normalize_company, normalize_url

INBOX_HEADER = ["Date found", "Verdict", "Company", "Title", "link", "Location", "Salary", "Notes", "Source"]


def _open_spreadsheet():
    import gspread  # imported lazily so dry runs work before Google setup

    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise SystemExit("Set SPREADSHEET_ID in .env (the long ID in the sheet's URL) to use the sheet.")
    client = gspread.service_account(filename=str(config.SERVICE_ACCOUNT_FILE))
    return client.open_by_key(spreadsheet_id)


def _index_rows(rows: list[list[str]], company_col: int, link_col: int, note_col: int,
                urls: set[str], companies: dict[str, str]) -> None:
    for row in rows[1:]:  # skip header
        cells = [c.strip() for c in row]
        if sum(1 for c in cells if c) < 2:  # blank rows and section labels like "PRIORITY"
            continue
        company = cells[company_col] if len(cells) > company_col else ""
        link = cells[link_col] if len(cells) > link_col else ""
        note = cells[note_col] if len(cells) > note_col else ""
        for url in (u for u in link.split(";") if u.strip()):  # some cells hold "url1 ; url2"
            urls.add(normalize_url(url))
        if company:
            companies.setdefault(normalize_company(company), note[:160])


def load_existing() -> tuple[set[str], dict[str, str], str]:
    """Return (known posting URLs, {company: status snippet}, mode description)."""
    urls: set[str] = set()
    companies: dict[str, str] = {}

    layout = config.sheet_settings()
    tracker_cols = (layout.company_col, layout.link_col, layout.notes_col)

    if config.SERVICE_ACCOUNT_FILE.exists():
        import gspread

        sh = _open_spreadsheet()
        _index_rows(sh.worksheet(layout.tracker_tab).get_all_values(), *tracker_cols, urls, companies)
        try:
            # The inbox is ours, so its columns are fixed by INBOX_HEADER.
            _index_rows(sh.worksheet(layout.inbox_tab).get_all_values(), 2, 4, 7, urls, companies)
        except gspread.exceptions.WorksheetNotFound:
            pass
        return urls, companies, "live sheet"

    if config.EXISTING_CSV.exists():
        with open(config.EXISTING_CSV, newline="", encoding="utf-8-sig") as f:
            _index_rows(list(csv.reader(f)), *tracker_cols, urls, companies)
        return urls, companies, f"CSV export ({config.EXISTING_CSV.name})"

    return urls, companies, "none – no sheet dedup (add a service account or data/existing.csv)"


def append_inbox(rows: list[list[str]]) -> None:
    import gspread

    inbox_tab = config.sheet_settings().inbox_tab
    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet(inbox_tab)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=inbox_tab, rows=1000, cols=len(INBOX_HEADER))
        ws.append_row(INBOX_HEADER)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_csv(rows: list[list[str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # -sig so Excel reads UTF-8
        writer = csv.writer(f)
        writer.writerow(INBOX_HEADER)
        writer.writerows(rows)
