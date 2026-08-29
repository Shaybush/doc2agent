from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from doc2agent.models.document import AnalyzedDocument, ContentBlock, Section
from doc2agent.semantic.models import EvidenceRef, PipelineStep, SemanticKind


class PatchFact(BaseModel):
    """One provider-proposed fact; it cannot replace structural content."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: SemanticKind
    value: str = Field(min_length=1)
    pipeline_step: PipelineStep | None = None
    evidence: list[EvidenceRef] = Field(min_length=1)


class EnrichmentPatch(BaseModel):
    """Strict, scope-limited semantic additions for one structural revision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"]
    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    structural_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_block_ids: list[str] = Field(min_length=1)
    reviewed_block_ids: list[str] = Field(min_length=1)
    facts: list[PatchFact] = Field(default_factory=list)


class PatchValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    path: str | None = None


class PatchValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[PatchValidationIssue] = Field(default_factory=list)


class EnrichmentPatchValidator:
    """Validate a patch against one immutable canonical document revision."""

    def validate(
        self,
        patch: EnrichmentPatch,
        document: AnalyzedDocument,
    ) -> PatchValidationReport:
        errors: list[PatchValidationIssue] = []
        if document.source_manifest is None or not document.structural_sha256:
            errors.append(
                PatchValidationIssue(
                    code="MISSING_DOCUMENT_PROVENANCE",
                    message="Document must include source manifest and structural hash.",
                )
            )
            return PatchValidationReport(valid=False, errors=errors)

        if patch.source_id != document.source_manifest.source_id:
            errors.append(
                PatchValidationIssue(
                    code="SOURCE_ID_MISMATCH",
                    message="Patch source_id does not match the document.",
                    path="source_id",
                )
            )
        if patch.structural_sha256 != document.structural_sha256:
            errors.append(
                PatchValidationIssue(
                    code="STRUCTURAL_HASH_MISMATCH",
                    message="Patch structural_sha256 does not match the document.",
                    path="structural_sha256",
                )
            )

        content_blocks = self._content_blocks(document)
        block_ids = set(content_blocks)
        block_texts = self._block_texts(document)
        scope_ids = set(patch.scope_block_ids)
        if len(scope_ids) != len(patch.scope_block_ids):
            errors.append(
                PatchValidationIssue(
                    code="DUPLICATE_SCOPE_BLOCK_ID",
                    message="Patch scope_block_ids must be unique.",
                    path="scope_block_ids",
                )
            )
        for block_id in sorted(scope_ids - block_ids):
            errors.append(
                PatchValidationIssue(
                    code="UNKNOWN_SCOPE_BLOCK_ID",
                    message="Patch scope references an unknown content block.",
                    path=f"scope_block_ids:{block_id}",
                )
            )

        reviewed_ids = set(patch.reviewed_block_ids)
        if len(reviewed_ids) != len(patch.reviewed_block_ids):
            errors.append(
                PatchValidationIssue(
                    code="DUPLICATE_REVIEWED_BLOCK_ID",
                    message="Patch reviewed_block_ids must be unique.",
                    path="reviewed_block_ids",
                )
            )
        if reviewed_ids != block_ids:
            errors.append(
                PatchValidationIssue(
                    code="REVIEWED_BLOCK_IDS_MISMATCH",
                    message=(
                        "Patch reviewed_block_ids must exactly match the document's "
                        "content block IDs."
                    ),
                    path="reviewed_block_ids",
                )
            )

        fact_ids: set[str] = set()
        for fact_index, fact in enumerate(patch.facts):
            if fact.id in fact_ids:
                errors.append(
                    PatchValidationIssue(
                        code="DUPLICATE_FACT_ID",
                        message="Patch fact IDs must be unique.",
                        path=f"facts[{fact_index}].id",
                    )
                )
            fact_ids.add(fact.id)

            if not fact.value.strip():
                errors.append(
                    PatchValidationIssue(
                        code="EMPTY_FACT_VALUE",
                        message="Patch fact value must contain non-whitespace text.",
                        path=f"facts[{fact_index}].value",
                    )
                )

            for evidence_index, evidence in enumerate(fact.evidence):
                path = f"facts[{fact_index}].evidence[{evidence_index}]"
                self._validate_evidence(
                    evidence,
                    patch,
                    scope_ids,
                    path,
                    errors,
                )

            evidence_texts = [
                block_texts[evidence.block_id]
                for evidence in fact.evidence
                if evidence.block_id in block_texts
            ]
            if fact.value.strip() and not any(
                self._normalise_text(fact.value) in self._normalise_text(text)
                for text in evidence_texts
            ):
                errors.append(
                    PatchValidationIssue(
                        code="FACT_VALUE_NOT_IN_EVIDENCE",
                        message=(
                            "Patch fact value must preserve wording from at least one "
                            "evidence block."
                        ),
                        path=f"facts[{fact_index}].value",
                    )
                )

            if fact.pipeline_step is not None:
                self._validate_pipeline_step(
                    fact,
                    content_blocks,
                    fact_index,
                    errors,
                )

        return PatchValidationReport(valid=not errors, errors=errors)

    def validate_json(
        self,
        patch_path: str | Path,
        document_path: str | Path,
    ) -> PatchValidationReport:
        try:
            patch = EnrichmentPatch.model_validate_json(
                Path(patch_path).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            return PatchValidationReport(
                valid=False,
                errors=[
                    PatchValidationIssue(
                        code="INVALID_PATCH_SCHEMA",
                        message=str(exc),
                        path=str(patch_path),
                    )
                ],
            )

        try:
            document = AnalyzedDocument.model_validate_json(
                Path(document_path).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            return PatchValidationReport(
                valid=False,
                errors=[
                    PatchValidationIssue(
                        code="INVALID_DOCUMENT_SCHEMA",
                        message=str(exc),
                        path=str(document_path),
                    )
                ],
            )

        return self.validate(patch, document)

    @staticmethod
    def _validate_evidence(
        evidence: EvidenceRef,
        patch: EnrichmentPatch,
        scope_ids: set[str],
        path: str,
        errors: list[PatchValidationIssue],
    ) -> None:
        if evidence.source_id != patch.source_id:
            errors.append(
                PatchValidationIssue(
                    code="EVIDENCE_SOURCE_ID_MISMATCH",
                    message="Evidence source_id does not match the patch.",
                    path=f"{path}.source_id",
                )
            )
        if evidence.structural_sha256 != patch.structural_sha256:
            errors.append(
                PatchValidationIssue(
                    code="EVIDENCE_STRUCTURAL_HASH_MISMATCH",
                    message="Evidence structural_sha256 does not match the patch.",
                    path=f"{path}.structural_sha256",
                )
            )
        if evidence.block_id not in scope_ids:
            errors.append(
                PatchValidationIssue(
                    code="EVIDENCE_OUTSIDE_SCOPE",
                    message="Evidence block_id is outside the allowed patch scope.",
                    path=f"{path}.block_id",
                )
            )

    @staticmethod
    def _block_ids(document: AnalyzedDocument) -> set[str]:
        return set(EnrichmentPatchValidator._block_texts(document))

    @staticmethod
    def _content_blocks(document: AnalyzedDocument) -> dict[str, ContentBlock]:
        blocks = {
            block.id: block
            for block in document.preamble
        }

        def collect(section: Section) -> None:
            blocks.update(
                {
                    block.id: block
                    for block in section.blocks
                }
            )
            for child in section.children:
                collect(child)

        for section in document.sections:
            collect(section)

        return blocks

    @staticmethod
    def _block_texts(document: AnalyzedDocument) -> dict[str, str]:
        return {
            block_id: EnrichmentPatchValidator._content_text(block)
            for block_id, block in EnrichmentPatchValidator._content_blocks(document).items()
        }

    @staticmethod
    def _content_text(block: ContentBlock) -> str:
        return "\n".join(
            [
                *([block.text] if block.text else []),
                *block.items,
                *block.headers,
                *(cell for row in block.rows for cell in row),
            ]
        )

    @staticmethod
    def _normalise_text(value: str) -> str:
        return " ".join(value.split())

    @classmethod
    def _validate_pipeline_step(
        cls,
        fact: PatchFact,
        content_blocks: dict[str, ContentBlock],
        fact_index: int,
        errors: list[PatchValidationIssue],
    ) -> None:
        if fact.kind != "architecture_pipeline":
            errors.append(
                PatchValidationIssue(
                    code="PIPELINE_STEP_KIND_INVALID",
                    message="pipeline_step is only allowed for architecture_pipeline facts.",
                    path=f"facts[{fact_index}].pipeline_step",
                )
            )
            return

        step = fact.pipeline_step
        assert step is not None
        for evidence in fact.evidence:
            block = content_blocks.get(evidence.block_id)
            if block is None or block.type != "table":
                continue
            columns = cls._pipeline_step_columns(block.headers)
            if columns is None:
                continue
            component_index, input_index, output_index = columns
            if any(
                len(row) <= index
                for row in block.rows
                for index in columns
            ):
                continue
            if any(
                row[component_index] == step.component
                and row[input_index] == step.input
                and row[output_index] == step.output
                for row in block.rows
            ):
                return

        errors.append(
            PatchValidationIssue(
                code="PIPELINE_STEP_NOT_IN_EVIDENCE_ROW",
                message=(
                    "pipeline_step must exactly match Component, Input, and Output cells "
                    "from one evidence table row."
                ),
                path=f"facts[{fact_index}].pipeline_step",
            )
        )

    @classmethod
    def _pipeline_step_columns(cls, headers: list[str]) -> tuple[int, int, int] | None:
        normalized_headers = [cls._normalise_header(header) for header in headers]
        aliases = {
            "component": {"component", "רכיב"},
            "input": {"input", "קלט"},
            "output": {"output", "פלט"},
        }
        matching_indexes = {
            field: [
                index
                for index, header in enumerate(normalized_headers)
                if header in field_aliases
            ]
            for field, field_aliases in aliases.items()
        }
        if any(len(indexes) != 1 for indexes in matching_indexes.values()):
            return None
        component_index = matching_indexes["component"][0]
        input_index = matching_indexes["input"][0]
        output_index = matching_indexes["output"][0]
        return component_index, input_index, output_index

    @staticmethod
    def _normalise_header(value: str) -> str:
        return " ".join(value.replace("*", "").split()).casefold()
