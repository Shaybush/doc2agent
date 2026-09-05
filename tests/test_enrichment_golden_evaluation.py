from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.pipeline import Doc2AgentPipeline
from doc2agent.semantic import (
    CandidateBuilder,
    EnrichmentGoldenRunner,
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
    PipelineStep,
    TopicRenderer,
)
from doc2agent.source_manifest import SourceManifest

FIXTURE = Path(__file__).parent / "fixtures" / "hebrew_test_document.docx"
GOLDEN = Path(__file__).parent / "fixtures" / "enrichment-v2-hebrew-golden.json"


def _document(tmp_path: Path):
    source = FIXTURE
    manifest = SourceManifest.from_path(
        source,
        received_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    # Conversion remains the source-faithful path; re-analysis fixes only the
    # artifact path and manifest timestamp so this golden input is repeatable.
    converted = Doc2AgentPipeline().converter.convert(source, tmp_path / "output")
    normalized = Doc2AgentPipeline().normalizer.normalize(
        converted.markdown.read_text(encoding="utf-8")
    )
    return StructuralAnalyzer().analyze_text(
        normalized,
        markdown_path="tests/fixtures/hebrew_test_document.normalized.md",
        metadata=json.loads(converted.metadata.read_text(encoding="utf-8")),
        source_manifest=manifest,
    )


def _patch(document) -> EnrichmentPatch:
    runner = EnrichmentGoldenRunner()
    golden = runner._load_fixture(GOLDEN)
    facts = []
    for required in golden.required_facts:
        evidence = runner._expected_evidence(required, document)
        facts.append(
            PatchFact(
                id=required.id,
                kind=required.kind,
                value=required.value,
                evidence=[evidence],
            )
        )
    return EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=facts,
    )


def test_hebrew_v2_golden_evaluation_passes_patch_candidate_and_markdown(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    patch = _patch(document)
    runner = EnrichmentGoldenRunner()

    patch_report = runner.evaluate_patch(patch, document, GOLDEN)
    candidate = CandidateBuilder().build(patch, document)
    rendered = TopicRenderer().render(candidate, tmp_path / "copilot-context")
    markdown = rendered.context_path.read_text(encoding="utf-8")
    candidate_report = runner.evaluate_candidate(candidate, GOLDEN, markdown=markdown)

    assert patch_report.passed is True
    assert patch_report.required_fact_count == 12
    assert patch_report.matched_required_fact_ids == [fact.id for fact in patch.facts]
    assert '<div dir="rtl">' in markdown
    assert candidate_report.passed is True
    assert candidate_report.markdown_mismatch_codes == []


def test_hebrew_v2_golden_reports_forbidden_and_invalid_evidence(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    patch = _patch(document)
    patch.facts[0].value = "doc2agent_test_image.png"
    patch.facts[0].evidence[0] = EvidenceRef(
        source_id=document.source_manifest.source_id,
        block_id="block:unknown",
        structural_sha256=document.structural_sha256,
        support="explicit",
    )

    report = EnrichmentGoldenRunner().evaluate_patch(patch, document, GOLDEN)

    assert report.passed is False
    assert report.missing_required_fact_ids == ["requirement:heading-hierarchy"]
    assert report.unexpected_fact_ids == ["requirement:heading-hierarchy"]
    assert report.forbidden_fact_ids == ["requirement:heading-hierarchy"]
    assert report.invalid_evidence_fact_ids == ["requirement:heading-hierarchy"]
    assert "EVIDENCE_OUTSIDE_SCOPE" in report.invalid_contract_codes


def test_hebrew_v2_golden_accepts_provider_owned_fact_ids(tmp_path: Path) -> None:
    document = _document(tmp_path)
    patch = _patch(document)
    for index, fact in enumerate(patch.facts, start=1):
        fact.id = f"provider:fact-{index}"

    candidate = CandidateBuilder().build(patch, document)
    rendered = TopicRenderer().render(candidate, tmp_path / "copilot-context")
    report = EnrichmentGoldenRunner().evaluate_candidate(
        candidate,
        GOLDEN,
        markdown=rendered.context_path.read_text(encoding="utf-8"),
    )

    assert report.passed is True
    assert report.unexpected_fact_ids == []


def test_hebrew_v2_golden_reports_missing_component_statement(tmp_path: Path) -> None:
    document = _document(tmp_path)
    patch = _patch(document)
    patch.facts = [
        fact
        for fact in patch.facts
        if fact.id != "architecture:component-structural-analyzer"
    ]

    report = EnrichmentGoldenRunner().evaluate_patch(patch, document, GOLDEN)

    assert report.passed is False
    assert report.missing_required_fact_ids == [
        "architecture:component-structural-analyzer"
    ]


def test_golden_markdown_matches_renderer_for_purpose_and_pipeline_step(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pipeline.md"
    source.write_text(
        "# Pipeline\n\nThe purpose is reliable conversion.\n\n"
        "| Component | Input | Output |\n| --- | --- | --- |\n"
        "| Converter | DOCX | Markdown |\n",
        encoding="utf-8",
    )
    document = StructuralAnalyzer().analyze(
        source,
        source_manifest=SourceManifest.from_path(source),
    )
    purpose_block, table = document.sections[0].blocks
    fixture = tmp_path / "golden.json"
    fixture.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "required_facts": [
                    {
                        "id": "purpose",
                        "kind": "purpose",
                        "value": "The purpose is reliable conversion.",
                        "evidence_text": "The purpose is reliable conversion.",
                    },
                    {
                        "id": "pipeline",
                        "kind": "architecture_pipeline",
                        "value": "Converter DOCX Markdown",
                        "evidence_row": ["Converter", "DOCX", "Markdown"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[purpose_block.id, table.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="purpose",
                kind="purpose",
                value="The purpose is reliable conversion.",
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=purpose_block.id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            ),
            PatchFact(
                id="pipeline",
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
            ),
        ],
    )
    candidate = CandidateBuilder().build(patch, document)
    rendered = TopicRenderer().render(candidate, tmp_path / "copilot-context")

    report = EnrichmentGoldenRunner().evaluate_candidate(
        candidate,
        fixture,
        markdown=rendered.context_path.read_text(encoding="utf-8"),
    )

    assert report.passed is True
    assert report.markdown_mismatch_codes == []
