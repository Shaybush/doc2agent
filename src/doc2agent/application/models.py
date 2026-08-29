from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProcessingResult(BaseModel):
    """Safe, browser-ready artifacts from one completed pipeline run."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    document_id: str
    source_id: str
    status: str
    markdown: str | None = None
    published_copilot_markdown: str | None = None
    document_json: dict[str, Any]
    validation_errors: list[str] = Field(default_factory=list)
    security_findings: int = 0


class ProofEvidence(BaseModel):
    support: str
    section_path: list[str]
    start_line: int
    end_line: int
    source_text: str


class ProofFact(BaseModel):
    kind: str
    value: str
    evidence: list[ProofEvidence]


class CandidateProof(BaseModel):
    candidate_sha256: str
    reviewed_block_ids: list[str]
    facts: list[ProofFact]
