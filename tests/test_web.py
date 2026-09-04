from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from doc2agent.application import (
    CandidateProof,
    DesignAlignmentApprovalPreview,
    DesignAlignmentBindingPreview,
    DesignAlignmentDraftPreview,
    DocumentProcessingError,
    DocumentProcessingService,
    FigmaImportRequestPreview,
    FigmaNodePreview,
    FigmaSnapshotPreview,
    KnowledgeApprovalPreview,
    ProcessingResult,
    ProofEvidence,
    ProofFact,
    UnmappedFigmaNodePreview,
)
from doc2agent.web.app import create_app


class StubProcessingService:
    def __init__(self, result: ProcessingResult | Exception) -> None:
        self.result = result
        self.sources: list[Path] = []
        self.document_ids: list[str | None] = []
        self.review_approvals: list[bool] = []
        self.figma_import_requests: list[FigmaImportRequestPreview] = []
        self.knowledge_approval: KnowledgeApprovalPreview | None = None
        self.design_alignment_approval: DesignAlignmentApprovalPreview | None = None

    def process(
        self,
        source: Path,
        *,
        document_id: str | None = None,
        approve_review: bool = False,
    ) -> ProcessingResult:
        self.sources.append(source)
        self.document_ids.append(document_id)
        self.review_approvals.append(approve_review)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def generate_candidate(self, result: ProcessingResult) -> CandidateProof:
        return CandidateProof(
            candidate_sha256="b" * 64,
            reviewed_block_ids=["block:reviewed"],
            facts=[
                ProofFact(
                    annotation_id="fact:explicit-requirement",
                    kind="requirement",
                    value="Explicit requirement",
                    evidence=[
                        ProofEvidence(
                            block_id="block:reviewed",
                            support="explicit",
                            section_path=["Requirements"],
                            start_line=4,
                            end_line=4,
                            source_text="Explicit requirement",
                        )
                    ],
                )
            ],
        )

    def approve_and_publish(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        approved_by: str,
    ) -> str:
        assert approved_by == "reviewer"
        assert proof.candidate_sha256 == "b" * 64
        return "# Approved knowledge\n\n- Explicit requirement\n"

    def approve_and_publish_with_figma(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        approved_by: str,
    ) -> str:
        assert approved_by == "reviewer"
        assert proof.candidate_sha256 == "b" * 64
        return "# Approved knowledge with Figma\n\n- Explicit requirement\n"

    def approve_knowledge(
        self, result: ProcessingResult, proof: CandidateProof, approved_by: str
    ) -> KnowledgeApprovalPreview:
        assert approved_by == "reviewer"
        self.knowledge_approval = KnowledgeApprovalPreview(
            candidate_sha256=proof.candidate_sha256
        )
        return self.knowledge_approval

    def current_knowledge_approval(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> KnowledgeApprovalPreview | None:
        return self.knowledge_approval

    def approve_design_alignment(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        alignment_sha256: str,
        approved_by: str,
    ) -> DesignAlignmentApprovalPreview:
        assert approved_by == "reviewer"
        self.design_alignment_approval = DesignAlignmentApprovalPreview(
            alignment_sha256=alignment_sha256,
            figma_snapshot_sha256="c" * 64,
        )
        return self.design_alignment_approval

    def current_design_alignment_approval(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> DesignAlignmentApprovalPreview | None:
        return self.design_alignment_approval

    def publish_copilot_with_design(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> str:
        assert self.knowledge_approval is not None
        assert self.design_alignment_approval is not None
        return "# Approved knowledge with Figma\n\n- Explicit requirement\n"

    def list_figma_snapshots(
        self, result: ProcessingResult,
    ) -> list[FigmaSnapshotPreview]:
        return []

    def request_figma_capture(
        self, result: ProcessingResult, figma_url: str
    ) -> FigmaImportRequestPreview:
        preview = FigmaImportRequestPreview(
            request_sha256="d" * 64,
            figma_url=figma_url,
            requested_at="2026-09-04T00:00:00+00:00",
            status="pending",
        )
        self.figma_import_requests = [preview]
        return preview

    def list_figma_import_requests(
        self, result: ProcessingResult
    ) -> list[FigmaImportRequestPreview]:
        return self.figma_import_requests

    def create_design_alignment_draft(self, *args, **kwargs) -> DesignAlignmentDraftPreview:
        return DesignAlignmentDraftPreview(
            alignment_sha256="e" * 64,
            figma_snapshot_sha256="c" * 64,
            bindings=[
                DesignAlignmentBindingPreview(
                    annotation_id="fact:explicit-requirement",
                    evidence_block_id="block:reviewed",
                    figma_node_id="2:15",
                    status="matches",
                )
            ],
        )

    def create_design_alignment_package(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        alignment_sha256s: list[str],
    ) -> DesignAlignmentDraftPreview:
        assert len(alignment_sha256s) >= 2
        return DesignAlignmentDraftPreview(
            alignment_sha256="g" * 64,
            figma_snapshot_sha256="c" * 64,
            bindings=[
                DesignAlignmentBindingPreview(
                    annotation_id="fact:explicit-requirement",
                    evidence_block_id="block:reviewed",
                    figma_node_id="2:15",
                    status="matches",
                ),
                DesignAlignmentBindingPreview(
                    annotation_id="fact:another-requirement",
                    evidence_block_id="block:another",
                    figma_node_id="2:16",
                    status="partial",
                ),
            ],
        )

    def list_design_alignment_drafts(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[DesignAlignmentDraftPreview]:
        return []

    def list_reusable_design_alignment_drafts(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[DesignAlignmentDraftPreview]:
        return []

    def list_unmapped_figma_nodes(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[UnmappedFigmaNodePreview]:
        return []

    def reuse_design_alignment_draft(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        alignment_sha256: str,
    ) -> DesignAlignmentDraftPreview:
        return self.create_design_alignment_draft(result, proof)


def _result() -> ProcessingResult:
    return ProcessingResult(
        filename="example.docx",
        document_id="document:example",
        source_id="sha256:" + "a" * 64,
        status="ready",
        markdown="# Example\n\nReady for Copilot.",
        document_json={"schema_version": "1.0", "document": {"title": "Example"}},
    )


def test_index_returns_upload_page() -> None:
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert "Choose DOC/DOCX" in response.text
    assert "Links will not be opened or fetched." in response.text


def test_valid_docx_upload_returns_markdown_json_and_downloads() -> None:
    service = StubProcessingService(_result())
    client = TestClient(create_app(service=service))

    response = client.post(
        "/documents/process",
        files={"file": ("../../example.docx", b"document bytes")},
    )

    assert response.status_code == 200
    assert "Ready for Copilot." in response.text
    assert '"schema_version": "1.0"' in response.text
    assert service.sources[0].name == "example.docx"
    assert service.review_approvals == [False]
    artifact_id = next(iter(client.app.state.processed_artifacts._items))

    markdown = client.get(f"/documents/{artifact_id}/markdown")
    document_json = client.get(f"/documents/{artifact_id}/json")

    assert markdown.status_code == 200
    assert markdown.text == "# Example\n\nReady for Copilot."
    assert document_json.status_code == 200
    assert document_json.json()["document"]["title"] == "Example"


def test_json_preview_and_download_preserve_hebrew_text() -> None:
    result = _result().model_copy(
        update={"document_json": {"document": {"title": "מסמך בדיקה"}}}
    )
    client = TestClient(create_app(service=StubProcessingService(result)))

    response = client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )
    assert "מסמך בדיקה" in response.text
    assert "\\u05de" not in response.text

    artifact_id = next(iter(client.app.state.processed_artifacts._items))
    downloaded = client.get(f"/documents/{artifact_id}/json")
    assert "מסמך בדיקה" in downloaded.text
    assert "\\u05de" not in downloaded.text


def test_published_copilot_markdown_is_available_only_when_present() -> None:
    result = _result().model_copy(update={"published_copilot_markdown": "# Approved knowledge\n"})
    client = TestClient(create_app(service=StubProcessingService(result)))

    response = client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )

    assert "Published Copilot Markdown" in response.text
    artifact_id = next(iter(client.app.state.processed_artifacts._items))
    published = client.get(f"/documents/{artifact_id}/copilot-markdown")
    assert published.status_code == 200
    assert published.text == "# Approved knowledge\n"


def test_candidate_proof_can_be_reviewed_then_approved_and_published() -> None:
    client = TestClient(create_app(service=StubProcessingService(_result())))
    processed = client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )
    assert processed.status_code == 200
    artifact_id = next(iter(client.app.state.processed_artifacts._items))

    review = client.post(f"/documents/{artifact_id}/candidate")
    assert review.status_code == 200
    assert "Candidate review / proof" in review.text
    assert "Reviewed content blocks: <code>block:reviewed</code>" in review.text
    assert "lines 4–4" in review.text

    published = client.post(
        f"/documents/{artifact_id}/approve",
        data={"approved_by": "reviewer"},
    )
    assert published.status_code == 200
    assert "Published Copilot Markdown" in published.text
    assert "Explicit requirement" in published.text


def test_new_candidate_is_shown_as_not_yet_approved_not_an_approval_error() -> None:
    service = DocumentProcessingService(storage_root="unused-for-this-unit-test")
    proof = StubProcessingService(_result()).generate_candidate(_result())

    with (
        patch.object(service, "_candidate_for_proof", return_value=object()),
        patch.object(
            service,
            "_require_current_knowledge",
            side_effect=ValueError("Current approved knowledge belongs to a different candidate"),
        ),
    ):
        assert service.current_knowledge_approval(_result(), proof) is None


def test_candidate_review_displays_imported_figma_frames() -> None:
    class FigmaAwareProcessingService(StubProcessingService):
        def list_figma_snapshots(
            self, result: ProcessingResult
        ) -> list[FigmaSnapshotPreview]:
            return [
                FigmaSnapshotPreview(
                    snapshot_sha256="c" * 64,
                    figma_url="https://www.figma.com/design/file-key/Registration",
                    captured_at="2026-09-04T00:00:00+00:00",
                    nodes=[
                        FigmaNodePreview(
                            node_id="2:15",
                            name="אימייל לא תקין",
                            kind="FRAME",
                            figma_url=(
                                "https://www.figma.com/design/file-key/Registration?node-id=2-15"
                            ),
                        )
                    ],
                )
            ]

    client = TestClient(create_app(service=FigmaAwareProcessingService(_result())))
    client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )
    artifact_id = next(iter(client.app.state.processed_artifacts._items))

    review = client.post(f"/documents/{artifact_id}/candidate")

    assert review.status_code == 200
    assert "Figma snapshots" in review.text
    assert "אימייל לא תקין" in review.text
    assert "2:15" in review.text
    assert "Create mapping draft" in review.text
    assert 'class="mapping-details"' in review.text
    assert 'name="binding_choice"' in review.text
    assert 'select name="figma_node_id"' in review.text


def test_candidate_review_can_request_a_figma_snapshot() -> None:
    service = StubProcessingService(_result())
    client = TestClient(create_app(service=service))
    client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )
    artifact_id = next(iter(client.app.state.processed_artifacts._items))
    client.post(f"/documents/{artifact_id}/candidate")

    response = client.post(
        f"/documents/{artifact_id}/figma-import-request",
        data={"figma_url": "https://www.figma.com/design/file-key/Registration"},
    )

    assert response.status_code == 200
    assert "Request Figma snapshot" in response.text
    assert "https://www.figma.com/design/file-key/Registration" in response.text


def test_candidate_review_creates_a_figma_mapping_draft() -> None:
    class MappingProcessingService(StubProcessingService):
        def __init__(self, result: ProcessingResult | Exception) -> None:
            super().__init__(result)
            self.drafts: list[DesignAlignmentDraftPreview] = []

        def list_figma_snapshots(
            self, result: ProcessingResult
        ) -> list[FigmaSnapshotPreview]:
            return [
                FigmaSnapshotPreview(
                    snapshot_sha256="c" * 64,
                    figma_url="https://www.figma.com/design/file-key/Registration",
                    captured_at="2026-09-04T00:00:00+00:00",
                    nodes=[
                        FigmaNodePreview(
                            node_id="2:15",
                            name="אימייל לא תקין",
                            kind="FRAME",
                            figma_url=(
                                "https://www.figma.com/design/file-key/Registration?node-id=2-15"
                            ),
                        )
                    ],
                )
            ]

        def create_design_alignment_draft(self, *args, **kwargs) -> DesignAlignmentDraftPreview:
            preview = super().create_design_alignment_draft(*args, **kwargs)
            self.drafts = [preview]
            return preview

        def list_design_alignment_drafts(
            self, result: ProcessingResult, proof: CandidateProof
        ) -> list[DesignAlignmentDraftPreview]:
            return self.drafts

    client = TestClient(create_app(service=MappingProcessingService(_result())))
    client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )
    artifact_id = next(iter(client.app.state.processed_artifacts._items))
    client.post(f"/documents/{artifact_id}/candidate")

    response = client.post(
        f"/documents/{artifact_id}/design-alignment",
        data={
            "snapshot_sha256": "c" * 64,
            "annotation_id": "fact:explicit-requirement",
            "evidence_block_id": "block:reviewed",
            "figma_node_id": "2:15",
            "status": "matches",
        },
    )

    assert response.status_code == 200
    assert "Current Figma mappings" in response.text
    assert "fact:explicit-requirement" in response.text


def test_mapping_draft_uses_one_requirement_evidence_pair() -> None:
    class PairProcessingService(StubProcessingService):
        def list_figma_snapshots(
            self, result: ProcessingResult
        ) -> list[FigmaSnapshotPreview]:
            return [
                FigmaSnapshotPreview(
                    snapshot_sha256="c" * 64,
                    figma_url="https://www.figma.com/design/file-key/Registration",
                    captured_at="2026-09-04T00:00:00+00:00",
                    nodes=[],
                )
            ]

        def create_design_alignment_draft(self, *args, **kwargs) -> DesignAlignmentDraftPreview:
            assert kwargs["annotation_id"] == "fact:explicit-requirement"
            assert kwargs["evidence_block_id"] == "block:reviewed"
            return super().create_design_alignment_draft(*args, **kwargs)

    client = TestClient(create_app(service=PairProcessingService(_result())))
    client.post("/documents/process", files={"file": ("example.docx", b"document bytes")})
    artifact_id = next(iter(client.app.state.processed_artifacts._items))
    client.post(f"/documents/{artifact_id}/candidate")

    response = client.post(
        f"/documents/{artifact_id}/design-alignment",
        data={
            "snapshot_sha256": "c" * 64,
            "binding_choice": '["fact:explicit-requirement", "block:reviewed"]',
            "figma_node_id": "2:15",
            "status": "matches",
        },
    )

    assert response.status_code == 200


def test_candidate_review_hides_advanced_reuse_flow() -> None:
    class ReuseProcessingService(StubProcessingService):
        def __init__(self, result: ProcessingResult | Exception) -> None:
            super().__init__(result)
            self.reused: list[str] = []

        def list_figma_snapshots(
            self, result: ProcessingResult
        ) -> list[FigmaSnapshotPreview]:
            return [
                FigmaSnapshotPreview(
                    snapshot_sha256="c" * 64,
                    figma_url="https://www.figma.com/design/file-key/Registration",
                    captured_at="2026-09-04T00:00:00+00:00",
                    nodes=[],
                )
            ]

        def list_reusable_design_alignment_drafts(
            self, result: ProcessingResult, proof: CandidateProof
        ) -> list[DesignAlignmentDraftPreview]:
            if self.reused:
                return []
            return [
                DesignAlignmentDraftPreview(
                    alignment_sha256="e" * 64,
                    figma_snapshot_sha256="c" * 64,
                    source_candidate_sha256="d" * 64,
                    bindings=[],
                )
            ]

        def reuse_design_alignment_draft(
            self,
            result: ProcessingResult,
            proof: CandidateProof,
            alignment_sha256: str,
        ) -> DesignAlignmentDraftPreview:
            self.reused.append(alignment_sha256)
            return super().reuse_design_alignment_draft(result, proof, alignment_sha256)

    service = ReuseProcessingService(_result())
    client = TestClient(create_app(service=service))
    client.post("/documents/process", files={"file": ("example.docx", b"document bytes")})
    artifact_id = next(iter(client.app.state.processed_artifacts._items))

    review = client.post(f"/documents/{artifact_id}/candidate")
    assert "Reusable Figma mapping drafts" not in review.text
    assert "Reuse for current candidate" not in review.text


def test_figma_review_approves_and_saves_markdown_in_one_action() -> None:
    class FigmaApprovalProcessingService(StubProcessingService):
        def __init__(self, result: ProcessingResult | Exception) -> None:
            super().__init__(result)
            self.drafts = [
                DesignAlignmentDraftPreview(
                    alignment_sha256="e" * 64,
                    figma_snapshot_sha256="c" * 64,
                    bindings=[
                        DesignAlignmentBindingPreview(
                            annotation_id="fact:explicit-requirement",
                            evidence_block_id="block:reviewed",
                            figma_node_id="2:15",
                            status="matches",
                        )
                    ],
                )
            ]

        def list_figma_snapshots(
            self, result: ProcessingResult
        ) -> list[FigmaSnapshotPreview]:
            return [
                FigmaSnapshotPreview(
                    snapshot_sha256="c" * 64,
                    figma_url="https://www.figma.com/design/file-key/Registration",
                    captured_at="2026-09-04T00:00:00+00:00",
                    nodes=[],
                )
            ]

        def list_design_alignment_drafts(
            self, result: ProcessingResult, proof: CandidateProof
        ) -> list[DesignAlignmentDraftPreview]:
            return self.drafts

    service = FigmaApprovalProcessingService(_result())
    client = TestClient(create_app(service=service))
    client.post("/documents/process", files={"file": ("example.docx", b"document bytes")})
    artifact_id = next(iter(client.app.state.processed_artifacts._items))

    review = client.post(f"/documents/{artifact_id}/candidate")
    assert "Current Figma mappings" in review.text
    assert "Approve and save Copilot Markdown" in review.text
    assert "Approval sequence" not in review.text

    published = client.post(
        f"/documents/{artifact_id}/approve-and-publish-figma",
        data={"approved_by": "reviewer"},
    )
    assert published.status_code == 200
    assert "Published Copilot Markdown" in published.text
    assert "Approved knowledge with Figma" in published.text


def test_candidate_review_can_create_a_same_snapshot_review_package() -> None:
    class PackageProcessingService(StubProcessingService):
        def __init__(self, result: ProcessingResult | Exception) -> None:
            super().__init__(result)
            self.drafts = [
                DesignAlignmentDraftPreview(
                    alignment_sha256="e" * 64,
                    figma_snapshot_sha256="c" * 64,
                    bindings=[
                        DesignAlignmentBindingPreview(
                            annotation_id="fact:explicit-requirement",
                            evidence_block_id="block:reviewed",
                            figma_node_id="2:2",
                            status="matches",
                        )
                    ],
                ),
                DesignAlignmentDraftPreview(
                    alignment_sha256="f" * 64,
                    figma_snapshot_sha256="c" * 64,
                    bindings=[
                        DesignAlignmentBindingPreview(
                            annotation_id="fact:another-requirement",
                            evidence_block_id="block:another",
                            figma_node_id="2:15",
                            status="partial",
                        )
                    ],
                ),
            ]

        def list_figma_snapshots(
            self, result: ProcessingResult
        ) -> list[FigmaSnapshotPreview]:
            return [
                FigmaSnapshotPreview(
                    snapshot_sha256="c" * 64,
                    figma_url="https://www.figma.com/design/file-key/Registration",
                    captured_at="2026-09-04T00:00:00+00:00",
                    nodes=[],
                )
            ]

        def list_design_alignment_drafts(
            self, result: ProcessingResult, proof: CandidateProof
        ) -> list[DesignAlignmentDraftPreview]:
            return self.drafts

        def create_design_alignment_package(
            self,
            result: ProcessingResult,
            proof: CandidateProof,
            alignment_sha256s: list[str],
        ) -> DesignAlignmentDraftPreview:
            assert alignment_sha256s == ["e" * 64, "f" * 64]
            package = super().create_design_alignment_package(
                result, proof, alignment_sha256s
            )
            self.drafts.append(package)
            return package

    client = TestClient(create_app(service=PackageProcessingService(_result())))
    client.post("/documents/process", files={"file": ("example.docx", b"document bytes")})
    artifact_id = next(iter(client.app.state.processed_artifacts._items))
    client.post(f"/documents/{artifact_id}/candidate")

    response = client.post(
        f"/documents/{artifact_id}/design-alignment-package",
        data={"alignment_sha256": ["e" * 64, "f" * 64]},
    )

    assert response.status_code == 200
    assert "Current Figma mappings" in response.text
    assert "2:15" in response.text


def test_unsupported_extension_returns_readable_error() -> None:
    service = StubProcessingService(_result())
    client = TestClient(create_app(service=service))

    response = client.post(
        "/documents/process",
        files={"file": ("notes.txt", b"not a Word document")},
    )

    assert response.status_code == 200
    assert "Choose a .doc or .docx file." in response.text
    assert service.sources == []


def test_pipeline_failure_returns_readable_error() -> None:
    client = TestClient(
        create_app(
            service=StubProcessingService(
                DocumentProcessingError("Analyzer", "Unable to parse document structure."),
            )
        )
    )

    response = client.post(
        "/documents/process",
        files={"file": ("example.docx", b"document bytes")},
    )

    assert response.status_code == 200
    assert "Processing failed" in response.text
    assert "Stage:</strong> Analyzer" in response.text
    assert "Unable to parse document structure." in response.text


def test_oversized_upload_returns_readable_error() -> None:
    service = StubProcessingService(_result())
    client = TestClient(create_app(service=service, max_upload_bytes=4))

    response = client.post(
        "/documents/process",
        files={"file": ("example.docx", b"12345")},
    )

    assert response.status_code == 200
    assert "File exceeds the configured upload limit." in response.text
    assert service.sources == []


def test_repeat_upload_reuses_document_identity_for_the_browser_session() -> None:
    service = StubProcessingService(_result())
    client = TestClient(create_app(service=service))

    for payload in (b"first", b"second"):
        response = client.post(
            "/documents/process",
            files={"file": ("example.docx", payload)},
        )
        assert response.status_code == 200

    assert service.document_ids == [None, "document:example"]


def test_external_link_review_requires_explicit_upload_approval() -> None:
    class ReviewAwareProcessingService(StubProcessingService):
        def process(
            self,
            source: Path,
            *,
            document_id: str | None = None,
            approve_review: bool = False,
        ) -> ProcessingResult:
            if not approve_review:
                raise DocumentProcessingError(
                    "Intake", "External relationships require explicit review approval."
                )
            return super().process(
                source,
                document_id=document_id,
                approve_review=approve_review,
            )

    service = ReviewAwareProcessingService(_result())
    client = TestClient(create_app(service=service))

    unapproved = client.post(
        "/documents/process",
        files={"file": ("external.docx", b"document bytes")},
    )
    approved = client.post(
        "/documents/process",
        data={"approve_review": "true"},
        files={"file": ("external.docx", b"document bytes")},
    )

    assert "Stage:</strong> Intake" in unapproved.text
    assert "External relationships require explicit review approval." in unapproved.text
    assert "Ready for Copilot." in approved.text
    assert service.review_approvals == [True]
