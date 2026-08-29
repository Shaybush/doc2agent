from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.ingest import IngestDecision, IngestService
from doc2agent.pipeline import PipelineStatus
from doc2agent.source_manifest import SourceManifest

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "hebrew_test_document.docx"
)
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


def test_pipeline_reports_security_artifact_and_status(
    tmp_path: Path,
) -> None:
    source = _copy_fixture_without_external_relationships(tmp_path)
    result = CliRunner().invoke(
        cli.app,
        [
            "pipeline",
            str(source),
            "--storage",
            str(tmp_path / "storage"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Validation:          OK" in result.output
    assert "security.json:" in result.output
    assert "Pipeline status:      ready" in result.output
    assert "Artifact root:" in result.output


def test_pipeline_creates_new_artifacts_for_an_updated_docx(
    tmp_path: Path,
) -> None:
    source = _copy_fixture_without_external_relationships(tmp_path)
    storage = tmp_path / "storage"

    first = CliRunner().invoke(
        cli.app,
        ["pipeline", str(source), "--storage", str(storage)],
    )
    assert first.exit_code == 0, first.output

    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("customXml/version.txt", "second version")

    second = CliRunner().invoke(
        cli.app,
        ["pipeline", str(source), "--storage", str(storage)],
    )
    assert second.exit_code == 0, second.output

    catalog = json.loads(
        (storage / "source-catalog.json").read_text(encoding="utf-8")
    )
    document = next(iter(catalog["documents"].values()))
    assert len(document["source_ids"]) == 2
    assert document["current_source_id"] == document["source_ids"][1]
    for source_id in document["source_ids"]:
        artifact_root = Path(catalog["sources"][source_id]["artifact_root"])
        assert (artifact_root / "structured" / "safe-fixture.document.json").exists()


def test_ingest_reports_stored_source(tmp_path: Path) -> None:
    source = tmp_path / "safe.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='urn:test'/>",
        )

    result = CliRunner().invoke(
        cli.app,
        [
            "ingest",
            str(source),
            "--storage",
            str(tmp_path / "storage"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingest decision: pass" in result.output
    assert "Source ID:       sha256:" in result.output
    assert "Stored source:" in result.output


def test_delete_previews_then_requires_yes_to_move_to_trash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "safe.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='urn:test'/>",
        )

    storage = tmp_path / "storage"
    ingested = IngestService(storage).ingest(source)
    preview = CliRunner().invoke(
        cli.app,
        [
            "delete",
            "--document-id",
            ingested.document_id,
            "--storage",
            str(storage),
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert "Preview only." in preview.output
    assert ingested.source_path.exists()

    deleted = CliRunner().invoke(
        cli.app,
        [
            "delete",
            "--document-id",
            ingested.document_id,
            "--storage",
            str(storage),
            "--yes",
        ],
    )
    assert deleted.exit_code == 0, deleted.output
    assert "Moved to trash:" in deleted.output
    assert not ingested.source_path.exists()


@pytest.mark.parametrize(
    ("status", "validation_valid"),
    [
        (PipelineStatus.READY_WITH_REDACTIONS, True),
        (PipelineStatus.BLOCKED, True),
        (PipelineStatus.INVALID, False),
    ],
)
def test_pipeline_reports_non_ready_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: PipelineStatus,
    validation_valid: bool,
) -> None:
    source = tmp_path / "source.docx"
    source.touch()
    _stub_accepted_ingest(monkeypatch, source)

    pipeline_result = SimpleNamespace(
        converted=SimpleNamespace(
            markdown=tmp_path / "raw.md",
            metadata=tmp_path / "metadata.json",
        ),
        source_manifest_json=tmp_path / "source-manifest.json",
        normalized_markdown=tmp_path / "normalized.md",
        document_json=tmp_path / "document.json",
        validation_json=tmp_path / "validation.json",
        validation=SimpleNamespace(valid=validation_valid),
        security_json=tmp_path / "security.json",
        status=status,
    )

    class PipelineStub:
        def run(
            self,
            ingested: object,
            artifact_root: Path,
        ) -> SimpleNamespace:
            del ingested, artifact_root
            return pipeline_result

    monkeypatch.setattr(
        cli,
        "Doc2AgentPipeline",
        PipelineStub,
    )

    result = CliRunner().invoke(
        cli.app,
        ["pipeline", str(source), "--storage", str(tmp_path / "storage")],
    )

    assert result.exit_code == 0, result.output
    expected_validation = "OK" if validation_valid else "FAILED"
    assert f"Validation:          {expected_validation}" in result.output
    assert f"Pipeline status:      {status}" in result.output


def test_pipeline_stops_before_conversion_when_ingest_requires_review(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli.app,
        ["pipeline", str(FIXTURE), "--storage", str(tmp_path / "storage")],
    )

    assert result.exit_code == 1
    assert "Ingest decision: requires_review" in result.output
    assert "Raw Markdown:" not in result.output


def test_pipeline_approves_review_without_opening_external_links(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "pipeline",
            str(FIXTURE),
            "--storage",
            str(tmp_path / "storage"),
            "--approve-review",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Review approval: external links will not be opened." in result.output
    assert "Pipeline status:      ready" in result.output


def _stub_accepted_ingest(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
) -> None:
    manifest = SourceManifest.from_path(source)
    ingested = SimpleNamespace(
        decision=IngestDecision.PASS,
        diagnostics=(),
        accepted=True,
        manifest=manifest,
        source_path=source,
        document_id="document:test",
        review_approved=False,
    )

    class IngestStub:
        def __init__(self, _: Path) -> None:
            pass

        def ingest(self, _: Path, **__: object) -> SimpleNamespace:
            return ingested

        def record_artifact_root(self, _: str, __: Path) -> None:
            pass

    monkeypatch.setattr(cli, "IngestService", IngestStub)


def _copy_fixture_without_external_relationships(tmp_path: Path) -> Path:
    source = tmp_path / "safe-fixture.docx"
    with zipfile.ZipFile(FIXTURE) as original:
        with zipfile.ZipFile(source, "w") as copy:
            for entry in original.infolist():
                content = original.read(entry.filename)
                if entry.filename.endswith(".rels"):
                    root = ElementTree.fromstring(content)
                    for relationship in list(root):
                        if relationship.attrib.get("TargetMode") == "External":
                            root.remove(relationship)
                    content = ElementTree.tostring(
                        root,
                        encoding="utf-8",
                        xml_declaration=True,
                    )
                copy.writestr(entry, content)
    return source


def test_evaluate_semantic_reports_fixture_pass(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "semantic-evaluation.json"

    result = CliRunner().invoke(
        cli.app,
        [
            "evaluate-semantic",
            str(EVALUATION_MARKDOWN),
            "--cases",
            str(EVALUATION_CASES),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Provider: fixture" in result.output
    assert "Matched: 9/9" in result.output
    assert "Evaluation: OK" in result.output
    assert json.loads(output_path.read_text(encoding="utf-8"))["passed"] is True
