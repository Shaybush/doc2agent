from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    CandidateBuilder,
    CandidateReviewer,
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
)
from doc2agent.source_manifest import SourceManifest

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.md"


def _candidate():
    document = StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )
    block = document.sections[0].blocks[0]
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:requirement",
                kind="requirement",
                value="General English paragraph.",
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=block.id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            )
        ],
    )
    return CandidateBuilder().build(patch, document), block


def test_candidate_review_shows_fact_and_exact_source_block() -> None:
    candidate, block = _candidate()

    review = CandidateReviewer().review(candidate)

    assert review.facts[0].annotation.value == "General English paragraph."
    assert review.facts[0].evidence[0].source_text == block.text
    assert review.facts[0].evidence[0].section_path == tuple(
        candidate.document.sections[0].path
    )
    assert review.reviewed_block_ids == tuple(candidate.reviewed_block_ids)
    assert "Reviewed content blocks: " in CandidateReviewer.render(review)
    assert "lines" in CandidateReviewer.render(review)


def test_review_candidate_cli_is_read_only(tmp_path: Path) -> None:
    candidate, block = _candidate()
    candidate_path = tmp_path / "candidate.knowledge.json"
    candidate_path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["review-candidate", str(candidate_path)])

    assert result.exit_code == 0, result.output
    assert "[requirement] General English paragraph." in result.output
    assert block.text in result.output
