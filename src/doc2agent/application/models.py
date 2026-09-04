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
    block_id: str
    support: str
    section_path: list[str]
    start_line: int
    end_line: int
    source_text: str


class ProofFact(BaseModel):
    annotation_id: str
    kind: str
    value: str
    evidence: list[ProofEvidence]


class CandidateProof(BaseModel):
    candidate_sha256: str
    reviewed_block_ids: list[str]
    facts: list[ProofFact]


class FigmaNodePreview(BaseModel):
    node_id: str
    name: str
    kind: str
    figma_url: str


class FigmaSnapshotPreview(BaseModel):
    snapshot_sha256: str
    figma_url: str
    captured_at: str
    nodes: list[FigmaNodePreview]


class FigmaImportRequestPreview(BaseModel):
    request_sha256: str
    figma_url: str
    requested_at: str
    status: str
    snapshot_sha256: str | None = None


class DesignAlignmentBindingPreview(BaseModel):
    annotation_id: str | None = None
    evidence_block_id: str | None = None
    figma_node_id: str | None = None
    status: str


class DesignAlignmentDraftPreview(BaseModel):
    alignment_sha256: str
    figma_snapshot_sha256: str
    bindings: list[DesignAlignmentBindingPreview]
    source_candidate_sha256: str | None = None


class UnmappedFigmaNodePreview(BaseModel):
    snapshot_sha256: str
    node_id: str
    name: str


class KnowledgeApprovalPreview(BaseModel):
    candidate_sha256: str


class DesignAlignmentApprovalPreview(BaseModel):
    alignment_sha256: str
    figma_snapshot_sha256: str
