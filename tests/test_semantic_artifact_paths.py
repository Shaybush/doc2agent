from __future__ import annotations

from pathlib import Path

from doc2agent.semantic import artifact_root_for_document, document_artifact_name


def test_artifact_root_reuses_pipeline_revision_directory() -> None:
    document_path = Path(
        "storage/artifacts/sha256-example/structured/example.document.json"
    )

    assert artifact_root_for_document(document_path, "sha256:ignored") == Path(
        "storage/artifacts/sha256-example"
    )
    assert document_artifact_name(document_path) == "example"


def test_artifact_root_falls_back_to_application_storage() -> None:
    assert artifact_root_for_document("document.json", "sha256:abc") == Path(
        "storage/artifacts/sha256-abc"
    )
    assert document_artifact_name("document.json") == "document"
