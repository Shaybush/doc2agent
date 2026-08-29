from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.ingest import IngestDecision, IngestResult, IngestService
from doc2agent.models.document import AnalyzedDocument
from doc2agent.pipeline import (
    Doc2AgentPipeline,
    PipelineStatus,
)
from doc2agent.source_manifest import SourceManifest

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "hebrew_test_document.docx"
)


def _ingest(source: Path, storage: Path):
    result = IngestService(storage).ingest(source, approve_review=True)
    assert result.accepted
    return result


def _copy_fixture_with_replaced_text(
    tmp_path: Path,
    replacement: str,
) -> Path:
    destination = tmp_path / "security-fixture.docx"
    target = b"<w:t>OpenAI</w:t>"
    replacement_xml = (
        f"<w:t>{replacement}</w:t>".encode()
    )

    with zipfile.ZipFile(FIXTURE) as source_archive:
        with zipfile.ZipFile(destination, "w") as destination_archive:
            for entry in source_archive.infolist():
                content = source_archive.read(entry.filename)

                if entry.filename == "word/document.xml":
                    assert target in content
                    content = content.replace(target, replacement_xml)

                destination_archive.writestr(entry, content)

    return destination


class DuplicateIdAnalyzer(StructuralAnalyzer):
    def analyze(
        self,
        markdown_path: str | Path,
        *,
        metadata_path: str | Path | None = None,
    ) -> AnalyzedDocument:
        document = super().analyze(
            markdown_path,
            metadata_path=metadata_path,
        )
        document.sections[0].id = document.document.id
        return document


def _flatten_sections(
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for section in sections:
        result.append(section)
        result.extend(
            _flatten_sections(
                section.get("children", [])
            )
        )

    return result


def _all_blocks(
    document: dict[str, Any],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = list(
        document.get("preamble", [])
    )

    for section in _flatten_sections(
        document.get("sections", [])
    ):
        blocks.extend(section.get("blocks", []))

    return blocks


def test_full_docx_pipeline(tmp_path: Path) -> None:
    assert FIXTURE.exists(), (
        f"Missing fixture: {FIXTURE}. "
        "Copy hebrew_test_document.docx to tests/fixtures/."
    )

    output_root = tmp_path / "output"

    result = Doc2AgentPipeline().run(
        _ingest(FIXTURE, tmp_path / "storage"),
        output_root,
    )

    # Pipeline readiness
    assert result.status == PipelineStatus.READY
    assert result.can_send_to_llm is True

    # Files were produced
    assert result.converted.markdown.exists()
    assert result.converted.metadata.exists()
    assert result.source_manifest_json.exists()
    assert result.normalized_markdown.exists()
    assert result.document_json.exists()
    assert result.validation_json.exists()
    assert result.security_json.exists()

    assert (
        result.converted.markdown
        != result.normalized_markdown
    )

    # Normalized Markdown
    normalized = result.normalized_markdown.read_text(
        encoding="utf-8"
    )

    assert "# 1. מבוא" in normalized
    assert "## 1.1 מטרות הבדיקה" in normalized
    assert "### 1.1.1 שלבים צפויים" in normalized

    assert r"**# 1. מבוא**" not in normalized
    assert (
        r"\- שמירה על היררכיית כותרות."
        not in normalized
    )
    assert r"1\. Converter" not in normalized

    assert "שמירה על היררכיית כותרות." in normalized

    # document.json
    document = json.loads(
        result.document_json.read_text(
            encoding="utf-8"
        )
    )

    assert document["schema_version"] == "1.0"
    assert document["source_manifest"]["source_id"].startswith("sha256:")
    assert len(document["structural_sha256"]) == 64

    source_manifest = json.loads(
        result.source_manifest_json.read_text(encoding="utf-8")
    )
    assert source_manifest == document["source_manifest"]

    # security.json
    security = json.loads(
        result.security_json.read_text(
            encoding="utf-8"
        )
    )

    assert security["decision"] == "pass"
    assert security["findings"] == []
    assert security["sanitized_document"] == document

    info = document["document"]

    assert info["source_format"] == "docx"
    assert info["language"] == "mixed"
    assert info["direction"] == "mixed"

    metadata = json.loads(result.converted.metadata.read_text(encoding="utf-8"))
    assert {
        "text": "Converter יוצר Markdown מנורמל.",
        "direction": "rtl",
        "alignment": "right",
    } in metadata["paragraph_layout"]

    assert (
        Path(info["markdown"])
        == result.normalized_markdown
    )

    # Section hierarchy
    sections = document["sections"]

    intro = next(
        section
        for section in sections
        if section["title"] == "1. מבוא"
    )

    assert intro["level"] == 1
    assert intro["direction"] == "rtl"
    assert intro["path"] == ["1. מבוא"]

    goals = next(
        section
        for section in intro["children"]
        if section["title"] == "1.1 מטרות הבדיקה"
    )

    assert goals["level"] == 2
    assert goals["path"] == [
        "1. מבוא",
        "1.1 מטרות הבדיקה",
    ]

    steps = next(
        section
        for section in goals["children"]
        if section["title"] == "1.1.1 שלבים צפויים"
    )

    assert steps["level"] == 3
    assert steps["path"] == [
        "1. מבוא",
        "1.1 מטרות הבדיקה",
        "1.1.1 שלבים צפויים",
    ]

    # Blocks
    blocks = _all_blocks(document)
    block_types = [
        block["type"]
        for block in blocks
    ]

    assert "paragraph" in block_types
    assert "unordered_list" in block_types
    assert "ordered_list" in block_types
    assert "table" in block_types
    assert "image" in block_types

    # Unordered list
    unordered = next(
        block
        for block in blocks
        if block["type"] == "unordered_list"
    )

    assert unordered["items"] == [
        "שמירה על היררכיית כותרות.",
        "זיהוי פסקאות ורשימות.",
        "שמירה על טקסט בעברית ובאנגלית.",
        "שמירה על קישורים, טבלאות ותמונות.",
    ]

    assert unordered["direction"] == "rtl"

    # Ordered list
    ordered = next(
        block
        for block in blocks
        if block["type"] == "ordered_list"
    )

    assert ordered["items"] == [
        "Converter יוצר Markdown מנורמל.",
        "StructuralAnalyzer יוצר document.json.",
        "Validator בודק שהמבנה עקבי.",
    ]
    assert ordered["direction"] == "rtl"

    # Table
    table = next(
        block
        for block in blocks
        if block["type"] == "table"
    )

    assert table["headers"] == [
        "**רכיב**",
        "**קלט**",
        "**פלט**",
    ]

    assert table["rows"] == [
        [
            "Converter",
            "DOCX",
            "Markdown",
        ],
        [
            "StructuralAnalyzer",
            "Markdown",
            "document.json",
        ],
        [
            "LLM Enrichment",
            "document.json",
            "enriched.json",
        ],
    ]

    # Image
    image = next(
        block
        for block in blocks
        if block["type"] == "image"
    )

    assert image["alt"] == "doc2agent_test_image.png"
    assert image["src"].endswith(
        "media/hebrew_test_document/"
        "doc2agent_test_image.png"
    )

    # Source line integrity
    for block in blocks:
        assert block["start_line"] >= 1
        assert (
            block["end_line"]
            >= block["start_line"]
        )

    for section in _flatten_sections(sections):
        assert section["start_line"] >= 1
        assert (
            section["end_line"]
            >= section["start_line"]
        )


def test_pipeline_returns_ready_with_redactions(
    tmp_path: Path,
) -> None:
    source = _copy_fixture_with_replaced_text(
        tmp_path,
        "security@example.com",
    )

    result = Doc2AgentPipeline().run(
        _ingest(source, tmp_path / "storage"),
        tmp_path / "output",
    )

    assert result.status == PipelineStatus.READY_WITH_REDACTIONS
    assert result.can_send_to_llm is True

    security = json.loads(
        result.security_json.read_text(encoding="utf-8")
    )

    assert security["decision"] == "redact"
    assert any(
        finding["kind"] == "email"
        for finding in security["findings"]
    )
    assert "[REDACTED_EMAIL]" in json.dumps(
        security["sanitized_document"],
    )


def test_pipeline_returns_blocked_for_internal_url(
    tmp_path: Path,
) -> None:
    source = _copy_fixture_with_replaced_text(
        tmp_path,
        "http://localhost/admin",
    )

    result = Doc2AgentPipeline().run(
        _ingest(source, tmp_path / "storage"),
        tmp_path / "output",
    )

    assert result.status == PipelineStatus.BLOCKED
    assert result.can_send_to_llm is False

    security = json.loads(
        result.security_json.read_text(encoding="utf-8")
    )

    assert security["decision"] == "block"
    assert any(
        finding["kind"] == "internal_url"
        for finding in security["findings"]
    )
    assert "[BLOCKED_INTERNAL_URL]" in json.dumps(
        security["sanitized_document"],
    )


def test_pipeline_returns_invalid_for_validation_errors(
    tmp_path: Path,
) -> None:
    result = Doc2AgentPipeline(
        analyzer=DuplicateIdAnalyzer(),
    ).run(
        _ingest(FIXTURE, tmp_path / "storage"),
        tmp_path / "output",
    )

    assert result.status == PipelineStatus.INVALID
    assert result.can_send_to_llm is False
    assert result.validation.valid is False
    assert any(
        error.code == "DUPLICATE_ID"
        for error in result.validation.errors
    )

    security = json.loads(
        result.security_json.read_text(encoding="utf-8")
    )

    assert security["decision"] == "pass"


def test_pipeline_rejects_a_source_that_did_not_pass_ingest(
    tmp_path: Path,
) -> None:
    rejected = IngestResult(
        decision=IngestDecision.REQUIRES_REVIEW,
        manifest=SourceManifest.from_path(FIXTURE),
        diagnostics=(),
        document_id=None,
        source_path=None,
        manifest_path=None,
        duplicate=False,
    )

    with pytest.raises(ValueError, match="accepted ingested source"):
        Doc2AgentPipeline().run(rejected, tmp_path / "output")
