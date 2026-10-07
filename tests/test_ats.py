"""Offline tests for the job-board API client and the client-tool loop – no network, no cost.

The canned JSON below is trimmed from real responses (Oct 2026), so a field rename on an ATS's
side shows up as a failing live smoke test, not a silent change here."""

import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from sweep import ats, config, research
from sweep.ats import BoardRef, Response, parse_url

ASHBY_BOARD = {"jobs": [
    {"id": "fe-1", "title": " Software Engineer, Frontend", "employmentType": "FullTime",
     "location": "New York, NY (HQ)", "secondaryLocations": [{"location": "Remote (US)"}],
     "publishedAt": "2026-09-21T17:12:35.753+00:00", "isListed": True, "isRemote": True,
     "workplaceType": "Hybrid", "jobUrl": "https://jobs.ashbyhq.com/acme/fe-1",
     "descriptionPlain": "Build UIs in React.",
     "compensation": {"scrapeableCompensationSalarySummary": "$200K - $310K",
                      "summaryComponents": [
                          {"compensationType": "EquityPercentage", "interval": "NONE"},
                          {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
                           "minValue": 200000, "maxValue": 310000}]}},
    {"id": "be-1", "title": "Backend Engineer", "location": "Remote", "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/acme/be-1"},
    {"id": "hidden", "title": "Frontend Engineer (internal)", "isListed": False,
     "jobUrl": "https://jobs.ashbyhq.com/acme/hidden"},
]}

GREENHOUSE_JOB = {
    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/81", "id": 81, "title": "Frontend Engineer, Ads",
    "company_name": "Acme", "location": {"name": "Remote - United States"},
    "first_published": "2026-09-11T02:19:34-04:00",
    "content": "&lt;p&gt;Ship &lt;strong&gt;Angular&lt;/strong&gt; apps.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;TypeScript&lt;/li&gt;&lt;/ul&gt;",
    "pay_input_ranges": [{"min_cents": 16420000, "max_cents": 22990000, "currency_type": "USD"}],
}

LEVER_JOB = {
    "id": "lv-1", "text": "Senior Front-End Engineer", "hostedUrl": "https://jobs.lever.co/acme/lv-1",
    "categories": {"commitment": "Full-time", "location": "Chicago, IL", "allLocations": ["Chicago, IL", "Remote"]},
    "workplaceType": "remote", "createdAt": 1786469891368,
    "descriptionPlain": "Intro.", "lists": [{"text": "What you'll do", "content": "<li>Build UI</li>"}],
    "additionalPlain": "Benefits.",
    "salaryRange": {"min": 150000, "max": 180000, "currency": "USD", "interval": "per-year-salary"},
}

WORKDAY_ROW = {"title": "UI Developer", "externalPath": "/job/Springfield-IL/UI-Developer_R1",
               "locationsText": "Springfield, IL", "postedOn": "Posted 3 Days Ago"}
WORKDAY_DETAIL = {"jobPostingInfo": {
    "title": "UI Developer", "jobDescription": "<p>Angular and <b>RxJS</b>.</p>", "location": "Springfield, IL",
    "startDate": "2026-10-04", "timeType": "Full time", "posted": True,
    "externalUrl": "https://acme.wd5.myworkdayjobs.com/External/job/Springfield-IL/UI-Developer_R1"}}


def fake_api(routes: dict):
    """A fetch() that serves JSON by URL prefix and records every call."""
    calls = []

    def fetch(url, json_body=None):
        calls.append((url, json_body))
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                status, body = payload if isinstance(payload, tuple) else (200, payload)
                return Response(status, url, body if isinstance(body, str) else json.dumps(body))
        return Response(404, url, "Not Found")

    fetch.calls = calls
    return fetch


class ParseUrlTests(unittest.TestCase):
    def test_board_and_posting_urls(self):
        cases = {
            "https://jobs.ashbyhq.com/ramp": BoardRef("ashby", "ramp"),
            "jobs.ashbyhq.com/ramp/abc-123/application": BoardRef("ashby", "ramp", job="abc-123"),
            "https://job-boards.greenhouse.io/reddit": BoardRef("greenhouse", "reddit"),
            "https://boards.greenhouse.io/reddit/jobs/8198130?gh_src=x": BoardRef("greenhouse", "reddit", job="8198130"),
            "https://boards.greenhouse.io/embed/job_board?for=acme&token=55": BoardRef("greenhouse", "acme", job="55"),
            "https://jobs.lever.co/palantir/6ed7/apply": BoardRef("lever", "palantir", job="6ed7"),
            "https://icf.wd5.myworkdayjobs.com/en-US/ICFExternal_Career_Site":
                BoardRef("workday", "icf", host="icf.wd5.myworkdayjobs.com", site="ICFExternal_Career_Site"),
            "https://acme.wd1.myworkdayjobs.com/External/job/Springfield-IL/UI-Developer_R1":
                BoardRef("workday", "acme", host="acme.wd1.myworkdayjobs.com", site="External",
                         job="/job/Springfield-IL/UI-Developer_R1"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(parse_url(url), expected)

    def test_unsupported_urls_raise_a_readable_error(self):
        for url in ["https://careers.example.com/jobs/123", "https://acme.wd5.myworkdayjobs.com/", "jobs.lever.co"]:
            with self.subTest(url=url), self.assertRaises(ats.BoardError):
                parse_url(url)


class NormalizeTests(unittest.TestCase):
    def test_ashby(self):
        postings, total = ats.list_board("jobs.ashbyhq.com/acme", ["front end"],
                                         fake_api({"https://api.ashbyhq.com/": ASHBY_BOARD}))
        self.assertEqual(total, 2)  # the unlisted job isn't counted
        [p] = postings
        self.assertEqual((p.title, p.workplace, p.salary_min, p.salary_max, p.posted),
                         ("Software Engineer, Frontend", "hybrid", 200000, 310000, "2026-09-21"))
        self.assertEqual(p.location, "New York, NY (HQ) / Remote (US)")
        self.assertEqual(p.description, "")  # listings stay small

    def test_greenhouse_unescapes_and_strips_html(self):
        fetch = fake_api({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/81": GREENHOUSE_JOB})
        p = ats.get_posting("https://job-boards.greenhouse.io/acme/jobs/81", fetch)
        self.assertEqual(p.description, "Ship Angular apps.\n\n- TypeScript")
        self.assertEqual((p.salary, p.salary_min, p.salary_max), ("$164K – $230K", 164200, 229900))
        self.assertIn("pay_transparency=true", fetch.calls[0][0])

    def test_lever(self):
        p = ats.get_posting("https://jobs.lever.co/acme/lv-1",
                            fake_api({"https://api.lever.co/v0/postings/acme/lv-1": LEVER_JOB}))
        self.assertEqual((p.workplace, p.salary, p.location, p.posted),
                         ("remote", "$150K – $180K", "Chicago, IL / Remote", "2026-08-11"))
        self.assertEqual(p.description, "Intro.\n\nWhat you'll do\n- Build UI\n\nBenefits.")

    def test_workday_detail(self):
        fetch = fake_api({"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/": WORKDAY_DETAIL})
        p = ats.get_posting("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Springfield-IL/UI-Developer_R1", fetch)
        self.assertEqual((p.title, p.posted, p.employment_type, p.description),
                         ("UI Developer", "2026-10-04", "Full time", "Angular and RxJS."))

    def test_keyword_matching_ignores_case_spaces_and_hyphens(self):
        for title in ["Senior Front-End Engineer", "FRONTEND dev", "Front End Engineer"]:
            self.assertTrue(ats._matches(title, ["frontend"]), title)
        self.assertFalse(ats._matches("Backend Engineer", ["frontend", "ui engineer"]))
        self.assertTrue(ats._matches("Anything", []))


class WorkdayListTests(unittest.TestCase):
    def test_pages_each_keyword_and_merges_duplicates(self):
        full_page = [dict(WORKDAY_ROW, externalPath=f"/job/x/Role_R{i}", title=f"Angular Dev {i}")
                     for i in range(ats.WORKDAY_PAGE_SIZE)]
        bodies = []

        def fetch(url, json_body=None):
            bodies.append(json_body)
            if json_body["searchText"] == "angular":
                rows = full_page if json_body["offset"] == 0 else [WORKDAY_ROW]
            else:  # "ui" finds one already-seen posting plus a description-only match
                rows = [WORKDAY_ROW, dict(WORKDAY_ROW, externalPath="/job/x/QA_R9", title="QA Analyst")]
            return Response(200, url, json.dumps({"total": 99, "jobPostings": rows}))

        postings, total = ats.list_board("https://acme.wd5.myworkdayjobs.com/External", ["angular", "ui"], fetch)
        self.assertIsNone(total)
        self.assertEqual([(b["searchText"], b["offset"]) for b in bodies],
                         [("angular", 0), ("angular", 20), ("ui", 0)])
        self.assertEqual(len(postings), ats.WORKDAY_PAGE_SIZE + 2)
        self.assertEqual(postings[-1].title, "QA Analyst")  # title non-matches sort last


class PostingStateTests(unittest.TestCase):
    def test_closed_and_live(self):
        fetch = fake_api({"https://api.ashbyhq.com/": ASHBY_BOARD})
        self.assertEqual(ats.posting_state("https://jobs.ashbyhq.com/acme/fe-1", fetch)[0], "live")
        self.assertEqual(ats.posting_state("https://jobs.ashbyhq.com/acme/gone", fetch)[0], "closed")
        self.assertEqual(ats.posting_state("https://jobs.lever.co/acme/gone", fetch)[0], "closed")

    def test_not_an_ats_posting(self):
        self.assertIsNone(ats.posting_state("https://careers.example.com/jobs/123", fetch=None))
        self.assertIsNone(ats.posting_state("https://jobs.lever.co/acme", fetch=None))

    def test_unknown_board_raises_instead_of_calling_it_closed(self):
        with self.assertRaises(ats.BoardError):
            ats.posting_state("https://jobs.ashbyhq.com/nope/1", fake_api({}))


class BoardToolTests(unittest.TestCase):
    """The client-tool layer in research.py, with ats.list_board / get_posting stubbed."""

    def run_tool(self, name, tool_input, calls_so_far=0):
        block = NS(type="tool_use", id="toolu_1", name=name, input=tool_input)
        return asyncio.run(research.tool_result(block, calls_so_far))

    def test_list_caps_results_and_says_so(self):
        postings = [ats.Posting("lever", "acme", f"FE {i}", f"https://jobs.lever.co/acme/{i}", "Remote")
                    for i in range(config.BOARD_LIST_LIMIT + 5)]
        with mock.patch.object(ats, "list_board", return_value=(postings, 300)):
            result = self.run_tool("list_company_jobs", {"board_url": "jobs.lever.co/acme", "title_keywords": []})
        payload = json.loads(result["content"])
        self.assertNotIn("is_error", result)
        self.assertEqual((payload["matching"], len(payload["postings"]), payload["total_on_board"]),
                         (config.BOARD_LIST_LIMIT + 5, config.BOARD_LIST_LIMIT, 300))
        self.assertIn("narrow title_keywords", payload["note"])

    def test_closed_posting(self):
        with mock.patch.object(ats, "get_posting", return_value=None):
            result = self.run_tool("get_job_posting", {"posting_url": "https://jobs.lever.co/acme/1"})
        self.assertEqual(json.loads(result["content"])["status"], "closed")

    def test_errors_go_back_to_the_model(self):
        with mock.patch.object(ats, "list_board", side_effect=ats.BoardError("lever board 'x': not found")):
            result = self.run_tool("list_company_jobs", {"board_url": "jobs.lever.co/x", "title_keywords": []})
        self.assertEqual((result["is_error"], result["tool_use_id"]), (True, "toolu_1"))
        self.assertIn("not found", result["content"])

    def test_budget(self):
        with mock.patch.object(ats, "get_posting", side_effect=AssertionError("should not be called")):
            result = self.run_tool("get_job_posting", {"posting_url": "x"},
                                   calls_so_far=config.MAX_BOARD_CALLS_PER_SOURCE)
        self.assertTrue(result["is_error"])
        self.assertIn("max_uses_exceeded", result["content"])


class FakeBlock(NS):
    def model_dump(self, mode=None):
        return dict(vars(self))


class ResearchLoopTests(unittest.TestCase):
    """research_source() against a scripted fake client: the model calls a client tool, pauses
    once mid server-tool loop, then writes its findings."""

    def test_tool_use_then_pause_then_finish(self):
        responses = [
            NS(stop_reason="tool_use", content=[
                FakeBlock(type="text", text="Checking the board."),
                FakeBlock(type="tool_use", id="toolu_1", name="list_company_jobs",
                          input={"board_url": "jobs.lever.co/acme", "title_keywords": ["frontend"]})]),
            NS(stop_reason="pause_turn", content=[FakeBlock(type="server_tool_use", name="web_search")]),
            NS(stop_reason="end_turn", content=[FakeBlock(type="text", text="1. Acme – Frontend Engineer")]),
        ]
        sent = []

        async def create(**kwargs):
            sent.append(json.loads(json.dumps(kwargs["messages"], default=lambda b: vars(b))))
            return NS(usage=None, **vars(responses[len(sent) - 1]))

        client = NS(messages=NS(create=create))
        costs = NS(add=lambda *a: None)
        source = config.Source("lever", "Lever job boards")
        with mock.patch.object(ats, "list_board", return_value=([], 3)), \
             mock.patch.object(config, "search_location", return_value={"city": "Springfield"}), \
             mock.patch.object(config, "search_settings", return_value={}), \
             mock.patch("builtins.print"):
            findings = asyncio.run(research.research_source(client, source, "frontend", costs))

        self.assertEqual(findings, "Checking the board.\n1. Acme – Frontend Engineer")
        # Turn 2: the tool_result goes back in a user message, matched by id.
        self.assertEqual([m["role"] for m in sent[1]], ["user", "assistant", "user"])
        self.assertEqual(sent[1][2]["content"][0]["tool_use_id"], "toolu_1")
        # Turn 3: after pause_turn the paused content is merged into the last assistant message
        # (never two assistant messages in a row) and no new user message is added.
        self.assertEqual([m["role"] for m in sent[2]], ["user", "assistant", "user", "assistant"])


if __name__ == "__main__":
    unittest.main()
