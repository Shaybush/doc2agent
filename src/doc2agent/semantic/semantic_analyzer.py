from __future__ import annotations

from pathlib import Path

from doc2agent.models.document import AnalyzedDocument, Section
from doc2agent.semantic.models import (
    EvidenceRef,
    KnowledgeDocument,
    SemanticAnnotation,
)
from doc2agent.semantic.providers import NoOpSemanticProvider, SemanticProvider


class SemanticAnalyzer:
    """Add provider-owned semantic annotations without mutating document.json."""

    def __init__(
        self,
        provider: SemanticProvider | None = None,
    ) -> None:
        self.provider = provider or NoOpSemanticProvider()

    def analyze(
        self,
        document: AnalyzedDocument,
    ) -> KnowledgeDocument:
        canonical_document = document.model_copy(deep=True)
        annotations = self.provider.enrich(canonical_document)
        self.validate_annotations(canonical_document, annotations)

        return KnowledgeDocument(
            document=canonical_document,
            provider=self.provider.name,
            annotations=annotations,
        )

    def write_json(
        self,
        knowledge: KnowledgeDocument,
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            knowledge.model_dump_json(
                indent=2,
                exclude_none=True,
            ),
            encoding="utf-8",
        )
        return output_path

    @staticmethod
    def validate_annotations(
        document: AnalyzedDocument,
        annotations: list[SemanticAnnotation],
    ) -> None:
        annotation_ids: set[str] = set()
        source_ids = SemanticAnalyzer._source_ids(document)
        block_ids = SemanticAnalyzer._block_ids(document)

        for annotation in annotations:
            if annotation.id in annotation_ids:
                raise ValueError(
                    f"Duplicate semantic annotation id: {annotation.id}"
                )

            annotation_ids.add(annotation.id)

            if not annotation.source_ids and not annotation.evidence:
                raise ValueError(
                    "Semantic annotation requires source_ids or evidence"
                )

            unknown_source_ids = set(annotation.source_ids) - source_ids

            if unknown_source_ids:
                unknown = ", ".join(sorted(unknown_source_ids))
                raise ValueError(
                    f"Unknown semantic annotation source ids: {unknown}"
                )

            SemanticAnalyzer._validate_evidence(
                document,
                annotation.evidence,
                block_ids,
            )

    @staticmethod
    def _validate_evidence(
        document: AnalyzedDocument,
        evidence: list[EvidenceRef],
        block_ids: set[str],
    ) -> None:
        if not evidence:
            return

        if document.source_manifest is None or not document.structural_sha256:
            raise ValueError(
                "Evidence requires a source manifest and structural hash"
            )

        for item in evidence:
            if item.source_id != document.source_manifest.source_id:
                raise ValueError("Evidence source_id does not match document")
            if item.structural_sha256 != document.structural_sha256:
                raise ValueError(
                    "Evidence structural_sha256 does not match document"
                )
            if item.block_id not in block_ids:
                raise ValueError(
                    f"Evidence block_id is not a content block: {item.block_id}"
                )

    @staticmethod
    def _source_ids(document: AnalyzedDocument) -> set[str]:
        source_ids = {document.document.id}

        for block in document.preamble:
            source_ids.add(block.id)

        def collect_section(section: Section) -> None:
            source_ids.add(section.id)

            for block in section.blocks:
                source_ids.add(block.id)

            for child in section.children:
                collect_section(child)

        for section in document.sections:
            collect_section(section)

        return source_ids

    @staticmethod
    def _block_ids(document: AnalyzedDocument) -> set[str]:
        block_ids = {block.id for block in document.preamble}

        def collect_section(section: Section) -> None:
            block_ids.update(block.id for block in section.blocks)
            for child in section.children:
                collect_section(child)

        for section in document.sections:
            collect_section(section)

        return block_ids
