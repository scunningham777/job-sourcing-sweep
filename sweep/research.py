"""Fan-out workers: one Sonnet agent per source, using server-side web tools plus our own
job-board API tools.

Two phases per source:
  1. research – an agent loop writes a findings dossier (free text) using two kinds of tool:
                server tools  web_search + web_fetch – Anthropic runs them inside the request
                client tools  list_company_jobs + get_job_posting – the model asks, *we* run them
                              (sweep/ats.py) and send the results back
  2. extract  – a structured-output call turns that dossier into validated JobLead objects

Keeping them separate means the research prompt can think freely, and the extraction step
is a small, cheap, schema-guaranteed call you can re-run without re-searching.
"""

import asyncio
import json
import urllib.error
from pathlib import Path

from anthropic import AsyncAnthropic

from . import ats, config
from .costs import CostTracker
from .models import ExtractedLeads, JobLead

RESEARCH_SYSTEM = """\
You are a job-sourcing researcher. Find currently open postings that match the candidate profile \
on the assigned source, and verify each one by opening the actual posting.

For every posting you keep, record: company, exact title, direct posting URL, location / remote \
policy as written, posted salary range (or say none is posted), main stack, how much backend work \
is required, and the posting date if visible.

Rules:
- Open the posting itself before including it; skip anything you could not verify is still open.
- Prefer the company's own ATS link (Ashby, Greenhouse, Lever, Workday, etc.) over aggregator copies.
- Quality over volume: 5–12 solid matches beats 30 loose ones.
- If a role is close but fails something (hybrid, low pay), still list it and say what fails – \
screening happens later.
- Finish with the findings list only, no preamble.

Job-board API tools: for any company board on Ashby, Greenhouse, Lever, or Workday, call \
list_company_jobs with the board URL and title keywords instead of fetching the page, then \
get_job_posting on the promising postings. They read the ATS's own JSON API, so they work on \
JavaScript-rendered boards, include the posted pay range when there is one, and don't count \
against your fetch budget (they have their own budget of {board_calls} calls). A posting that \
get_job_posting returns as open counts as opened and verified. Use web_search to discover \
companies and their board URLs; use web_fetch for pages on other sites.

Tool budget: at most {searches} web searches and {fetches} page fetches for this whole task. \
Plan them – a few broad searches built from the profile's titles, stack, and location (e.g. \
"senior frontend engineer remote", "angular developer contract {city}"), then spend fetches \
opening the most promising postings. web_fetch can only open \
URLs that already appeared in search results or fetched pages. Every search counts against the \
budget even if it repeats an earlier query – if a script fails, rerun only the calls that did \
not complete, never searches whose results you already have. If a tool returns \
max_uses_exceeded, that budget is permanently spent: do not wait, sleep, or retry – write up \
the postings you have already verified."""


# --- Client tools ---------------------------------------------------------------------------
# Unlike web_search/web_fetch, Anthropic never runs these. The model replies with a tool_use
# block (stop_reason "tool_use"), we run the function here, and send back a tool_result.
# The description is the tool's whole user manual – it's all the model reads to decide when
# and how to call it. `strict` makes the API guarantee the inputs match the schema.

BOARD_TOOLS = [
    {
        "name": "list_company_jobs",
        "description": (
            "List a company's open postings straight from its applicant-tracking system's public "
            "job API (Ashby, Greenhouse, Lever, or Workday). Use this instead of web_fetch for any "
            "board on jobs.ashbyhq.com, job-boards.greenhouse.io / boards.greenhouse.io, "
            "jobs.lever.co, or *.myworkdayjobs.com – those pages are often rendered by JavaScript "
            "and fetch as empty. Returns, for each posting whose title matches any keyword: title, "
            "URL, location, workplace type when stated, salary when the board lists it, and posted "
            "date. No descriptions – call get_job_posting for those. Workday sites are searched "
            "server-side, which also matches descriptions, so title matches are listed first. An "
            "unknown company slug returns an error; find the real board URL with web_search rather "
            "than guessing slugs."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "board_url": {
                    "type": "string",
                    "description": "The company's board URL, or any posting URL on it. E.g. "
                                   "https://jobs.ashbyhq.com/ramp, https://job-boards.greenhouse.io/reddit, "
                                   "https://jobs.lever.co/palantir, https://acme.wd5.myworkdayjobs.com/External",
                },
                "title_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keep postings whose title contains any of these – case-insensitive, "
                                   "spaces and hyphens ignored, so 'frontend' also matches 'Front-End'. "
                                   "E.g. [\"frontend\", \"front end\", \"ui engineer\", \"angular\"]. "
                                   "An empty list returns every posting (capped).",
                },
            },
            "required": ["board_url", "title_keywords"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_job_posting",
        "description": (
            "Get one posting's full details from its ATS job API: description, location, workplace "
            "type, employment type, and posted pay range when available. Works for posting URLs on "
            "Ashby, Greenhouse, Lever, and Workday. status 'open' means the board lists it right "
            "now – that counts as verified. status 'closed' means the board no longer lists it."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "posting_url": {"type": "string", "description": "The posting's own URL on the ATS."},
            },
            "required": ["posting_url"],
            "additionalProperties": False,
        },
    },
]
BOARD_TOOL_NAMES = {t["name"] for t in BOARD_TOOLS}


def run_board_tool(name: str, tool_input: dict) -> dict:
    """Execute one client tool call. Blocking (plain HTTP), so call it in a thread. Returns the
    JSON-able result; raises ats.BoardError for problems the model should see as an error."""
    if name == "list_company_jobs":
        postings, total = ats.list_board(tool_input["board_url"], tool_input.get("title_keywords") or [])
        shown = postings[:config.BOARD_LIST_LIMIT]
        result: dict = {"matching": len(postings), "postings": [p.to_dict() for p in shown]}
        if total is not None:
            result["total_on_board"] = total
        if len(postings) > len(shown):
            result["note"] = (f"showing the first {len(shown)} of {len(postings)} matches – "
                              "narrow title_keywords to see the rest")
        return result
    if name == "get_job_posting":
        posting = ats.get_posting(tool_input["posting_url"])
        if posting is None:
            return {"status": "closed", "posting_url": tool_input["posting_url"]}
        return {"status": "open", **posting.to_dict(description_chars=config.POSTING_DESCRIPTION_CHARS)}
    raise ats.BoardError(f"Unknown tool {name}")


async def tool_result(block, calls_so_far: int) -> dict:
    """Run one tool_use block and wrap the outcome as a tool_result content block. Failures go
    back to the model with is_error set, so it can adapt instead of the whole worker crashing."""
    if calls_so_far >= config.MAX_BOARD_CALLS_PER_SOURCE:
        content, is_error = ("max_uses_exceeded: the job-board API budget for this task is spent. "
                             "Do not retry – write up the postings you have already verified."), True
    else:
        try:
            content = json.dumps(await asyncio.to_thread(run_board_tool, block.name, block.input),
                                 ensure_ascii=False)
            is_error = False
        except ats.BoardError as err:
            content, is_error = str(err), True
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            content, is_error = f"Request failed ({type(err).__name__}) – the board API may be down", True
    result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
    if is_error:
        result["is_error"] = True
    return result


def summarize_tool_activity(blocks: list) -> dict:
    """Count tool calls and error codes in a transcript – the first thing to check when a
    worker comes back empty. `blocks` mixes response content blocks (objects) with the
    tool_result dicts we sent back for client tools."""
    summary: dict = {"web_search": 0, "web_fetch": 0, "board_api": 0, "errors": {}}
    for b in blocks:
        if isinstance(b, dict):  # our own tool_result for a board-API call
            if b.get("is_error"):
                summary["errors"]["board_api_error"] = summary["errors"].get("board_api_error", 0) + 1
        elif b.type == "server_tool_use" and b.name in ("web_search", "web_fetch"):
            summary[b.name] += 1
        elif b.type == "tool_use" and b.name in BOARD_TOOL_NAMES:
            summary["board_api"] += 1
        elif b.type in ("web_search_tool_result", "web_fetch_tool_result"):
            code = getattr(b.content, "error_code", None)  # errors are one object, not a list
            if code:
                summary["errors"][code] = summary["errors"].get(code, 0) + 1
    return summary


def _tools(source: config.Source) -> list[dict]:
    search = {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": config.MAX_SEARCHES_PER_SOURCE,
        "user_location": {"type": "approximate", **config.search_location()},
    }
    if source.search_domains:
        search["allowed_domains"] = source.search_domains
    fetch = {
        "type": "web_fetch_20260209",
        "name": "web_fetch",
        "max_uses": config.MAX_FETCHES_PER_SOURCE,
        "max_content_tokens": config.MAX_FETCH_TOKENS,
    }
    # Server and client tools share one tools array – the model doesn't know who runs which.
    return [search, fetch, *BOARD_TOOLS]


async def research_source(
    client: AsyncAnthropic, source: config.Source, search_profile: str, costs: CostTracker,
    transcript_path: Path | None = None,
) -> str:
    """Run the agent loop for one source and return its findings as text."""
    system = RESEARCH_SYSTEM.format(
        searches=config.MAX_SEARCHES_PER_SOURCE, fetches=config.MAX_FETCHES_PER_SOURCE,
        board_calls=config.MAX_BOARD_CALLS_PER_SOURCE, city=config.search_location()["city"].lower(),
    )
    hint = " ".join(filter(None, [
        source.hint, config.search_settings().get("source_notes", {}).get(source.key, "").strip()
    ]))
    user_msg = (
        f"Source: {source.name}\n{hint}\n\n"
        f"Candidate profile:\n{search_profile}"
    )
    messages: list[dict] = [{"role": "user", "content": user_msg}]
    transcript: list = []  # every assistant block plus our tool_results, for debugging
    pauses = board_calls = 0

    for _ in range(config.MAX_AGENT_TURNS):
        response = await client.messages.create(
            model=config.WORKER_MODEL,
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},  # workers read a lot; medium keeps cost sane – tune later
            tools=_tools(source),
            messages=messages,
        )
        costs.add(f"research:{source.key}", config.WORKER_MODEL, response.usage)
        transcript.extend(response.content)
        # A reply after pause_turn continues the same assistant turn, so merge it into that
        # message rather than adding a second assistant message in a row.
        if messages[-1]["role"] == "assistant":
            messages[-1]["content"].extend(response.content)
        else:
            messages.append({"role": "assistant", "content": list(response.content)})

        if response.stop_reason == "pause_turn":
            # The server-side tool loop hit its iteration cap. Re-send as-is (no "continue"
            # message) and the server resumes where it left off.
            pauses += 1
            if pauses > config.MAX_PAUSE_CONTINUATIONS:
                print(f"  ! {source.key}: still paused after {config.MAX_PAUSE_CONTINUATIONS} continuations")
                break
            continue
        if response.stop_reason == "tool_use":
            # The model called our client tools. Run every tool_use block (in parallel), then reply
            # with a user message holding only the matching tool_result blocks. Any server tool
            # blocks in the same response were already run by Anthropic – leave them alone.
            calls = [b for b in response.content if b.type == "tool_use"]
            results = await asyncio.gather(*(tool_result(b, board_calls + i) for i, b in enumerate(calls)))
            board_calls += len(calls)
            transcript.extend(results)
            messages.append({"role": "user", "content": list(results)})
            continue
        if response.stop_reason == "refusal":
            raise RuntimeError(f"{source.key}: model declined the research request")
        if response.stop_reason == "max_tokens":
            print(f"  ! {source.key}: hit max_tokens – findings may be truncated")
        break
    else:
        print(f"  ! {source.key}: stopped after {config.MAX_AGENT_TURNS} turns – findings may be incomplete")

    activity = summarize_tool_activity(transcript)
    errors = ", ".join(f"{code} ×{n}" for code, n in activity["errors"].items()) or "none"
    print(f"    {source.key}: {activity['web_search']} searches, {activity['web_fetch']} fetches, "
          f"{activity['board_api']} board-API calls, tool errors: {errors}")
    if transcript_path:
        transcript_path.write_text(json.dumps(
            [b if isinstance(b, dict) else b.model_dump(mode="json") for b in transcript],
            indent=2, ensure_ascii=False), encoding="utf-8")

    # The write-up is in the text blocks; tool calls/results and thinking are other block types.
    return "\n".join(b.text for b in transcript if not isinstance(b, dict) and b.type == "text").strip()


async def extract_leads(
    client: AsyncAnthropic, source: config.Source, findings: str, costs: CostTracker
) -> list[JobLead]:
    """Structured extraction: free-text findings -> validated JobLead list."""
    if not findings:
        return []
    response = await client.messages.parse(
        model=config.WORKER_MODEL,
        max_tokens=16000,
        output_format=ExtractedLeads,
        messages=[{
            "role": "user",
            "content": (
                "Convert these job-sourcing findings into structured leads. Include every posting "
                "listed with its own direct URL. Skip items the findings mark as closed, seen only "
                "in search results, or not opened, and items whose only link is a search or listing "
                "page. Use null for anything not stated – do not guess salaries.\n\n"
                f"<findings source=\"{source.key}\">\n{findings}\n</findings>"
            ),
        }],
    )
    costs.add(f"extract:{source.key}", config.WORKER_MODEL, response.usage)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        print(f"  ! {source.key}: extraction returned nothing (stop_reason={response.stop_reason})")
        return []
    return response.parsed_output.leads
