"""Pydantic schemas – these double as the structured-output contracts sent to Claude."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class JobLead(BaseModel):
    company: str
    title: str
    url: str = Field(description="Direct link to the posting, preferably the company's own ATS page")
    location: str = Field(description="Location text as written in the posting")
    remote: Literal["remote_us", "remote_global", "hybrid", "onsite", "unclear"]
    salary_min: Optional[int] = Field(None, description="Base salary lower bound in USD, if posted")
    salary_max: Optional[int] = Field(None, description="Base salary upper bound in USD, if posted")
    salary_text: Optional[str] = Field(None, description="Salary as written, or where an estimate came from")
    stack: list[str] = Field(default_factory=list, description="Main technologies named in the posting")
    backend_requirement: Literal["none", "light", "heavy", "unclear"]
    posted: Optional[str] = Field(None, description="Posting date or recency as stated, if visible")
    summary: str = Field(description="Two or three sentences: team, scope, anything notable")


class ExtractedLeads(BaseModel):
    leads: list[JobLead]


class Verdict(BaseModel):
    lead_id: int = Field(description="The `id` of the lead being judged")
    verdict: Literal["priority", "candidate", "exclude"]
    reasons: list[str] = Field(description="Short reasons, each tied to a specific criterion")
    notes: str = Field(description="One paragraph in the style of the tracker's status/notes column")


class ScreeningReport(BaseModel):
    verdicts: list[Verdict]
