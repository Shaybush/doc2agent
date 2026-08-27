from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.converter import Converter, ConverterResult
from doc2agent.normalization.markdown_normalizer import MarkdownNormalizer
from doc2agent.security import SecurityGateway
from doc2agent.security.models import (
    SecurityDecision,
    SecurityResult,
)
from doc2agent.validator import DocumentValidator


class PipelineStatus(StrEnum):
    READY = "ready"
    READY_WITH_REDACTIONS = "ready_with_redactions"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PipelineResult:
    status: PipelineStatus
    converted: ConverterResult
    normalized_markdown: Path
    document_json: Path
    validation_json: Path
    security_json: Path
    security: SecurityResult

    @property
    def can_send_to_llm(self) -> bool:
        return self.status != PipelineStatus.BLOCKED


class Doc2AgentPipeline:
    """
    Current deterministic MVP pipeline:

    office document
        -> raw Markdown
        -> normalized Markdown
        -> document.json
        -> validation.json
        -> Security Gateway
        -> security.json
        -> readiness decision
    """

    def __init__(
        self,
        converter: Converter | None = None,
        analyzer: StructuralAnalyzer | None = None,
        normalizer: MarkdownNormalizer | None = None,
        validator: DocumentValidator | None = None,
        security_gateway: SecurityGateway | None = None,
    ):
        self.converter = converter or Converter()
        self.analyzer = analyzer or StructuralAnalyzer()
        self.normalizer = normalizer or MarkdownNormalizer()
        self.validator = validator or DocumentValidator()
        self.security_gateway = security_gateway or SecurityGateway()

    def run(
        self,
        source: str | Path,
        output_root: str | Path = "output",
    ) -> PipelineResult:
        output_root = Path(output_root)

        # 1. Office document -> raw Markdown
        converted = self.converter.convert(
            source,
            output_root,
        )

        # 2. Raw Markdown -> normalized Markdown
        raw_markdown = converted.markdown.read_text(
            encoding="utf-8"
        )

        normalized_markdown = self.normalizer.normalize(
            raw_markdown
        )

        normalized_dir = output_root / "normalized"
        normalized_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        normalized_path = (
            normalized_dir
            / converted.markdown.name
        )

        normalized_path.write_text(
            normalized_markdown,
            encoding="utf-8",
        )

        # 3. Normalized Markdown -> document.json
        document = self.analyzer.analyze(
            normalized_path,
            metadata_path=converted.metadata,
        )

        structured_dir = output_root / "structured"

        document_json = (
            structured_dir
            / f"{converted.markdown.stem}.document.json"
        )

        self.analyzer.write_json(
            document,
            document_json,
        )

        # 4. document.json -> validation.json
        report = self.validator.validate(
            document,
            markdown_path=normalized_path,
        )

        validation_dir = output_root / "validation"

        validation_json = (
            validation_dir
            / f"{converted.markdown.stem}.validation.json"
        )

        self.validator.write_json(
            report,
            validation_json,
        )

        # 5. Pydantic model -> JSON-compatible dictionary
        security_document = document.model_dump(
            mode="json",
            exclude_none=True,
        )
        
        # 6. document dictionary -> Security Gateway
        security_result = self.security_gateway.process(
            security_document
        )

        # 7. Save the security report and sanitized document
        security_dir = output_root / "security"
        security_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        security_json = (
            security_dir
            / f"{converted.markdown.stem}.security.json"
        )

        security_json.write_text(
            security_result.model_dump_json(indent=2),
            encoding="utf-8",
        )

        # 8. Determine whether the document may be sent to an LLM
        if security_result.decision == SecurityDecision.BLOCK:
            status = PipelineStatus.BLOCKED
        elif security_result.decision == SecurityDecision.REDACT:
            status = PipelineStatus.READY_WITH_REDACTIONS
        else:
            status = PipelineStatus.READY

        return PipelineResult(
            status=status,
            converted=converted,
            normalized_markdown=normalized_path,
            document_json=document_json,
            validation_json=validation_json,
            security_json=security_json,
            security=security_result,
        )