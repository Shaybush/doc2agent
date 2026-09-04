from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.design import (
    DesignAlignmentApprovalService,
    DesignAlignmentBuilder,
    DesignBinding,
    FigmaNode,
    FigmaSnapshot,
)
from doc2agent.semantic import (
    ApprovalService,
    CandidateBuilder,
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
    TopicRenderer,
)
from doc2agent.source_manifest import SourceManifest


def _candidate(tmp_path: Path):
    source = tmp_path / "registration.md"
    source.write_text(
        "<div dir=\"rtl\">\n\n"
        "# הרשמה\n\n"
        "כתובת אימייל לא תקינה מציגה הודעת שגיאה.\n\n"
        "</div>\n",
        encoding="utf-8",
    )
    document = StructuralAnalyzer().analyze(
        source,
        source_manifest=SourceManifest.from_path(source),
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
                id="fact:invalid-email",
                kind="requirement",
                value="כתובת אימייל לא תקינה מציגה הודעת שגיאה.",
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
    return CandidateBuilder().build(patch, document), block.id


def _snapshot() -> FigmaSnapshot:
    return FigmaSnapshot.build(
        figma_file_key="registration-file",
        figma_url="https://www.figma.com/design/registration-file/Registration",
        nodes=[
            FigmaNode(
                node_id="12:56",
                name="Registration — invalid email",
                kind="FRAME",
                figma_url=(
                    "https://www.figma.com/design/registration-file/Registration?node-id=12-56"
                ),
            )
        ],
        adapter="fixture",
        captured_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def test_snapshot_hash_is_stable_and_rejects_non_figma_url() -> None:
    first = _snapshot()
    second = _snapshot()

    assert first.snapshot_sha256 == second.snapshot_sha256
    with pytest.raises(ValidationError, match="figma.com"):
        FigmaNode(
            node_id="12:56",
            name="Registration",
            kind="FRAME",
            figma_url="https://example.com/design/registration",
        )


def test_alignment_requires_annotation_evidence_and_selected_figma_node(tmp_path: Path) -> None:
    candidate, block_id = _candidate(tmp_path)
    snapshot = _snapshot()
    binding = DesignBinding(
        annotation_id="fact:invalid-email",
        evidence_block_id=block_id,
        figma_node_id="12:56",
        status="matches",
    )

    alignment = DesignAlignmentBuilder().build(candidate, snapshot, [binding])

    assert alignment.candidate_sha256 == candidate.candidate_sha256
    assert alignment.figma_snapshot_sha256 == snapshot.snapshot_sha256
    with pytest.raises(ValueError, match="unknown Figma node"):
        DesignAlignmentBuilder().build(
            candidate,
            snapshot,
            [binding.model_copy(update={"figma_node_id": "unknown"})],
        )
    with pytest.raises(ValueError, match="not attached to annotation"):
        DesignAlignmentBuilder().build(
            candidate,
            snapshot,
            [binding.model_copy(update={"evidence_block_id": "block:unknown"})],
        )


def test_design_approval_requires_the_approved_knowledge_snapshot(tmp_path: Path) -> None:
    candidate, block_id = _candidate(tmp_path)
    snapshot = _snapshot()
    alignment = DesignAlignmentBuilder().build(
        candidate,
        snapshot,
        [
            DesignBinding(
                annotation_id="fact:invalid-email",
                evidence_block_id=block_id,
                figma_node_id="12:56",
                status="matches",
            )
        ],
    )
    storage = tmp_path / "storage"
    approval = DesignAlignmentApprovalService(storage)

    with pytest.raises(ValueError, match="Copilot context requires an approved snapshot"):
        approval.approve(
            alignment,
            candidate,
            snapshot,
            document_id="document:registration",
            approved_by="reviewer",
            knowledge_path=tmp_path / "unapproved.knowledge.json",
        )

    knowledge_path = ApprovalService(storage).approve(
        candidate,
        document_id="document:registration",
        approved_by="reviewer",
    )
    current_path = approval.approve(
        alignment,
        candidate,
        snapshot,
        document_id="document:registration",
        approved_by="reviewer",
        knowledge_path=knowledge_path,
        approved_at=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert current_path.is_file()
    assert approval.load_current("document:registration").alignment == alignment


def test_renderer_uses_only_approved_design_alignment(tmp_path: Path) -> None:
    candidate, block_id = _candidate(tmp_path)
    snapshot = _snapshot()
    alignment = DesignAlignmentBuilder().build(
        candidate,
        snapshot,
        [
            DesignBinding(
                annotation_id="fact:invalid-email",
                evidence_block_id=block_id,
                figma_node_id="12:56",
                status="matches",
            )
        ],
    )
    storage = tmp_path / "storage"
    knowledge_path = ApprovalService(storage).approve(
        candidate,
        document_id="document:registration",
        approved_by="reviewer",
    )
    approved_path = DesignAlignmentApprovalService(storage).approve(
        alignment,
        candidate,
        snapshot,
        document_id="document:registration",
        approved_by="reviewer",
        knowledge_path=knowledge_path,
        approved_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    approved_alignment = DesignAlignmentApprovalService(storage).load_current(
        "document:registration"
    )

    result = TopicRenderer().render(
        candidate,
        tmp_path / "copilot-context",
        design_alignment=approved_alignment,
    )
    markdown = result.context_path.read_text(encoding="utf-8")
    manifest = result.manifest_path.read_text(encoding="utf-8")

    assert approved_path.is_file()
    assert "## Approved Figma designs" in markdown
    assert "Registration — invalid email" in markdown
    assert "Figma node: `12:56`" in markdown
    assert alignment.alignment_sha256 in manifest
