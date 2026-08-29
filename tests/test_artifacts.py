from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.source_manifest import SourceManifest


def test_source_manifest_uses_source_bytes_for_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "тестовый-документ.docx"
    source.write_bytes(b"same source bytes")
    received_at = datetime(2026, 8, 28, tzinfo=UTC)

    first = SourceManifest.from_path(source, received_at=received_at)
    second = SourceManifest.from_path(source, received_at=received_at)

    assert first == second
    assert first.source_id.startswith("sha256:")
    assert first.byte_size == len(b"same source bytes")


def test_structural_hash_is_stable_for_identical_input() -> None:
    analyzer = StructuralAnalyzer()
    text = "# כותרת\n\nСтабильный текст.\n"

    first = analyzer.analyze_text(text, markdown_path="stable.md")
    second = analyzer.analyze_text(text, markdown_path="stable.md")

    assert first.structural_sha256 == second.structural_sha256


def test_structural_hash_excludes_source_intake_time(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"source bytes")
    analyzer = StructuralAnalyzer()

    first = analyzer.analyze_text(
        "# Title\n\nText.\n",
        source_manifest=SourceManifest.from_path(
            source,
            received_at=datetime(2026, 8, 28, tzinfo=UTC),
        ),
    )
    second = analyzer.analyze_text(
        "# Title\n\nText.\n",
        source_manifest=SourceManifest.from_path(
            source,
            received_at=datetime(2026, 8, 29, tzinfo=UTC),
        ),
    )

    assert first.structural_sha256 == second.structural_sha256
