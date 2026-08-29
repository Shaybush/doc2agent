from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
    PipelineStep,
)
from doc2agent.source_manifest import SourceManifest

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.md"


def _document():
    return StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )


def _patch(document) -> EnrichmentPatch:
    block_id = document.sections[0].blocks[0].id
    return EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block_id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:requirement",
                kind="requirement",
                value="General English paragraph.",
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=block_id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            )
        ],
    )


def test_valid_patch_is_limited_to_its_document_scope() -> None:
    document = _document()

    report = EnrichmentPatchValidator().validate(_patch(document), document)

    assert report.valid is True
    assert report.errors == []


def test_patch_rejects_evidence_outside_its_scope() -> None:
    document = _document()
    patch = _patch(document)
    patch.facts[0].evidence[0].block_id = document.sections[0].children[0].blocks[0].id

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is False
    assert report.errors[0].code == "EVIDENCE_OUTSIDE_SCOPE"


def test_patch_requires_review_coverage_for_every_content_block() -> None:
    document = _document()
    patch = _patch(document)
    patch.reviewed_block_ids = [document.sections[0].blocks[0].id]

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is False
    assert any(issue.code == "REVIEWED_BLOCK_IDS_MISMATCH" for issue in report.errors)


def test_patch_schema_requires_reviewed_block_ids() -> None:
    patch_data = _patch(_document()).model_dump(mode="json")
    patch_data.pop("reviewed_block_ids")

    with pytest.raises(ValidationError, match="reviewed_block_ids"):
        EnrichmentPatch.model_validate(patch_data)


def test_patch_schema_rejects_legacy_version() -> None:
    patch_data = _patch(_document()).model_dump(mode="json")
    patch_data["schema_version"] = "1.0"

    with pytest.raises(ValidationError, match="schema_version"):
        EnrichmentPatch.model_validate(patch_data)


def test_patch_schema_requires_version() -> None:
    patch_data = _patch(_document()).model_dump(mode="json")
    patch_data.pop("schema_version")

    with pytest.raises(ValidationError, match="schema_version"):
        EnrichmentPatch.model_validate(patch_data)


def test_patch_rejects_a_different_structural_revision() -> None:
    document = _document()
    patch = _patch(document)
    patch.structural_sha256 = "0" * 64

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is False
    assert any(
        issue.code == "STRUCTURAL_HASH_MISMATCH"
        for issue in report.errors
    )


def test_patch_rejects_translated_or_paraphrased_fact_value() -> None:
    document = _document()
    patch = _patch(document)
    patch.facts[0].value = "A general English paragraph."

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is False
    assert any(issue.code == "FACT_VALUE_NOT_IN_EVIDENCE" for issue in report.errors)


def _pipeline_document(tmp_path: Path):
    source = tmp_path / "pipeline.md"
    source.write_text(
        "# Pipeline\n\n| Component | Input | Output |\n| --- | --- | --- |\n"
        "| Converter | DOCX | Markdown |\n",
        encoding="utf-8",
    )
    return StructuralAnalyzer().analyze(
        source,
        source_manifest=SourceManifest.from_path(source),
    )


def _pipeline_patch(document) -> EnrichmentPatch:
    table = document.sections[0].blocks[0]
    return EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[table.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:pipeline",
                kind="architecture_pipeline",
                value="Converter DOCX Markdown",
                pipeline_step=PipelineStep(
                    component="Converter",
                    input="DOCX",
                    output="Markdown",
                ),
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=table.id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            )
        ],
    )


def test_pipeline_step_must_match_its_exact_evidence_table_row(tmp_path: Path) -> None:
    document = _pipeline_document(tmp_path)

    report = EnrichmentPatchValidator().validate(_pipeline_patch(document), document)

    assert report.valid is True


def test_pipeline_step_rejects_values_not_in_its_evidence_table_row(tmp_path: Path) -> None:
    document = _pipeline_document(tmp_path)
    patch = _pipeline_patch(document)
    patch.facts[0].pipeline_step.input = "PDF"

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is False
    assert any(issue.code == "PIPELINE_STEP_NOT_IN_EVIDENCE_ROW" for issue in report.errors)


def test_pipeline_step_requires_an_architecture_pipeline_fact(tmp_path: Path) -> None:
    document = _pipeline_document(tmp_path)
    patch = _pipeline_patch(document)
    patch.facts[0].kind = "requirement"

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is False
    assert any(issue.code == "PIPELINE_STEP_KIND_INVALID" for issue in report.errors)


def test_pipeline_step_accepts_hebrew_component_input_output_headers(tmp_path: Path) -> None:
    source = tmp_path / "pipeline-hebrew.md"
    source.write_text(
        "# Pipeline\n\n| **רכיב** | **קלט** | **פלט** |\n| --- | --- | --- |\n"
        "| Converter | DOCX | Markdown |\n",
        encoding="utf-8",
    )
    document = StructuralAnalyzer().analyze(
        source,
        source_manifest=SourceManifest.from_path(source),
    )
    patch = _pipeline_patch(document)

    report = EnrichmentPatchValidator().validate(patch, document)

    assert report.valid is True


def test_validate_patch_cli_reports_schema_and_contract_errors(
    tmp_path: Path,
) -> None:
    document = _document()
    document_path = tmp_path / "document.json"
    patch_path = tmp_path / "patch.json"
    document_path.write_text(
        document.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    patch_path.write_text(
        json.dumps(_patch(document).model_dump(mode="json")),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        ["validate-patch", str(patch_path), "--document", str(document_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Patch validation: OK" in result.output
