from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from doc2agent.design import FigmaNode, FigmaSnapshot, FigmaSnapshotStore
from doc2agent.design.import_request import FigmaImportRequestStore
from doc2agent.ingest import IngestDecision, IngestService


def _write_docx(
    path: Path,
    *,
    extra_entries: dict[str, bytes] | None = None,
) -> None:
    entries = {
        "[Content_Types].xml": b"<Types/>",
        "word/document.xml": b"<w:document xmlns:w='urn:test'/>",
    }
    entries.update(extra_entries or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_ingest_stores_valid_docx_by_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)

    result = IngestService(storage).ingest(source)

    assert result.accepted is True
    assert result.duplicate is False
    assert result.source_path is not None
    assert result.source_path.read_bytes() == source.read_bytes()
    assert result.manifest_path is not None
    assert result.manifest_path.exists()

    catalog = json.loads(
        (storage / "source-catalog.json").read_text(encoding="utf-8")
    )
    assert catalog["sources"][result.manifest.source_id]["document_id"] == result.document_id
    assert (
        catalog["documents"][result.document_id]["current_source_id"]
        == result.manifest.source_id
    )


def test_ingest_reuses_identical_source_without_overwriting_it(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first.docx"
    second_source = tmp_path / "second.docx"
    storage = tmp_path / "storage"
    _write_docx(first_source)
    second_source.write_bytes(first_source.read_bytes())

    service = IngestService(storage)
    first = service.ingest(first_source)
    stored_bytes = first.source_path.read_bytes()
    second = service.ingest(second_source)

    assert second.accepted is True
    assert second.duplicate is True
    assert second.manifest.source_id == first.manifest.source_id
    assert second.document_id == first.document_id
    assert second.source_path.read_bytes() == stored_bytes


def test_ingest_rejects_duplicate_content_for_a_different_document(
    tmp_path: Path,
) -> None:
    source = tmp_path / "requirements.docx"
    _write_docx(source)
    service = IngestService(tmp_path / "storage")

    first = service.ingest(source, document_id="document:requirements")

    with pytest.raises(ValueError, match="already belongs to a different document"):
        service.ingest(source, document_id="document:other")

    assert service.document_id_for_source(first.manifest.source_id) == first.document_id


def test_ingest_records_changed_source_as_new_version_automatically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)
    service = IngestService(storage)

    first = service.ingest(source)
    _write_docx(source, extra_entries={"word/comments.xml": b"<comments/>"})
    second = service.ingest(source)

    assert second.accepted is True
    assert second.duplicate is False
    assert second.manifest.source_id != first.manifest.source_id
    assert second.document_id == first.document_id
    assert first.source_path.exists()
    assert second.source_path.exists()

    catalog = json.loads(
        (storage / "source-catalog.json").read_text(encoding="utf-8")
    )
    document = catalog["documents"][first.document_id]
    assert document["source_ids"] == [first.manifest.source_id, second.manifest.source_id]
    assert document["current_source_id"] == second.manifest.source_id


def test_ingest_blocks_macros_without_storing_source(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.docx"
    storage = tmp_path / "storage"
    _write_docx(source, extra_entries={"word/vbaProject.bin": b"macro"})

    result = IngestService(storage).ingest(source)

    assert result.decision == IngestDecision.BLOCK
    assert result.source_path is None
    assert result.diagnostics[0].code == "MACROS_NOT_ALLOWED"
    assert not storage.exists()


def test_ingest_requires_review_for_external_relationship(tmp_path: Path) -> None:
    source = tmp_path / "external.docx"
    relationship = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Target="https://example.com" TargetMode="External"/>'
        b"</Relationships>"
    )
    _write_docx(source, extra_entries={"word/_rels/document.xml.rels": relationship})

    result = IngestService(tmp_path / "storage").ingest(source)

    assert result.decision == IngestDecision.REQUIRES_REVIEW
    assert result.diagnostics[0].code == "EXTERNAL_RELATIONSHIP"


def test_ingest_stores_explicitly_approved_external_relationship(
    tmp_path: Path,
) -> None:
    source = tmp_path / "external.docx"
    storage = tmp_path / "storage"
    relationship = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Target="https://example.com" TargetMode="External"/>'
        b"</Relationships>"
    )
    _write_docx(source, extra_entries={"word/_rels/document.xml.rels": relationship})
    service = IngestService(storage)

    approved = service.ingest(source, approve_review=True)
    reused = service.ingest(source)

    assert approved.accepted is True
    assert approved.review_approved is True
    assert approved.source_path.exists()
    assert reused.accepted is True
    assert reused.duplicate is True
    assert reused.review_approved is True
    catalog = json.loads(
        (storage / "source-catalog.json").read_text(encoding="utf-8")
    )
    entry = catalog["sources"][approved.manifest.source_id]
    assert entry["preflight_decision"] == "requires_review"
    assert entry["review_approved"] is True


def test_ingest_blocks_unsafe_archive_path(tmp_path: Path) -> None:
    source = tmp_path / "unsafe-path.docx"
    _write_docx(source, extra_entries={"../outside.txt": b"unsafe"})

    result = IngestService(tmp_path / "storage").ingest(source)

    assert result.decision == IngestDecision.BLOCK
    assert result.diagnostics[0].code == "UNSAFE_ARCHIVE_PATH"


def test_ingest_records_artifact_root_for_current_source(tmp_path: Path) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)
    service = IngestService(storage)
    ingested = service.ingest(source)
    artifact_root = storage / "artifacts" / ingested.manifest.source_id.replace(":", "-")

    service.record_artifact_root(ingested.manifest.source_id, artifact_root)

    catalog = json.loads(
        (storage / "source-catalog.json").read_text(encoding="utf-8")
    )
    assert catalog["sources"][ingested.manifest.source_id]["artifact_root"] == str(
        artifact_root
    )


def test_delete_document_previews_then_moves_all_versions_to_trash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)
    service = IngestService(storage)
    first = service.ingest(source)
    first_artifacts = storage / "artifacts" / first.manifest.source_id.replace(":", "-")
    first_artifacts.mkdir(parents=True)
    service.record_artifact_root(first.manifest.source_id, first_artifacts)

    _write_docx(source, extra_entries={"customXml/version.txt": b"second"})
    second = service.ingest(source)
    second_artifacts = storage / "artifacts" / second.manifest.source_id.replace(":", "-")
    second_artifacts.mkdir(parents=True)
    service.record_artifact_root(second.manifest.source_id, second_artifacts)
    approved_dir = service._approved_document_dir(first.document_id)
    approved_dir.mkdir(parents=True)
    (approved_dir / "current.knowledge.json").write_text("{}", encoding="utf-8")
    design_approved_dir = service._design_approved_document_dir(first.document_id)
    design_approved_dir.mkdir(parents=True)
    (design_approved_dir / "current.design-alignment.json").write_text(
        "{}", encoding="utf-8"
    )
    figma_request = FigmaImportRequestStore(storage).create(
        second.manifest.source_id, "https://www.figma.com/design/file-key/Example"
    )
    figma_request_dir = FigmaImportRequestStore(storage).path_for(
        second.manifest.source_id, figma_request.request_sha256
    ).parent
    version_root = storage / "copilot-context" / ".requirements--document-test-versions"
    version = version_root / "candidate-v5"
    version.mkdir(parents=True)
    (version / "manifest.json").write_text(
        json.dumps({"source_id": second.manifest.source_id}), encoding="utf-8"
    )
    context_root = storage / "copilot-context" / "requirements--document-test"
    context_root.parent.mkdir(parents=True, exist_ok=True)
    context_root.symlink_to(version, target_is_directory=True)
    approval_log = storage / "approval-log.jsonl"
    approval_log.write_text(
        '{"document_id": "document:other"}\n'
        f'{{"document_id": "{first.document_id}"}}\n'
        f'{{"source_id": "{second.manifest.source_id}"}}\n',
        encoding="utf-8",
    )

    preview = service.delete_document(first.document_id)

    assert preview.deleted is False
    assert first.source_path.exists()
    assert second.source_path.exists()
    assert first_artifacts.exists()
    assert second_artifacts.exists()
    assert design_approved_dir in preview.paths
    assert figma_request_dir in preview.paths

    deleted = service.delete_document(first.document_id, confirm=True)

    assert deleted.deleted is True
    assert deleted.trash_path is not None
    assert deleted.trash_path.exists()
    assert not first.source_path.exists()
    assert not second.source_path.exists()
    assert not first_artifacts.exists()
    assert not second_artifacts.exists()
    assert not approved_dir.exists()
    assert not design_approved_dir.exists()
    assert not figma_request_dir.exists()
    assert not context_root.exists()
    assert not version_root.exists()
    assert 'document:other' in approval_log.read_text(encoding="utf-8")
    assert first.document_id not in approval_log.read_text(encoding="utf-8")
    assert second.manifest.source_id not in approval_log.read_text(encoding="utf-8")
    assert (deleted.trash_path / "approval-log.jsonl").exists()
    catalog = json.loads(
        (storage / "source-catalog.json").read_text(encoding="utf-8")
    )
    assert catalog["documents"] == {}
    assert catalog["sources"] == {}


def test_delete_document_rollback_restores_relative_context_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)
    service = IngestService(storage)
    ingested = service.ingest(source)
    version_root = storage / "copilot-context" / ".requirements-versions"
    version = version_root / "candidate-v1"
    version.mkdir(parents=True)
    (version / "manifest.json").write_text(
        json.dumps({"source_id": ingested.manifest.source_id}), encoding="utf-8"
    )
    context_root = storage / "copilot-context" / "requirements"
    context_root.symlink_to(".requirements-versions/candidate-v1", target_is_directory=True)

    def fail_catalog_write(path: Path, payload: object) -> None:
        if path.name == "source-catalog.json":
            raise OSError("catalog write failed")

    monkeypatch.setattr(service, "_write_json_atomic", fail_catalog_write)

    with pytest.raises(OSError, match="catalog write failed"):
        service.delete_document(ingested.document_id, confirm=True)

    assert context_root.is_symlink()
    assert context_root.readlink() == Path(".requirements-versions/candidate-v1")
    assert context_root.exists()
    assert version.exists()


def test_delete_document_removes_figma_artifacts_without_registered_pipeline_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)
    service = IngestService(storage)
    ingested = service.ingest(source)
    snapshot = FigmaSnapshot.build(
        figma_file_key="requirements",
        figma_url="https://www.figma.com/design/requirements/Requirements",
        nodes=[
            FigmaNode(
                node_id="1:2",
                name="Requirements",
                kind="FRAME",
                figma_url="https://www.figma.com/design/requirements/Requirements?node-id=1-2",
            )
        ],
        adapter="fixture",
        captured_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    snapshot_path = FigmaSnapshotStore(storage).write(ingested.manifest.source_id, snapshot)
    artifact_root = snapshot_path.parents[1]

    preview = service.delete_document(ingested.document_id)
    assert artifact_root in preview.paths

    service.delete_document(ingested.document_id, confirm=True)
    assert not artifact_root.exists()


def test_record_artifact_root_refuses_non_isolated_path(tmp_path: Path) -> None:
    source = tmp_path / "requirements.docx"
    storage = tmp_path / "storage"
    _write_docx(source)
    service = IngestService(storage)
    ingested = service.ingest(source)
    shared_artifacts = storage / "artifacts"

    with pytest.raises(ValueError, match="isolated storage directory"):
        service.record_artifact_root(ingested.manifest.source_id, shared_artifacts)
