from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from doc2agent.models.document import AnalyzedDocument

SemanticOrigin = Literal["VERIFIED", "AI_INFERRED"]
SemanticKind = Literal[
    "purpose",
    "requirement",
    "decision",
    "action_item",
    "architecture_pipeline",
]
EvidenceSupport = Literal["explicit", "inferred", "ambiguous"]
COPILOT_CONTEXT_KINDS = (
    "purpose",
    "requirement",
    "decision",
    "architecture_pipeline",
    "action_item",
)
COPILOT_CONTEXT_LABELS = {
    "purpose": "Purpose",
    "requirement": "Requirements",
    "decision": "Decisions",
    "architecture_pipeline": "Architecture and pipeline",
    "action_item": "Action items",
}


class EvidenceRef(BaseModel):
    """Evidence for a semantic fact in one structural document revision."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    block_id: str = Field(min_length=1)
    structural_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    support: EvidenceSupport


class PipelineStep(BaseModel):
    """Exact Component/Input/Output cells from one evidence-table row."""

    model_config = ConfigDict(extra="forbid")

    component: str = Field(min_length=1)
    input: str = Field(min_length=1)
    output: str = Field(min_length=1)


class SemanticAnnotation(BaseModel):
    """Provider-produced knowledge with explicit evidence in document.json."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: SemanticKind
    value: str = Field(min_length=1)
    pipeline_step: PipelineStep | None = None
    # Legacy migration field. New annotations should use ``evidence``.
    source_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    origin: SemanticOrigin


def copilot_fact_value(annotation: SemanticAnnotation) -> str:
    """Format a semantic fact without changing source-linked values."""

    if annotation.pipeline_step is None:
        return annotation.value
    step = annotation.pipeline_step
    return f"{step.component}: {step.input} → {step.output}"


class KnowledgeDocument(BaseModel):
    """Immutable semantic wrapper around the canonical structural document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    document: AnalyzedDocument
    provider: str = Field(min_length=1)
    annotations: list[SemanticAnnotation] = Field(default_factory=list)
