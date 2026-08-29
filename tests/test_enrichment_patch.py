from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
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
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block_id],
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
