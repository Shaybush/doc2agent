from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.application import DocumentProcessingService
from doc2agent.design import (
    DesignAlignmentApprovalService,
    DesignAlignmentBuilder,
    DesignAlignmentStore,
    DesignBinding,
    FigmaCaptureNode,
    FigmaImportRequestStore,
    FigmaMcpCaptureManifest,
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


def test_figma_save_requires_every_snapshot_frame_to_be_mapped(tmp_path: Path) -> None:
    candidate, block_id = _candidate(tmp_path)
    snapshot = FigmaSnapshot.build(
        figma_file_key="registration-file",
        figma_url="https://www.figma.com/design/registration-file/Registration",
        nodes=[
            FigmaNode(
                node_id="12:56",
                name="Registration",
                kind="FRAME",
                figma_url="https://www.figma.com/design/registration-file/Registration?node-id=12-56",
            ),
            FigmaNode(
                node_id="12:80",
                name="Invalid email",
                kind="FRAME",
                figma_url="https://www.figma.com/design/registration-file/Registration?node-id=12-80",
            ),
        ],
        adapter="fixture",
        captured_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    draft = DesignAlignmentBuilder().build(
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

    assert DocumentProcessingService._unmapped_node_ids(snapshot, [draft]) == {"12:80"}


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


def test_figma_import_request_is_source_scoped_and_idempotent(tmp_path: Path) -> None:
    source_id = "sha256:" + "b" * 64
    store = FigmaImportRequestStore(tmp_path / "storage")

    first = store.create(source_id, "https://www.figma.com/design/file-key/Registration")
    second = store.create(source_id, "https://www.figma.com/design/file-key/Registration")

    assert first == second
    assert store.list(source_id) == [first]
    snapshot = _snapshot()
    request = store.create(
        source_id,
        "https://www.figma.com/design/registration-file/Registration",
    )
    assert store.assert_can_fulfill(source_id, request.request_sha256, snapshot) == request
    fulfilled = store.fulfill(source_id, request.request_sha256, snapshot)
    assert fulfilled.status == "captured"
    assert fulfilled.snapshot_sha256 == snapshot.snapshot_sha256
    with pytest.raises(ValueError, match="different Figma file"):
        store.assert_can_fulfill(
            source_id,
            request.request_sha256,
            snapshot.model_copy(update={"figma_file_key": "other-file"}),
        )
    with pytest.raises(ValueError, match="figma.com"):
        store.create(source_id, "https://example.com/not-figma")


def test_figma_mcp_capture_manifest_builds_one_snapshot_for_multiple_nodes() -> None:
    capture = FigmaMcpCaptureManifest(
        figma_url="https://www.figma.com/design/registration-file/Registration?node-id=12-56",
        captured_at=datetime(2026, 9, 4, tzinfo=UTC),
        nodes=[
            FigmaCaptureNode(node_id="12:56", name="Registration", kind="FRAME"),
            FigmaCaptureNode(node_id="12:80", name="Invalid email", kind="FRAME"),
        ],
    )

    snapshot = capture.build_snapshot()

    assert snapshot.figma_file_key == "registration-file"
    assert [node.node_id for node in snapshot.nodes] == ["12:56", "12:80"]
    assert snapshot.nodes[1].figma_url.endswith("node-id=12-80")


def test_capture_command_completes_request_with_multiple_figma_nodes(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    source = Path(__file__).parent / "fixtures" / "hebrew_test_document.docx"
    ingested = IngestService(storage).ingest(source, approve_review=True)
    assert ingested.accepted
    request = FigmaImportRequestStore(storage).create(
        ingested.manifest.source_id,
        "https://www.figma.com/design/registration-file/Registration",
    )
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(
        FigmaMcpCaptureManifest(
            figma_url=request.figma_url,
            captured_at=datetime(2026, 9, 4, tzinfo=UTC),
            nodes=[
                FigmaCaptureNode(node_id="12:56", name="Registration", kind="FRAME"),
                FigmaCaptureNode(node_id="12:80", name="Invalid email", kind="FRAME"),
            ],
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "capture-figma-import-request",
            str(capture_path),
            "--source-id",
            ingested.manifest.source_id,
            "--request-sha256",
            request.request_sha256,
            "--storage",
            str(storage),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "(captured)" in result.output
    completed = FigmaImportRequestStore(storage).list(ingested.manifest.source_id)[0]
    snapshot = FigmaSnapshotStore(storage).load(
        ingested.manifest.source_id, completed.snapshot_sha256 or ""
    )
    assert [node.node_id for node in snapshot.nodes] == ["12:56", "12:80"]


def test_pending_figma_capture_queue_excludes_completed_requests(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    first_source_id = "sha256:" + "a" * 64
    second_source_id = "sha256:" + "b" * 64
    store = FigmaImportRequestStore(storage)
    pending = store.create(
        first_source_id, "https://www.figma.com/design/pending-file/Pending"
    )
    captured = store.create(
        second_source_id, "https://www.figma.com/design/registration-file/Registration"
    )
    store.fulfill(second_source_id, captured.request_sha256, _snapshot())

    result = CliRunner().invoke(
        cli.app,
        ["list-pending-figma-import-requests", "--storage", str(storage)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [pending.model_dump(mode="json")]


def test_cli_completes_registered_figma_import_request(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    source = Path(__file__).parent / "fixtures" / "hebrew_test_document.docx"
    ingested = IngestService(storage).ingest(source, approve_review=True)
    assert ingested.accepted
    snapshot = _snapshot()
    request = FigmaImportRequestStore(storage).create(
        ingested.manifest.source_id,
        "https://www.figma.com/design/registration-file/Registration",
    )
    snapshot_path = tmp_path / "snapshot.figma.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "complete-figma-import-request",
            str(snapshot_path),
            "--source-id",
            ingested.manifest.source_id,
            "--request-sha256",
            request.request_sha256,
            "--storage",
            str(storage),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "(captured)" in result.output
    completed = FigmaImportRequestStore(storage).list(ingested.manifest.source_id)[0]
    assert completed.status == "captured"


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
    assert DesignAlignmentStore(storage).list(candidate.source_id)[0].candidate_sha256 == (
        candidate.candidate_sha256
    )


def test_cli_approves_alignment_and_renders_figma_context(tmp_path: Path) -> None:
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
    knowledge_path = ApprovalService(storage).approve(
        candidate,
        document_id="document:registration",
        approved_by="reviewer",
    )
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
    alignment_path = tmp_path / "alignment.design-alignment.json"
    alignment_path.write_text(alignment.model_dump_json(indent=2), encoding="utf-8")

    approved = CliRunner().invoke(
        cli.app,
        [
            "approve-design-alignment",
            str(alignment_path),
            "--approved",
            str(knowledge_path),
            "--by",
            "reviewer",
            "--storage",
            str(storage),
        ],
    )

    assert approved.exit_code == 0, approved.output
    approved_design = DesignAlignmentApprovalService(storage).current_path(
        "document:registration"
    )
    rendered = CliRunner().invoke(
        cli.app,
        [
            "render-copilot",
            "--approved",
            str(knowledge_path),
            "--storage",
            str(storage),
            "--design-alignment",
            str(approved_design),
        ],
    )

    assert rendered.exit_code == 0, rendered.output
    context = storage / "copilot-context/registration--document-registration/registration.md"
    assert "## Approved Figma designs" in context.read_text(encoding="utf-8")


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
