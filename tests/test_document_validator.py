from __future__ import annotations

from pathlib import Path

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.validator import DocumentValidator


def test_valid_document(tmp_path: Path) -> None:
    normalized_dir = tmp_path / "output" / "normalized"
    normalized_dir.mkdir(parents=True)

    markdown_path = normalized_dir / "document.md"

    markdown_path.write_text(
        """# Test

Paragraph.
""",
        encoding="utf-8",
    )

    document = StructuralAnalyzer().analyze(
        markdown_path
    )

    report = DocumentValidator().validate(
        document,
        markdown_path=markdown_path,
    )

    assert report.valid is True
    assert report.errors == []


def test_missing_markdown_is_error(tmp_path: Path) -> None:
    missing = (
        tmp_path
        / "output"
        / "normalized"
        / "missing.md"
    )

    document = StructuralAnalyzer().analyze_text(
        "# Test\n\nParagraph.\n",
        markdown_path=missing,
    )

    report = DocumentValidator().validate(
        document,
        markdown_path=missing,
    )

    assert report.valid is False

    assert any(
        issue.code == "MARKDOWN_NOT_FOUND"
        for issue in report.errors
    )


def test_missing_local_image_is_error(tmp_path: Path) -> None:
    normalized_dir = tmp_path / "output" / "normalized"
    normalized_dir.mkdir(parents=True)

    markdown_path = normalized_dir / "document.md"

    markdown_path.write_text(
        """# Test

![missing](../media/missing.png)
""",
        encoding="utf-8",
    )

    document = StructuralAnalyzer().analyze(
        markdown_path
    )

    report = DocumentValidator().validate(
        document,
        markdown_path=markdown_path,
    )

    assert report.valid is False

    assert any(
        issue.code == "IMAGE_FILE_NOT_FOUND"
        for issue in report.errors
    )


def test_existing_local_image_is_valid(tmp_path: Path) -> None:
    normalized_dir = tmp_path / "output" / "normalized"
    media_dir = tmp_path / "output" / "media"

    normalized_dir.mkdir(parents=True)
    media_dir.mkdir(parents=True)

    markdown_path = normalized_dir / "document.md"
    image_path = media_dir / "image.png"

    image_path.write_bytes(b"test")

    markdown_path.write_text(
        """# Test

![image](../media/image.png)
""",
        encoding="utf-8",
    )

    document = StructuralAnalyzer().analyze(
        markdown_path
    )

    report = DocumentValidator().validate(
        document,
        markdown_path=markdown_path,
    )

    assert report.valid is True

    assert not any(
        issue.code == "IMAGE_FILE_NOT_FOUND"
        for issue in report.errors
    )
