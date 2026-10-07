"""Offline tests – no API calls, no cost.  Run: python -m unittest"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sweep import config, sheet
from sweep.dedup import dedup_leads, normalize_company, normalize_title, normalize_url
from sweep.models import JobLead


def lead(company="Acme", title="Senior Frontend Engineer", url="https://jobs.ashbyhq.com/acme/1"):
    return JobLead(company=company, title=title, url=url, location="Remote US", remote="remote_us",
                   backend_requirement="none", summary="x")


class NormalizeTests(unittest.TestCase):
    def test_url_ignores_scheme_query_slash_and_old_greenhouse_host(self):
        self.assertEqual(
            normalize_url("https://boards.greenhouse.io/reddit/jobs/8198130/?gh_src=abc"),
            normalize_url("http://job-boards.greenhouse.io/reddit/jobs/8198130"),
        )

    def test_company_suffixes(self):
        self.assertEqual(normalize_company("Vercel, Inc."), normalize_company("vercel"))

    def test_title_variants(self):
        self.assertEqual(normalize_title("Sr. Front-End Engineer"), normalize_title("Senior Frontend Engineer"))


class DedupTests(unittest.TestCase):
    def test_drops_known_url(self):
        kept, dropped = dedup_leads([lead()], {normalize_url("https://jobs.ashbyhq.com/acme/1/")})
        self.assertEqual((len(kept), dropped[0][1]), (0, "already in sheet"))

    def test_drops_same_role_on_two_boards(self):
        a = lead(url="https://jobs.ashbyhq.com/acme/1")
        b = lead(company="Acme Inc", title="Sr Front End Engineer", url="https://builtin.com/job/acme/99")
        kept, dropped = dedup_leads([a, b], set())
        self.assertEqual((kept, dropped[0][1]), ([a], "duplicate within this sweep"))

    def test_keeps_new_role_at_tracked_company(self):
        kept, _ = dedup_leads([lead(title="Staff Frontend Engineer")], set())
        self.assertEqual(len(kept), 1)


class SheetIndexTests(unittest.TestCase):
    def test_csv_export_matches_tracker_layout(self):
        rows = [
            ["Company", "link", "application date", "status/notes"],
            ["Reddit", "https://job-boards.greenhouse.io/reddit/jobs/8198130", "9/21", "SUBMITTED"],
            ["PRIORITY"],  # section label – must be ignored
            ["Calendly", "https://x.io/a ; https://x.io/b", "", "PAUSED: backend"],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            layout = config.SheetSettings("Tracker", "Sweep inbox", company_col=0, link_col=1, notes_col=3)
            with mock.patch.object(config, "SERVICE_ACCOUNT_FILE", Path(tmp) / "none.json"), \
                 mock.patch.object(config, "EXISTING_CSV", path), \
                 mock.patch.object(config, "sheet_settings", return_value=layout):
                urls, companies, mode = sheet.load_existing()
        self.assertIn(normalize_url("https://boards.greenhouse.io/reddit/jobs/8198130"), urls)
        self.assertIn("x.io/b", urls)
        self.assertEqual(companies["calendly"], "PAUSED: backend")
        self.assertNotIn("priority", companies)
        self.assertTrue(mode.startswith("CSV"))


class ToolActivityTests(unittest.TestCase):
    def test_counts_calls_and_error_codes(self):
        from types import SimpleNamespace as NS
        from sweep.research import summarize_tool_activity

        blocks = [
            NS(type="server_tool_use", name="web_search"),
            NS(type="web_search_tool_result", content=[NS(type="web_search_result")]),  # success = list
            NS(type="server_tool_use", name="web_search"),
            NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error",
                                                         error_code="max_uses_exceeded")),
            NS(type="server_tool_use", name="web_fetch"),
            NS(type="text", text="done"),
        ]
        self.assertEqual(summarize_tool_activity(blocks),
                         {"web_search": 2, "web_fetch": 1, "errors": {"max_uses_exceeded": 1}})


if __name__ == "__main__":
    unittest.main()


def use_settings(path: Path):
    """Point config at a settings file for one test, bypassing the cached search.toml."""
    config.search_settings.cache_clear()
    patcher = mock.patch.object(config, "SEARCH_SETTINGS_FILE", path)
    patcher.start()
    return lambda: (patcher.stop(), config.search_settings.cache_clear())


class SettingsTests(unittest.TestCase):
    """Runs against search.example.toml, so the shipped template stays valid."""

    def setUp(self):
        self.addCleanup(use_settings(config.PROJECT_ROOT / "search.example.toml"))

    def test_example_tracks_load_with_existing_criteria_files(self):
        tracks = config.tracks()
        self.assertIn(config.default_track(), tracks)
        for track in tracks.values():
            self.assertTrue(track.search_profile, track.key)
            self.assertTrue(track.criteria_path.is_file(), track.criteria_path)

    def test_example_sheet_layout(self):
        layout = config.sheet_settings()
        self.assertEqual((layout.company_col, layout.link_col, layout.notes_col), (0, 1, 2))

    def test_column_letters(self):
        self.assertEqual([config._column_index(c) for c in ("A", "d", "Z", "AA")], [0, 3, 25, 26])

    def test_bad_settings_fail_with_a_message(self):
        cases = {
            'unknown source': '[tracks.x]\ncriteria_file = "x.md"\nsearch_profile = "x"\nsources = ["nope"]',
            'missing field': '[tracks.x]\nsources = ["dice"]',
            'bad default': 'default_track = "y"\n[tracks.x]\ncriteria_file = "x.md"\n'
                           'search_profile = "x"\nsources = ["dice"]',
            'no tracks': '[location]\ncity = "x"',
        }
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "search.toml"
                path.write_text(text, encoding="utf-8")
                restore = use_settings(path)
                try:
                    with self.assertRaises(SystemExit):
                        config.default_track()
                finally:
                    restore()


class LivenessTests(unittest.TestCase):
    """classify() with canned responses – no network."""

    @staticmethod
    def fake(status=200, body="<h1>Senior Angular Developer</h1>", final_url=None):
        from sweep.liveness import _Response
        return lambda url: _Response(status, final_url or url, body)

    def classify(self, url, fetch):
        from sweep.liveness import classify
        return classify(url, fetch)

    def test_gone_status_is_closed_even_with_full_description(self):
        result = self.classify("https://www.dice.com/job-detail/abc", self.fake(410))
        self.assertEqual((result.state, result.reason), ("closed", "HTTP 410"))

    def test_closed_text_on_a_200_page(self):
        body = "<p>Sorry, this job is no longer available. Similar jobs below.</p>"
        self.assertEqual(self.classify("https://x.com/job/1", self.fake(body=body)).state, "closed")

    def test_greenhouse_error_redirect(self):
        fetch = self.fake(final_url="https://job-boards.greenhouse.io/acme?error=true")
        self.assertEqual(self.classify("https://job-boards.greenhouse.io/acme/jobs/1", fetch).state, "closed")

    def test_open_page_is_live(self):
        self.assertEqual(self.classify("https://jobs.lever.co/acme/1", self.fake()).state, "live")

    def test_blocked_is_unknown_not_closed(self):
        self.assertEqual(self.classify("https://x.com/job/1", self.fake(403)).state, "unknown")

    def test_js_rendered_host_is_unknown(self):
        url = "https://acme.wd5.myworkdayjobs.com/External/job/Springfield/UI-Developer_R1"
        self.assertEqual(self.classify(url, self.fake()).state, "unknown")

    def test_search_and_listing_pages_are_not_postings(self):
        never_fetch = lambda url: self.fail(f"should not fetch {url}")  # rejected from the URL alone
        for url in [
            "https://www.dice.com/jobs/q-angular-l-nevada-jobs",          # real ones from the Oct 5 run
            "https://www.dice.com/jobs/q-AngularJS+Developer-l-Springfield,+IL-jobs",
            "https://acme.wd5.myworkdayjobs.com/en-US/External",          # Workday board, no /job/
            "https://job-boards.greenhouse.io/acme",                      # Greenhouse board
            "https://jobs.ashbyhq.com/acme",                              # Ashby board
            "https://www.indeed.com/jobs?q=angular&l=Springfield",          # generic ?q= search
            "https://example.com/careers/search",
            "",                                                           # extraction gave no URL
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.classify(url, never_fetch).state, "not_a_posting")

    def test_real_posting_urls_pass_the_shape_check(self):
        from sweep.liveness import posting_url_problem
        for url in [
            "https://www.dice.com/job-detail/0aed1a4c-9f61-4886-b5d8-d1f010684942",
            "https://icf.wd5.myworkdayjobs.com/en-US/ICFExternal_Career_Site/job/Reston-VA/Senior-Front-End-Angular-Developer_R2600968",
            "https://alight.wd5.myworkdayjobs.com/careers/job/us-il-illinois-virtual/software-engineer_r-36272",
            "https://motionrecruitment.com/tech-jobs/palatine/direct-hire/angular-engineer/882629",
            "https://job-boards.greenhouse.io/reddit/jobs/8198130",
            "https://jobs.lever.co/acme/1",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(posting_url_problem(url))

    def test_ashby_uses_board_api(self):
        board = self.fake(body='{"jobs": [{"id": "live-id"}]}')
        self.assertEqual(self.classify("https://jobs.ashbyhq.com/acme/live-id", board).state, "live")
        self.assertEqual(self.classify("https://jobs.ashbyhq.com/acme/gone-id", board).state, "closed")
