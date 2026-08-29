from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.models.document import AnalyzedDocument, Section
from doc2agent.semantic.models import SemanticAnnotation, SemanticKind, SemanticOrigin
from doc2agent.semantic.providers import FixtureSemanticProvider, SemanticProvider
from doc2agent.semantic.semantic_analyzer import SemanticAnalyzer


class SemanticEvaluationCase(BaseModel):
    """Expected annotation and its unique source text in a fixture document."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: SemanticKind
    value: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    origin: SemanticOrigin


class SemanticEvaluationReport(BaseModel):
    """Exact-match report for a semantic provider against fixture cases."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    expected_count: int
    matched_case_ids: list[str]
    missing_case_ids: list[str]
    unexpected_annotation_ids: list[str]
    passed: bool


class SemanticEvaluationRunner:
    """Evaluate a provider without connecting it to the runtime pipeline."""

    def __init__(
        self,
        structural_analyzer: StructuralAnalyzer | None = None,
    ) -> None:
        self.structural_analyzer = structural_analyzer or StructuralAnalyzer()

    def run(
        self,
        markdown_path: str | Path,
        cases_path: str | Path,
        *,
        provider: SemanticProvider | None = None,
    ) -> SemanticEvaluationReport:
        document = self.structural_analyzer.analyze(markdown_path)
        cases = self._load_cases(cases_path)
        expected = [
            self._annotation_from_case(document, case)
            for case in cases
        ]
        evaluation_provider = provider or FixtureSemanticProvider(
            annotations=tuple(expected),
        )
        actual = SemanticAnalyzer(evaluation_provider).analyze(document)

        unmatched_cases = list(zip(cases, expected, strict=True))
        matched_case_ids: list[str] = []
        unexpected_annotation_ids: list[str] = []

        for annotation in actual.annotations:
            match_index = next(
                (
                    index
                    for index, (_, expected_annotation) in enumerate(
                        unmatched_cases
                    )
                    if self._annotation_key(annotation)
                    == self._annotation_key(expected_annotation)
                ),
                None,
            )

            if match_index is None:
                unexpected_annotation_ids.append(annotation.id)
                continue

            case, _ = unmatched_cases.pop(match_index)
            matched_case_ids.append(case.id)

        missing_case_ids = [
            case.id
            for case, _ in unmatched_cases
        ]

        return SemanticEvaluationReport(
            provider=actual.provider,
            expected_count=len(cases),
            matched_case_ids=matched_case_ids,
            missing_case_ids=missing_case_ids,
            unexpected_annotation_ids=unexpected_annotation_ids,
            passed=not missing_case_ids and not unexpected_annotation_ids,
        )

    @staticmethod
    def write_json(
        report: SemanticEvaluationReport,
        output_path: str | Path,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            report.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return output_path

    @staticmethod
    def _load_cases(
        cases_path: str | Path,
    ) -> list[SemanticEvaluationCase]:
        raw_cases = json.loads(
            Path(cases_path).read_text(encoding="utf-8")
        )
        return [
            SemanticEvaluationCase.model_validate(case)
            for case in raw_cases
        ]

    @staticmethod
    def _annotation_from_case(
        document: AnalyzedDocument,
        case: SemanticEvaluationCase,
    ) -> SemanticAnnotation:
        return SemanticAnnotation(
            id=f"evaluation:{case.id}",
            kind=case.kind,
            value=case.value,
            source_ids=[
                SemanticEvaluationRunner._source_id_by_text(
                    document,
                    case.source_text,
                )
            ],
            origin=case.origin,
        )

    @staticmethod
    def _source_id_by_text(
        document: AnalyzedDocument,
        source_text: str,
    ) -> str:
        blocks = list(document.preamble)

        def collect(section: Section) -> None:
            blocks.extend(section.blocks)

            for child in section.children:
                collect(child)

        for section in document.sections:
            collect(section)

        matching_blocks = [
            block
            for block in blocks
            if block.text == source_text
        ]

        if len(matching_blocks) != 1:
            raise ValueError(
                "Evaluation source text must match exactly one content block: "
                f"{source_text}"
            )

        return matching_blocks[0].id

    @staticmethod
    def _annotation_key(
        annotation: SemanticAnnotation,
    ) -> tuple[str, str, tuple[str, ...], str]:
        return (
            annotation.kind,
            annotation.value,
            tuple(sorted(annotation.source_ids)),
            annotation.origin,
        )
