"""Fan-out workers: one Sonnet agent per source, using Anthropic's server-side web tools.

Two phases per source:
  1. research – an agent loop with web_search + web_fetch writes a findings dossier (free text)
  2. extract  – a structured-output call turns that dossier into validated JobLead objects

Keeping them separate means the research prompt can think freely, and the extraction step
is a small, cheap, schema-guaranteed call you can re-run without re-searching.
"""

import json
from pathlib import Path

from anthropic import AsyncAnthropic

from . import config
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

Tool budget: at most {searches} web searches and {fetches} page fetches for this whole task. \
Plan them – a few broad searches built from the profile's titles, stack, and location (e.g. \
"senior frontend engineer remote", "angular developer contract {city}"), then spend fetches \
opening the most promising postings. web_fetch can only open \
URLs that already appeared in search results or fetched pages. Every search counts against the \
budget even if it repeats an earlier query – if a script fails, rerun only the calls that did \
not complete, never searches whose results you already have. If a tool returns \
max_uses_exceeded, that budget is permanently spent: do not wait, sleep, or retry – write up \
the postings you have already verified."""


def summarize_tool_activity(blocks: list) -> dict:
    """Count server tool calls and error codes in a transcript – the first thing to check
    when a worker comes back empty."""
    summary: dict = {"web_search": 0, "web_fetch": 0, "errors": {}}
    for b in blocks:
        if b.type == "server_tool_use" and b.name in ("web_search", "web_fetch"):
            summary[b.name] += 1
        elif b.type in ("web_search_tool_result", "web_fetch_tool_result"):
            code = getattr(b.content, "error_code", None)  # errors are one object, not a list
            if code:
                summary["errors"][code] = summary["errors"].get(code, 0) + 1
    return summary


def _web_tools(source: config.Source) -> list[dict]:
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
    return [search, fetch]


async def research_source(
    client: AsyncAnthropic, source: config.Source, search_profile: str, costs: CostTracker,
    transcript_path: Path | None = None,
) -> str:
    """Run the web-tool agent loop for one source and return its findings as text."""
    system = RESEARCH_SYSTEM.format(
        searches=config.MAX_SEARCHES_PER_SOURCE, fetches=config.MAX_FETCHES_PER_SOURCE,
        city=config.search_location()["city"].lower(),
    )
    hint = " ".join(filter(None, [
        source.hint, config.search_settings().get("source_notes", {}).get(source.key, "").strip()
    ]))
    user_msg = (
        f"Source: {source.name}\n{hint}\n\n"
        f"Candidate profile:\n{search_profile}"
    )
    messages: list[dict] = [{"role": "user", "content": user_msg}]
    assistant_blocks: list = []  # accumulated across pause_turn continuations

    for _ in range(config.MAX_PAUSE_CONTINUATIONS + 1):
        response = await client.messages.create(
            model=config.WORKER_MODEL,
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},  # workers read a lot; medium keeps cost sane – tune later
            tools=_web_tools(source),
            messages=messages,
        )
        costs.add(f"research:{source.key}", config.WORKER_MODEL, response.usage)
        assistant_blocks.extend(response.content)

        if response.stop_reason == "pause_turn":
            # The server-side tool loop hit its iteration cap. Re-send [user, assistant-so-far]
            # (no "continue" message) and the server resumes where it left off.
            messages = [messages[0], {"role": "assistant", "content": assistant_blocks}]
            continue
        if response.stop_reason == "refusal":
            raise RuntimeError(f"{source.key}: model declined the research request")
        if response.stop_reason == "max_tokens":
            print(f"  ! {source.key}: hit max_tokens – findings may be truncated")
        break
    else:
        print(f"  ! {source.key}: still paused after {config.MAX_PAUSE_CONTINUATIONS} continuations")

    activity = summarize_tool_activity(assistant_blocks)
    errors = ", ".join(f"{code} ×{n}" for code, n in activity["errors"].items()) or "none"
    print(f"    {source.key}: {activity['web_search']} searches, {activity['web_fetch']} fetches, "
          f"tool errors: {errors}")
    if transcript_path:
        transcript_path.write_text(
            json.dumps([b.model_dump(mode="json") for b in assistant_blocks], indent=2), encoding="utf-8"
        )

    # The write-up is in the text blocks; tool calls/results and thinking are other block types.
    return "\n".join(b.text for b in assistant_blocks if b.type == "text").strip()


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
                "listed with its own direct URL. Skip items the findings mark as seen only in "
                "search results or not opened, and items whose only link is a search or listing "
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
