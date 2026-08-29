from __future__ import annotations

import json
from pathlib import Path

import pytest

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    EvidenceRef,
    FixtureSemanticProvider,
    SemanticAnalyzer,
    SemanticAnnotation,
)
from doc2agent.source_manifest import SourceManifest

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.md"
EVALUATION_MARKDOWN = (
    Path(__file__).parent
    / "fixtures"
    / "semantic-evaluation.md"
)
EVALUATION_CASES = (
    Path(__file__).parent
    / "fixtures"
    / "semantic-evaluation-cases.json"
)


def _document():
    return StructuralAnalyzer().analyze(SAMPLE)


def _source_id_by_text(document, source_text: str) -> str:
    blocks = list(document.preamble)

    def collect(section) -> None:
        blocks.extend(section.blocks)

        for child in section.children:
            collect(child)

    for section in document.sections:
        collect(section)

    return next(
        block.id
        for block in blocks
        if block.text == source_text
    )


def test_noop_provider_preserves_canonical_document() -> None:
    document = _document()

    knowledge = SemanticAnalyzer().analyze(document)

    assert knowledge.provider == "noop"
    assert knowledge.annotations == []
    assert knowledge.document == document
    assert knowledge.document is not document


def test_fixture_provider_keeps_evidence_linked_to_source_ids(
    tmp_path: Path,
) -> None:
    document = _document()
    annotation = SemanticAnnotation(
        id="annotation:requirement",
        kind="requirement",
        value="Preserve the canonical source document.",
        source_ids=[document.document.id],
        origin="VERIFIED",
    )

    knowledge = SemanticAnalyzer(
        FixtureSemanticProvider(annotations=(annotation,)),
    ).analyze(document)
    output_path = SemanticAnalyzer().write_json(
        knowledge,
        tmp_path / "knowledge.json",
    )

    output = json.loads(output_path.read_text(encoding="utf-8"))

    assert knowledge.provider == "fixture"
    assert output["annotations"][0]["source_ids"] == [document.document.id]
    assert output["document"] == document.model_dump(
        mode="json",
        exclude_none=True,
    )


def test_analyzer_rejects_unknown_annotation_source_id() -> None:
    annotation = SemanticAnnotation(
        id="annotation:unknown",
        kind="requirement",
        value="Invalid evidence.",
        source_ids=["missing-source"],
        origin="AI_INFERRED",
    )
    analyzer = SemanticAnalyzer(
        FixtureSemanticProvider(annotations=(annotation,)),
    )

    with pytest.raises(
        ValueError,
        match="Unknown semantic annotation source ids: missing-source",
    ):
        analyzer.analyze(_document())


def test_evidence_is_bound_to_source_and_structural_revision() -> None:
    document = StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )
    block_id = document.sections[0].blocks[0].id
    annotation = SemanticAnnotation(
        id="annotation:evidence",
        kind="requirement",
        value="Evidence must identify one source revision.",
        origin="AI_INFERRED",
        evidence=[
            EvidenceRef(
                source_id=document.source_manifest.source_id,
                block_id=block_id,
                structural_sha256=document.structural_sha256,
                support="explicit",
            )
        ],
    )

    knowledge = SemanticAnalyzer(
        FixtureSemanticProvider(annotations=(annotation,)),
    ).analyze(document)

    assert knowledge.annotations[0].evidence == annotation.evidence


def test_analyzer_rejects_evidence_for_another_revision() -> None:
    document = StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )
    annotation = SemanticAnnotation(
        id="annotation:wrong-revision",
        kind="requirement",
        value="Invalid evidence.",
        origin="AI_INFERRED",
        evidence=[
            EvidenceRef(
                source_id=document.source_manifest.source_id,
                block_id=document.sections[0].blocks[0].id,
                structural_sha256="0" * 64,
                support="explicit",
            )
        ],
    )

    with pytest.raises(
        ValueError,
        match="Evidence structural_sha256 does not match document",
    ):
        SemanticAnalyzer(
            FixtureSemanticProvider(annotations=(annotation,)),
        ).analyze(document)


def test_multilingual_evaluation_cases_match_semantic_contract() -> None:
    document = StructuralAnalyzer().analyze(EVALUATION_MARKDOWN)
    cases = json.loads(EVALUATION_CASES.read_text(encoding="utf-8"))
    annotations = tuple(
        SemanticAnnotation(
            id=f"annotation:{case['id']}",
            kind=case["kind"],
            value=case["value"],
            source_ids=[
                _source_id_by_text(document, case["source_text"])
            ],
            origin=case["origin"],
        )
        for case in cases
    )

    knowledge = SemanticAnalyzer(
        FixtureSemanticProvider(annotations=annotations),
    ).analyze(document)

    assert len(knowledge.annotations) == 9
    assert {annotation.kind for annotation in knowledge.annotations} == {
        "requirement",
        "decision",
        "action_item",
    }
    assert {
        annotation.origin
        for annotation in knowledge.annotations
    } == {"AI_INFERRED"}
