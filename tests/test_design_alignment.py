from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.design import (
    DesignAlignmentApprovalService,
    DesignAlignmentBuilder,
    DesignBinding,
    FigmaNode,
    FigmaSnapshot,
    FigmaSnapshotStore,
)
from doc2agent.ingest import IngestService
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


def test_snapshot_store_is_source_revision_scoped_and_idempotent(tmp_path: Path) -> None:
    snapshot = _snapshot()
    source_id = "sha256:" + "a" * 64
    store = FigmaSnapshotStore(tmp_path / "storage")

    path = store.write(source_id, snapshot)

    assert path == (
        tmp_path
        / "storage"
        / "artifacts"
        / ("sha256-" + "a" * 64)
        / "figma"
        / f"{snapshot.snapshot_sha256}.figma.json"
    )
    assert store.write(source_id, snapshot) == path
    assert store.load(source_id, snapshot.snapshot_sha256) == snapshot
    with pytest.raises(ValueError, match="Source ID"):
        store.write("source:unknown", snapshot)


def test_cli_imports_snapshot_only_for_a_registered_source(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    source = Path(__file__).parent / "fixtures" / "hebrew_test_document.docx"
    ingested = IngestService(storage).ingest(source, approve_review=True)
    assert ingested.accepted
    snapshot = _snapshot()
    snapshot_path = tmp_path / "snapshot.figma.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "import-figma-snapshot",
            str(snapshot_path),
            "--source-id",
            ingested.manifest.source_id,
            "--storage",
            str(storage),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Snapshot hash:" in result.output
    assert FigmaSnapshotStore(storage).load(
        ingested.manifest.source_id,
        snapshot.snapshot_sha256,
    ) == snapshot


def test_cli_creates_design_alignment_from_explicit_bindings(tmp_path: Path) -> None:
    candidate, block_id = _candidate(tmp_path)
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "source-catalog.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sources": {
                    candidate.source_id: {
                        "source_id": candidate.source_id,
                        "document_id": "document:registration",
                        "filename": "registration.md",
                        "source_path": "storage/sources/registration.md",
                        "received_at": "2026-09-04T00:00:00+00:00",
                    }
                },
                "documents": {},
            }
        ),
        encoding="utf-8",
    )
    snapshot = _snapshot()
    FigmaSnapshotStore(storage).write(candidate.source_id, snapshot)
    candidate_path = tmp_path / "candidate.knowledge.json"
    candidate_path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text(
        json.dumps(
            [
                {
                    "annotation_id": "fact:invalid-email",
                    "evidence_block_id": block_id,
                    "figma_node_id": "12:56",
                    "status": "matches",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "create-design-alignment",
            str(bindings_path),
            "--candidate",
            str(candidate_path),
            "--snapshot-sha256",
            snapshot.snapshot_sha256,
            "--storage",
            str(storage),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Alignment hash:" in result.output
    alignment_paths = list(
        (storage / "artifacts" / candidate.source_id.replace(":", "-") / "design-alignment").glob(
            "*.design-alignment.json"
        )
    )
    assert len(alignment_paths) == 1


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
