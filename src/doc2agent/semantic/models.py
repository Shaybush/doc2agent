from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from doc2agent.models.document import AnalyzedDocument

SemanticOrigin = Literal["VERIFIED", "AI_INFERRED"]
SemanticKind = Literal["requirement", "decision", "action_item"]
EvidenceSupport = Literal["explicit", "inferred", "ambiguous"]


class EvidenceRef(BaseModel):
    """Evidence for a semantic fact in one structural document revision."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    block_id: str = Field(min_length=1)
    structural_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    support: EvidenceSupport


class SemanticAnnotation(BaseModel):
    """Provider-produced knowledge with explicit evidence in document.json."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: SemanticKind
    value: str = Field(min_length=1)
    # Legacy migration field. New annotations should use ``evidence``.
    source_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    origin: SemanticOrigin


class KnowledgeDocument(BaseModel):
    """Immutable semantic wrapper around the canonical structural document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    document: AnalyzedDocument
    provider: str = Field(min_length=1)
    annotations: list[SemanticAnnotation] = Field(default_factory=list)
