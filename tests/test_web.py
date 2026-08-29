from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from doc2agent.application import (
    CandidateProof,
    DocumentProcessingError,
    ProcessingResult,
    ProofEvidence,
    ProofFact,
)
from doc2agent.web.app import create_app


class StubProcessingService:
    def __init__(self, result: ProcessingResult | Exception) -> None:
        self.result = result
        self.sources: list[Path] = []
        self.document_ids: list[str | None] = []
        self.review_approvals: list[bool] = []

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
                    kind="requirement",
                    value="Explicit requirement",
                    evidence=[
                        ProofEvidence(
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
