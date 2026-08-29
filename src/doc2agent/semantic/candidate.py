from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from doc2agent.artifacts import canonical_json_sha256
from doc2agent.models.document import AnalyzedDocument
from doc2agent.semantic.enrichment_patch import (
    EnrichmentPatch,
    EnrichmentPatchValidator,
    PatchFact,
    PatchValidationReport,
)
from doc2agent.semantic.models import SemanticAnnotation


class KnowledgeCandidate(BaseModel):
    """Validated semantic proposal that is not yet approved or published."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"]
    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    structural_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_block_ids: list[str] = Field(min_length=1)
    document: AnalyzedDocument
    annotations: list[SemanticAnnotation] = Field(default_factory=list)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateBuilder:
    """Create a hash-addressed candidate only from a validated patch."""

    def __init__(
        self,
        patch_validator: EnrichmentPatchValidator | None = None,
    ) -> None:
        self.patch_validator = patch_validator or EnrichmentPatchValidator()

    def build(
        self,
        patch: EnrichmentPatch,
        document: AnalyzedDocument,
    ) -> KnowledgeCandidate:
        report = self.patch_validator.validate(patch, document)
        if not report.valid:
            codes = ", ".join(issue.code for issue in report.errors)
            raise ValueError(f"Cannot create candidate from invalid patch: {codes}")

        annotations = [
            SemanticAnnotation(
                id=fact.id,
                kind=fact.kind,
                value=fact.value,
                pipeline_step=fact.pipeline_step,
                evidence=fact.evidence,
                origin="AI_INFERRED",
            )
            for fact in patch.facts
        ]
        candidate = KnowledgeCandidate(
            schema_version="2.0",
            source_id=patch.source_id,
            structural_sha256=patch.structural_sha256,
            reviewed_block_ids=patch.reviewed_block_ids,
            document=document.model_copy(deep=True),
            annotations=annotations,
            candidate_sha256="0" * 64,
        )
        candidate_hash = canonical_json_sha256(
            candidate.model_dump(
                mode="json",
                exclude={"candidate_sha256"},
            )
        )
        return candidate.model_copy(
            update={"candidate_sha256": candidate_hash}
        )

    @staticmethod
    def write_json(
        candidate: KnowledgeCandidate,
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            candidate.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
        return output_path


def candidate_contract_report(candidate: KnowledgeCandidate) -> PatchValidationReport:
    """Apply the v2 patch invariants to a self-contained candidate snapshot."""

    document = candidate.document
    patch = EnrichmentPatch.model_construct(
        schema_version="2.0",
        source_id=candidate.source_id,
        structural_sha256=candidate.structural_sha256,
        scope_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        reviewed_block_ids=candidate.reviewed_block_ids,
        facts=[
            PatchFact.model_construct(
                id=annotation.id,
                kind=annotation.kind,
                value=annotation.value,
                pipeline_step=annotation.pipeline_step,
                evidence=annotation.evidence,
            )
            for annotation in candidate.annotations
        ],
    )
    return EnrichmentPatchValidator().validate(patch, document)


def validate_candidate_contract(candidate: KnowledgeCandidate) -> None:
    """Reject candidate JSON that cannot satisfy the constrained patch contract."""

    report = candidate_contract_report(candidate)
    if not report.valid:
        codes = ", ".join(issue.code for issue in report.errors)
        raise ValueError(f"Candidate violates enrichment contract: {codes}")
