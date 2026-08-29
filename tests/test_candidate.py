from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    CandidateBuilder,
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


def test_candidate_is_hash_addressed_and_preserves_document() -> None:
    document = _document()

    candidate = CandidateBuilder().build(_patch(document), document)

    assert len(candidate.candidate_sha256) == 64
    assert candidate.schema_version == "2.0"
    assert candidate.source_id == document.source_manifest.source_id
    assert candidate.reviewed_block_ids == sorted(EnrichmentPatchValidator._block_ids(document))
    assert candidate.document == document
    assert candidate.document is not document
    assert candidate.annotations[0].origin == "AI_INFERRED"


def test_candidate_schema_rejects_legacy_version() -> None:
    document = _document()
    candidate = CandidateBuilder().build(_patch(document), document)
    candidate_data = candidate.model_dump(mode="json")
    candidate_data["schema_version"] = "1.0"

    with pytest.raises(ValidationError, match="schema_version"):
        candidate.__class__.model_validate(candidate_data)


def test_candidate_hash_is_stable_for_identical_patch_and_document() -> None:
    document = _document()
    builder = CandidateBuilder()

    first = builder.build(_patch(document), document)
    second = builder.build(_patch(document), document)

    assert first.candidate_sha256 == second.candidate_sha256


def test_candidate_rejects_invalid_patch() -> None:
    document = _document()
    patch = _patch(document)
    patch.structural_sha256 = "0" * 64

    with pytest.raises(ValueError, match="STRUCTURAL_HASH_MISMATCH"):
        CandidateBuilder().build(patch, document)


def test_create_candidate_cli_writes_candidate_artifact(tmp_path: Path) -> None:
    document = _document()
    document_path = tmp_path / "document.json"
    patch_path = tmp_path / "patch.json"
    output_path = tmp_path / "candidate.knowledge.json"
    document_path.write_text(
        document.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    patch_path.write_text(
        _patch(document).model_dump_json(indent=2),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "create-candidate",
            str(patch_path),
            "--document",
            str(document_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output_path.exists()
    assert "Candidate hash:" in result.output
