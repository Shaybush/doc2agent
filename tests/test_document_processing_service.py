from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.application import (
    DocumentProcessingError,
    DocumentProcessingService,
    ProcessingResult,
)
from doc2agent.application import document_processing_service as service_module
from doc2agent.models.document import AnalyzedDocument
from doc2agent.security import SecurityGateway
from doc2agent.semantic import EnrichmentPatch, EnrichmentPatchValidator, EvidenceRef, PatchFact
from doc2agent.source_manifest import SourceManifest
from doc2agent.validator import ValidationReport


def test_process_forwards_external_relationship_review_approval(tmp_path: Path) -> None:
    source = tmp_path / "external.docx"
    source.write_bytes(b"document bytes")
    manifest = SourceManifest.from_path(source)
    approvals: list[bool] = []

    class RecordingIngestService:
        def ingest(
            self,
            received_source: Path,
            *,
            document_id: str | None = None,
            approve_review: bool = False,
        ) -> SimpleNamespace:
            assert received_source == source
            assert document_id == "document:external"
            approvals.append(approve_review)
            return SimpleNamespace(
                accepted=True,
                source_path=source,
                manifest=manifest,
                document_id=document_id,
            )

    class StoppingPipeline:
        def run(self, *_: object) -> object:
            raise DocumentProcessingError("Test", "stop after intake")

    service = DocumentProcessingService(
        tmp_path / "storage",
        ingest_service=RecordingIngestService(),  # type: ignore[arg-type]
        pipeline=StoppingPipeline(),  # type: ignore[arg-type]
    )

    with pytest.raises(DocumentProcessingError, match="stop after intake"):
        service.process(source, document_id="document:external", approve_review=True)

    assert approvals == [True]


def test_generate_candidate_uses_sanitized_document_for_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "requirements.md"
    source.write_text(
        "# Requirements\n\nContact alice@example.com for approval.\n",
        encoding="utf-8",
    )
    document = StructuralAnalyzer().analyze(
        source,
        source_manifest=SourceManifest.from_path(source),
    )
    security = SecurityGateway().process(
        document.model_dump(mode="json", exclude_none=True)
    )
    sanitized_document = AnalyzedDocument.model_validate(security.sanitized_document)
    block = sanitized_document.sections[0].blocks[0]
    assert block.text == "Contact [REDACTED_EMAIL] for approval."

    artifact_root = (
        tmp_path
        / "storage"
        / "artifacts"
        / document.source_manifest.source_id.replace(":", "-")
    )
    structured = artifact_root / "structured"
    validation = artifact_root / "validation"
    security_dir = artifact_root / "security"
    for directory in (structured, validation, security_dir):
        directory.mkdir(parents=True)
    (structured / "requirements.document.json").write_text(
        document.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    (validation / "requirements.validation.json").write_text(
        ValidationReport(valid=True).model_dump_json(indent=2), encoding="utf-8"
    )
    (security_dir / "requirements.security.json").write_text(
        security.model_dump_json(indent=2), encoding="utf-8"
    )

    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:approval-contact",
                kind="requirement",
                value="Contact [REDACTED_EMAIL] for approval.",
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

    class StubProvider:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def create_patch(self, supplied_document):
            assert supplied_document == sanitized_document
            return patch

    monkeypatch.setattr(
        service_module,
        "deepseek_settings_from_environment",
        lambda: object(),
    )
    monkeypatch.setattr(service_module, "OpenAICompatibleEnrichmentProvider", StubProvider)

    result = ProcessingResult(
        filename=source.name,
        document_id="document:requirements",
        source_id=document.source_manifest.source_id,
        status="ready_with_redactions",
        document_json=security.sanitized_document,
    )
    proof = DocumentProcessingService(tmp_path / "storage").generate_candidate(result)

    candidate_path = next((artifact_root / "candidate").glob("*.knowledge.json"))
    candidate = service_module.KnowledgeCandidate.model_validate_json(
        candidate_path.read_text(encoding="utf-8")
    )
    assert candidate.source_id == document.source_manifest.source_id
    assert candidate.structural_sha256 == document.structural_sha256
    assert candidate.document == sanitized_document
    assert candidate.reviewed_block_ids == proof.reviewed_block_ids
    assert proof.facts[0].evidence[0].source_text == block.text
