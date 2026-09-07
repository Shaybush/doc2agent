from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.semantic import (
    ApprovalService,
    CandidateBuilder,
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
    PipelineStep,
    TopicRenderer,
)
from doc2agent.source_manifest import SourceManifest

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.md"


def _candidate():
    document = StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )
    block = document.sections[0].blocks[0]
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:requirement",
                kind="requirement",
                value="General English paragraph.",
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
    return CandidateBuilder().build(patch, document)


def _rtl_candidate(filename: str = "hebrew_test_document.docx"):
    manifest = SourceManifest(
        source_id="sha256:" + "1" * 64,
        filename=filename,
        byte_size=1,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        received_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    document = StructuralAnalyzer().analyze_text(
        "# כותרת\n\nאין צורך ב-LLM בשלב הניתוח המבני.\n",
        markdown_path="hebrew_test_document.md",
        source_manifest=manifest,
    )
    block = document.sections[0].blocks[0]
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:decision",
                kind="decision",
                value="אין צורך ב-LLM בשלב הניתוח המבני.",
                evidence=[
                    EvidenceRef(
                        source_id=manifest.source_id,
                        block_id=block.id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            )
        ],
    )
    return CandidateBuilder().build(patch, document)


def test_renderer_writes_deterministic_context_and_manifest(tmp_path: Path) -> None:
    candidate = _candidate()
    output_root = tmp_path / "copilot-context"
    renderer = TopicRenderer()

    first = renderer.render(candidate, output_root)
    first_agent = (first.output_root / "sample.md").read_bytes()
    second = renderer.render(candidate, output_root)

    assert output_root.is_symlink()
    assert (output_root / "INDEX.md").exists()
    assert (output_root / "sample.md").read_bytes() == first_agent
    assert "GENERATED — DO NOT EDIT" in (output_root / "INDEX.md").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert manifest["approved_candidate_sha256"] == candidate.candidate_sha256
    assert [item["path"] for item in manifest["files"]] == ["INDEX.md", "sample.md"]
    assert "[Approved knowledge](sample.md)" in (
        output_root / "INDEX.md"
    ).read_text(encoding="utf-8")


def test_renderer_reserves_index_filename_for_generated_index(tmp_path: Path) -> None:
    result = TopicRenderer().render(_rtl_candidate("INDEX.docx"), tmp_path / "context")

    assert result.context_path.name == "INDEX.approved-knowledge.md"
    assert (result.output_root / "INDEX.md").read_text(encoding="utf-8").count(
        "# Copilot context index"
    ) == 1
    assert "[Approved knowledge](INDEX.approved-knowledge.md)" in (
        result.output_root / "INDEX.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("corruption", ["missing", "modified"])
def test_renderer_refuses_incomplete_or_corrupted_existing_version(
    tmp_path: Path, corruption: str
) -> None:
    renderer = TopicRenderer()
    candidate = _candidate()
    output_root = tmp_path / "context"
    result = renderer.render(candidate, output_root)
    version_path = result.output_root.resolve()

    if corruption == "missing":
        (version_path / "INDEX.md").unlink()
        expected_message = "incomplete"
    else:
        result.context_path.write_text("corrupted", encoding="utf-8")
        expected_message = "mismatched hash"

    with pytest.raises(ValueError, match=expected_message):
        renderer.render(candidate, output_root)


def test_renderer_can_prepare_before_activating_a_context_version(tmp_path: Path) -> None:
    renderer = TopicRenderer()
    output_root = tmp_path / "context"

    prepared = renderer.prepare(_candidate(), output_root)

    assert prepared.version_path.is_dir()
    assert not output_root.exists()
    result = renderer.activate_prepared(prepared)
    assert output_root.is_symlink()
    assert result.context_path.read_text(encoding="utf-8").startswith(
        "<!-- GENERATED — DO NOT EDIT -->"
    )


def test_renderer_refuses_to_replace_non_generated_directory(tmp_path: Path) -> None:
    output_root = tmp_path / "copilot-context"
    output_root.mkdir()

    with pytest.raises(ValueError, match="not generated by Doc2Agent"):
        TopicRenderer().render(_candidate(), output_root)


def test_renderer_isolates_rtl_fact_values_from_ltr_markdown(tmp_path: Path) -> None:
    output_root = tmp_path / "copilot-context"

    result = TopicRenderer().render(_rtl_candidate(), output_root)

    markdown = result.context_path.read_text(encoding="utf-8")
    assert "אין צורך ב-LLM בשלב הניתוח המבני." in markdown
    assert '<div dir="rtl">' in markdown
    assert "\u2067" not in markdown
    assert "<bdi" not in markdown


def test_renderer_writes_architecture_and_pipeline_section(tmp_path: Path) -> None:
    candidate = _candidate()
    block = candidate.document.sections[0].blocks[0]
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=candidate.source_id,
        structural_sha256=candidate.structural_sha256,
        scope_block_ids=[block.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(candidate.document)),
        facts=[
            PatchFact(
                id="fact:architecture-pipeline",
                kind="architecture_pipeline",
                value="General English paragraph.",
                evidence=[
                    EvidenceRef(
                        source_id=candidate.source_id,
                        block_id=block.id,
                        structural_sha256=candidate.structural_sha256,
                        support="explicit",
                    )
                ],
            )
        ],
    )

    architecture_candidate = CandidateBuilder().build(patch, candidate.document)
    result = TopicRenderer().render(architecture_candidate, tmp_path / "copilot-context")
    markdown = result.context_path.read_text(encoding="utf-8")
    index = (result.output_root / "INDEX.md").read_text(encoding="utf-8")

    assert "## Architecture and pipeline" in markdown
    assert "- General English paragraph." in markdown
    assert "- Architecture and pipeline: 1" in index


def test_renderer_formats_structured_pipeline_step_and_purpose(tmp_path: Path) -> None:
    source = tmp_path / "pipeline.md"
    source.write_text(
        "# Pipeline\n\nThe purpose is reliable conversion.\n\n"
        "| Component | Input | Output |\n| --- | --- | --- |\n"
        "| Converter | DOCX | Markdown |\n",
        encoding="utf-8",
    )
    document = StructuralAnalyzer().analyze(
        source,
        source_manifest=SourceManifest.from_path(source),
    )
    purpose_block, table = document.sections[0].blocks
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[purpose_block.id, table.id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:purpose",
                kind="purpose",
                value="The purpose is reliable conversion.",
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=purpose_block.id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            ),
            PatchFact(
                id="fact:pipeline",
                kind="architecture_pipeline",
                value="Converter DOCX Markdown",
                pipeline_step=PipelineStep(
                    component="Converter",
                    input="DOCX",
                    output="Markdown",
                ),
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=table.id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            ),
        ],
    )

    result = TopicRenderer().render(
        CandidateBuilder().build(patch, document),
        tmp_path / "copilot-context",
    )
    markdown = result.context_path.read_text(encoding="utf-8")
    index = (result.output_root / "INDEX.md").read_text(encoding="utf-8")

    assert "## Purpose" in markdown
    assert "- The purpose is reliable conversion." in markdown
    assert "- Converter: DOCX → Markdown" in markdown
    assert "- Purpose: 1" in index


def test_renderer_preserves_mixed_text_exactly() -> None:
    fact = _rtl_candidate().annotations[0].model_copy(
        update={"value": "Converter יוצר Markdown מנורמל."}
    )

    rendered = TopicRenderer._fact_value_markdown(fact)

    assert rendered == "Converter יוצר Markdown מנורמל."


def test_render_copilot_cli_accepts_an_explicit_approved_snapshot(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    candidate = _candidate()
    candidate_path = ApprovalService(storage).approve(
        candidate,
        document_id="document:test",
        approved_by="reviewer",
    )
    (storage / "source-catalog.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sources": {
                    candidate.source_id: {
                        "source_id": candidate.source_id,
                        "document_id": "document:test",
                        "filename": "sample.md",
                        "source_path": "storage/sources/sample.md",
                        "received_at": "2026-08-28T00:00:00+00:00",
                    }
                },
                "documents": {},
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "copilot-context"

    result = CliRunner().invoke(
        cli.app,
        [
            "render-copilot",
            "--approved",
            str(candidate_path),
            "--output",
            str(output_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Copilot context:" in result.output


def test_render_copilot_cli_rejects_unapproved_candidate(tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidate.knowledge.json"
    candidate_path.write_text(_candidate().model_dump_json(indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        ["render-copilot", "--approved", str(candidate_path)],
    )

    assert result.exit_code != 0
    assert "not registered" in result.output


def test_render_copilot_cli_uses_source_name_and_logical_document_directory(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    storage = tmp_path / "storage"
    approved_path = storage / "approved/document%3Atest--f49b964b6bad/current.knowledge.json"
    approved_path.parent.mkdir(parents=True)
    approved_path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
    (storage / "source-catalog.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sources": {
                    candidate.source_id: {
                        "source_id": candidate.source_id,
                        "document_id": "document:test",
                        "filename": "sample.md",
                        "source_path": "storage/sources/sample.md",
                        "received_at": "2026-08-28T00:00:00+00:00",
                    }
                },
                "documents": {},
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        ["render-copilot", "--approved", str(approved_path)],
    )

    output_root = storage / "copilot-context/sample--document-test"
    assert result.exit_code == 0, result.output
    assert (output_root / "sample.md").exists()
    assert f"Knowledge Markdown: {output_root / 'sample.md'}" in result.output
