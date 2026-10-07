"""Orchestrator screening pass: Opus judges every deduped lead against your criteria file."""

import json
from pathlib import Path

from anthropic import AsyncAnthropic

from . import config
from .costs import CostTracker
from .dedup import tracked_status
from .models import JobLead, ScreeningReport, Verdict

SCREEN_SYSTEM = """\
You screen job leads for one candidate against their written criteria (below). For each lead \
return a verdict:
- priority: passes every hard filter cleanly and has strong positive signals
- candidate: passes hard filters, or fails one only on missing info that screening could confirm
- exclude: clearly fails a hard filter

Tie every reason to a specific criterion. Where the lead is missing information (no posted \
salary, ambiguous remote policy), say exactly what to confirm rather than guessing. If the \
company is already tracked, say so and how this role differs. If a lead has \
`liveness_unconfirmed`, an automated check couldn't confirm the posting is still open – say so \
in the notes so the candidate checks before applying. Write `notes` in the same voice \
and density as the candidate's tracker: role, remote policy, comp, stack fit, company \
stability, notable flags.

<criteria>
{criteria}
</criteria>"""


async def screen_leads(
    client: AsyncAnthropic,
    leads: list[JobLead],
    known_companies: dict[str, str],
    criteria: str,
    costs: CostTracker,
    raw_output_path: Path | None = None,
    unconfirmed: dict[str, str] | None = None,
) -> dict[str, Verdict]:
    """Return {lead url: verdict}. Leads are keyed by a small integer id in the prompt – models
    copy short ids reliably, while long URLs can come back subtly altered and silently not match."""
    payload = []
    for i, lead in enumerate(leads):
        item = {"id": i, **lead.model_dump()}
        status = tracked_status(lead, known_companies)
        if status:
            item["already_tracked_company_status"] = status
        if unconfirmed and lead.url in unconfirmed:
            item["liveness_unconfirmed"] = unconfirmed[lead.url]
        payload.append(item)

    response = await client.beta.messages.parse(
        model=config.SCREEN_MODEL,
        max_tokens=16000,
        system=SCREEN_SYSTEM.format(criteria=criteria),
        thinking={"type": "adaptive"},
        # If a safety classifier ever declines, the API re-runs on a recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_format=ScreeningReport,
        messages=[{
            "role": "user",
            "content": (
                f"Screen all {len(leads)} leads below – return exactly one verdict per lead id "
                f"(0 to {len(leads) - 1}).\n\n{json.dumps(payload, indent=2)}"
            ),
        }],
    )
    costs.add("screen", response.model, response.usage)
    if raw_output_path:
        raw_output_path.write_text(response.model_dump_json(indent=2), encoding="utf-8")

    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Screening returned no result (stop_reason={response.stop_reason})")

    verdicts = {
        leads[v.lead_id].url: v
        for v in response.parsed_output.verdicts
        if 0 <= v.lead_id < len(leads)
    }
    missing = len(leads) - len(verdicts)
    if missing:
        print(f"  ! screening skipped {missing} lead(s) (stop_reason={response.stop_reason}); "
              "they'll be marked 'unscreened' – see screening.json")
    return verdicts
