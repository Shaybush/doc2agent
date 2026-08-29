from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.ingest import IngestService
from doc2agent.support_bundle import SupportBundleExporter


def _write_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document xmlns:w='urn:test'/>")


def test_support_bundle_excludes_document_content_and_sanitized_payload(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    source = tmp_path / "private.docx"
    _write_docx(source)
    ingested = IngestService(storage).ingest(source)
    artifact_root = storage / "artifacts" / ingested.manifest.source_id.replace(":", "-")
    (artifact_root / "validation").mkdir(parents=True)
    (artifact_root / "security").mkdir(parents=True)
    (artifact_root / "validation" / "private.validation.json").write_text(
        '{"valid": true, "errors": [], "warnings": []}', encoding="utf-8"
    )
    (artifact_root / "security" / "private.security.json").write_text(
        '{"decision": "pass", "sanitized_document": '
        '{"text": "confidential document content"}, "findings": []}',
        encoding="utf-8",
    )
    output = tmp_path / "support.zip"

    result = SupportBundleExporter(storage).export(
        ingested.document_id,
        output=output,
    )

    assert result == output
    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == [
            "checksums.json",
            "diagnostics.json",
            "support-manifest.json",
        ]
        bundle_text = "\n".join(
            archive.read(name).decode("utf-8") for name in archive.namelist()
        )
    assert "confidential document content" not in bundle_text
    diagnostics = json.loads(
        zipfile.ZipFile(output).read("diagnostics.json").decode("utf-8")
    )
    assert diagnostics["sources"][0]["validation"]["valid"] is True
    assert diagnostics["sources"][0]["security"]["decision"] == "pass"
    with zipfile.ZipFile(output) as archive:
        checksums = json.loads(archive.read("checksums.json").decode("utf-8"))
        for name, expected in checksums.items():
            assert sha256(archive.read(name)).hexdigest() == expected


def test_support_bundle_marks_malformed_artifacts_without_failing(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    source = tmp_path / "private.docx"
    _write_docx(source)
    ingested = IngestService(storage).ingest(source)
    artifact_root = storage / "artifacts" / ingested.manifest.source_id.replace(":", "-")
    (artifact_root / "validation").mkdir(parents=True)
    (artifact_root / "validation" / "private.validation.json").write_text(
        "{broken", encoding="utf-8"
    )

    output = SupportBundleExporter(storage).export(
        ingested.document_id,
        output=tmp_path / "support.zip",
    )

    with zipfile.ZipFile(output) as archive:
        diagnostics = json.loads(archive.read("diagnostics.json").decode("utf-8"))
    assert diagnostics["sources"][0]["validation"] == {"status": "malformed"}


def test_export_support_cli_prints_created_bundle(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    source = tmp_path / "document.docx"
    _write_docx(source)
    ingested = IngestService(storage).ingest(source)
    output = tmp_path / "support.zip"

    result = CliRunner().invoke(
        cli.app,
        [
            "export-support",
            "--document-id",
            ingested.document_id,
            "--storage",
            str(storage),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert f"Support bundle: {output}" in result.output
