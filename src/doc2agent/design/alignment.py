"""Immutable Figma snapshot and requirement-alignment contracts.

This module intentionally knows nothing about MCP transports. An adapter may
produce a normalized ``FigmaSnapshot`` later; review and rendering consume only
the snapshot's immutable content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doc2agent.artifacts import canonical_json_sha256

if TYPE_CHECKING:
    from doc2agent.semantic.candidate import KnowledgeCandidate

DesignReviewStatus = Literal[
    "matches",
    "partial",
    "missing_design",
    "uncovered_design",
    "needs_clarification",
]


def validate_figma_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    if parsed.scheme != "https" or not (hostname == "figma.com" or hostname.endswith(".figma.com")):
        raise ValueError("Figma URL must use HTTPS and a figma.com host")
    return value


class FigmaNode(BaseModel):
    """Minimal, display-safe identity for one selected Figma object."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    figma_url: str = Field(min_length=1)

    _url_is_figma = model_validator(mode="after")(
        lambda model: model._validate_url()
    )

    def _validate_url(self) -> FigmaNode:
        validate_figma_url(self.figma_url)
        return self


class FigmaSnapshot(BaseModel):
    """Hash-addressed, normalized snapshot of explicitly selected Figma nodes."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    figma_file_key: str = Field(min_length=1)
    figma_url: str = Field(min_length=1)
    nodes: list[FigmaNode] = Field(min_length=1)
    captured_at: datetime
    adapter: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_contract(self) -> FigmaSnapshot:
        validate_figma_url(self.figma_url)
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("Figma snapshot has duplicate node IDs")
        expected_hash = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"snapshot_sha256"})
        )
        if self.snapshot_sha256 != expected_hash:
            raise ValueError("Figma snapshot hash does not match snapshot content")
        return self

    @classmethod
    def build(
        cls,
        *,
        figma_file_key: str,
        figma_url: str,
        nodes: list[FigmaNode],
        adapter: str,
        captured_at: datetime | None = None,
    ) -> FigmaSnapshot:
        snapshot = cls.model_construct(
            schema_version="1.0",
            figma_file_key=figma_file_key,
            figma_url=figma_url,
            nodes=nodes,
            captured_at=captured_at or datetime.now(UTC),
            adapter=adapter,
            snapshot_sha256="0" * 64,
        )
        snapshot_hash = canonical_json_sha256(
            snapshot.model_dump(mode="json", exclude={"snapshot_sha256"})
        )
        payload = snapshot.model_dump(mode="json") | {"snapshot_sha256": snapshot_hash}
        return cls.model_validate(payload)


class DesignBinding(BaseModel):
    """One explicit review decision between a requirement and a design node."""

    model_config = ConfigDict(extra="forbid")

    annotation_id: str | None = Field(default=None, min_length=1)
    evidence_block_id: str | None = Field(default=None, min_length=1)
    figma_node_id: str | None = Field(default=None, min_length=1)
    status: DesignReviewStatus
    comment: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> DesignBinding:
        requirement_binding = self.status != "uncovered_design"
        if requirement_binding and (not self.annotation_id or not self.evidence_block_id):
            raise ValueError("Requirement bindings require annotation and evidence block IDs")
        if self.status == "uncovered_design" and (self.annotation_id or self.evidence_block_id):
            raise ValueError("Uncovered design must not claim a requirement")
        if self.status == "missing_design" and self.figma_node_id:
            raise ValueError("Missing design must not point to a Figma node")
        if self.status != "missing_design" and not self.figma_node_id:
            raise ValueError("Design binding requires a Figma node")
        return self


class DesignAlignmentCandidate(BaseModel):
    """Reviewable mapping tied to one knowledge candidate and one snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    structural_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    figma_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: list[DesignBinding] = Field(min_length=1)
    alignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_hash(self) -> DesignAlignmentCandidate:
        expected_hash = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"alignment_sha256"})
        )
        if self.alignment_sha256 != expected_hash:
            raise ValueError("Design alignment hash does not match alignment content")
        return self


class ApprovedDesignAlignment(BaseModel):
    """A human-approved design mapping, kept outside semantic knowledge."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    document_id: str = Field(min_length=1)
    alignment: DesignAlignmentCandidate
    snapshot: FigmaSnapshot
    approved_by: str = Field(min_length=1)
    approved_at: datetime

    @model_validator(mode="after")
    def validate_snapshot_link(self) -> ApprovedDesignAlignment:
        if self.alignment.figma_snapshot_sha256 != self.snapshot.snapshot_sha256:
            raise ValueError("Approved design alignment references a different snapshot")
        return self


class DesignAlignmentBuilder:
    """Build validated design mappings without inferring semantic facts."""

    def build(
        self,
        knowledge: KnowledgeCandidate,
        snapshot: FigmaSnapshot,
        bindings: list[DesignBinding],
    ) -> DesignAlignmentCandidate:
        self._validate_bindings(knowledge, snapshot, bindings)
        candidate = DesignAlignmentCandidate.model_construct(
            schema_version="1.0",
            source_id=knowledge.source_id,
            structural_sha256=knowledge.structural_sha256,
            candidate_sha256=knowledge.candidate_sha256,
            figma_snapshot_sha256=snapshot.snapshot_sha256,
            bindings=bindings,
            alignment_sha256="0" * 64,
        )
        alignment_hash = canonical_json_sha256(
            candidate.model_dump(mode="json", exclude={"alignment_sha256"})
        )
        return DesignAlignmentCandidate.model_validate(
            candidate.model_dump(mode="json") | {"alignment_sha256": alignment_hash}
        )

    @staticmethod
    def _validate_bindings(
        knowledge: KnowledgeCandidate,
        snapshot: FigmaSnapshot,
        bindings: list[DesignBinding],
    ) -> None:
        if not bindings:
            raise ValueError("Design alignment requires at least one binding")
        annotations = {annotation.id: annotation for annotation in knowledge.annotations}
        node_ids = {node.node_id for node in snapshot.nodes}
        claimed_annotations: set[str] = set()
        claimed_nodes: set[str] = set()
        for binding in bindings:
            if binding.annotation_id:
                if binding.annotation_id in claimed_annotations:
                    raise ValueError("Design alignment has duplicate annotation bindings")
                claimed_annotations.add(binding.annotation_id)
                annotation = annotations.get(binding.annotation_id)
                if annotation is None:
                    raise ValueError("Design alignment references an unknown annotation")
                evidence_block_ids = {
                    evidence.block_id for evidence in annotation.evidence
                }
                if binding.evidence_block_id not in evidence_block_ids:
                    raise ValueError(
                        "Design alignment evidence block is not attached to annotation"
                    )
            if binding.figma_node_id:
                if binding.figma_node_id not in node_ids:
                    raise ValueError("Design alignment references an unknown Figma node")
                if binding.status == "uncovered_design":
                    if binding.figma_node_id in claimed_nodes:
                        raise ValueError("Design alignment has duplicate uncovered Figma nodes")
                    claimed_nodes.add(binding.figma_node_id)
