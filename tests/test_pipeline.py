from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doc2agent.pipeline import (
    Doc2AgentPipeline,
    PipelineStatus,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "hebrew_test_document.docx"
)


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
        FIXTURE,
        output_root,
    )

    # Pipeline readiness
    assert result.status == PipelineStatus.READY
    assert result.can_send_to_llm is True

    # Files were produced
    assert result.converted.markdown.exists()
    assert result.converted.metadata.exists()
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