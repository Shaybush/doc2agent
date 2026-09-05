from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.models.document import AnalyzedDocument, ContentBlock, Section
from doc2agent.semantic.candidate import KnowledgeCandidate, candidate_contract_report
from doc2agent.semantic.copilot_context import TopicRenderer
from doc2agent.semantic.enrichment_patch import (
    EnrichmentPatch,
    EnrichmentPatchValidator,
    PatchFact,
    PatchValidationIssue,
)
from doc2agent.semantic.models import (
    COPILOT_CONTEXT_KINDS,
    COPILOT_CONTEXT_LABELS,
    EvidenceRef,
    SemanticAnnotation,
    SemanticKind,
    SemanticOrigin,
)
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


class GoldenRequiredFact(BaseModel):
    """One human-reviewed v2 fact tied to exact structural evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: SemanticKind
    value: str = Field(min_length=1)
    evidence_text: str | None = Field(default=None, min_length=1)
    evidence_row: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def requires_one_evidence_selector(self) -> GoldenRequiredFact:
        if (self.evidence_text is None) == (self.evidence_row is None):
            raise ValueError(
                "Golden facts require exactly one of evidence_text or evidence_row"
            )
        return self


class EnrichmentGoldenFixture(BaseModel):
    """Checked-in, human-reviewed required facts for one source fixture."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    required_facts: list[GoldenRequiredFact] = Field(min_length=1)
    forbidden_value_fragments: list[str] = Field(default_factory=list)


class EnrichmentGoldenReport(BaseModel):
    """Deterministic exact-match report for a v2 patch or candidate."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    required_fact_count: int
    matched_required_fact_ids: list[str]
    missing_required_fact_ids: list[str]
    unexpected_fact_ids: list[str]
    forbidden_fact_ids: list[str]
    invalid_evidence_fact_ids: list[str]
    invalid_contract_codes: list[str]
    markdown_mismatch_codes: list[str]
    passed: bool


class EnrichmentGoldenRunner:
    """Evaluate reviewed v2 facts without a provider, network, or secrets."""

    def evaluate_patch(
        self,
        patch: EnrichmentPatch,
        document: AnalyzedDocument,
        fixture_path: str | Path,
    ) -> EnrichmentGoldenReport:
        fixture = self._load_fixture(fixture_path)
        validation = EnrichmentPatchValidator().validate(patch, document)
        invalid_evidence_fact_ids = self._patch_invalid_evidence_ids(
            patch,
            validation.errors,
        )
        return self._evaluate(
            subject="patch",
            facts=patch.facts,
            document=document,
            fixture=fixture,
            invalid_evidence_fact_ids=invalid_evidence_fact_ids,
            invalid_contract_codes=[issue.code for issue in validation.errors],
        )

    def evaluate_candidate(
        self,
        candidate: KnowledgeCandidate,
        fixture_path: str | Path,
        *,
        markdown: str | None = None,
    ) -> EnrichmentGoldenReport:
        fixture = self._load_fixture(fixture_path)
        invalid_evidence_fact_ids, invalid_contract_codes = (
            self._candidate_contract_issues(candidate)
        )
        report = self._evaluate(
            subject="candidate",
            facts=candidate.annotations,
            document=candidate.document,
            fixture=fixture,
            invalid_evidence_fact_ids=invalid_evidence_fact_ids,
            invalid_contract_codes=invalid_contract_codes,
        )
        if markdown is None:
            return report

        markdown_mismatch_codes = list(report.markdown_mismatch_codes)
        if markdown != self._expected_markdown(candidate):
            markdown_mismatch_codes.append("COPILOT_MARKDOWN_MISMATCH")
        return report.model_copy(
            update={
                "markdown_mismatch_codes": markdown_mismatch_codes,
                "passed": report.passed and not markdown_mismatch_codes,
            }
        )

    @staticmethod
    def _load_fixture(path: str | Path) -> EnrichmentGoldenFixture:
        return EnrichmentGoldenFixture.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )

    def _evaluate(
        self,
        *,
        subject: str,
        facts: list[PatchFact] | list[SemanticAnnotation],
        document: AnalyzedDocument,
        fixture: EnrichmentGoldenFixture,
        invalid_evidence_fact_ids: list[str],
        invalid_contract_codes: list[str],
    ) -> EnrichmentGoldenReport:
        expected = [
            (fact, self._expected_evidence(fact, document))
            for fact in fixture.required_facts
        ]
        unmatched = list(expected)
        matched_required_fact_ids: list[str] = []
        unexpected_fact_ids: list[str] = []

        for fact in facts:
            index = next(
                (
                    item_index
                    for item_index, (required, evidence) in enumerate(unmatched)
                    if self._fact_matches(fact, required, evidence)
                ),
                None,
            )
            if index is None:
                unexpected_fact_ids.append(fact.id)
                continue
            required, _ = unmatched.pop(index)
            matched_required_fact_ids.append(required.id)

        forbidden_fact_ids = [
            fact.id
            for fact in facts
            if any(
                fragment.casefold() in fact.value.casefold()
                for fragment in fixture.forbidden_value_fragments
            )
        ]
        missing_required_fact_ids = [required.id for required, _ in unmatched]
        passed = not (
            missing_required_fact_ids
            or unexpected_fact_ids
            or forbidden_fact_ids
            or invalid_evidence_fact_ids
            or invalid_contract_codes
        )
        return EnrichmentGoldenReport(
            subject=subject,
            required_fact_count=len(fixture.required_facts),
            matched_required_fact_ids=matched_required_fact_ids,
            missing_required_fact_ids=missing_required_fact_ids,
            unexpected_fact_ids=unexpected_fact_ids,
            forbidden_fact_ids=forbidden_fact_ids,
            invalid_evidence_fact_ids=invalid_evidence_fact_ids,
            invalid_contract_codes=invalid_contract_codes,
            markdown_mismatch_codes=[],
            passed=passed,
        )

    @staticmethod
    def _expected_evidence(
        required: GoldenRequiredFact,
        document: AnalyzedDocument,
    ) -> EvidenceRef:
        if document.source_manifest is None or not document.structural_sha256:
            raise ValueError("Golden evaluation requires document provenance")
        matches = []
        for block_id, block in EnrichmentGoldenRunner._blocks_by_id(document).items():
            if required.evidence_text is not None:
                matches_selector = (
                    block.text == required.evidence_text
                    or required.evidence_text in block.items
                    or required.evidence_text in block.headers
                )
            else:
                matches_selector = required.evidence_row in block.rows
            if matches_selector:
                matches.append(block_id)
        if len(matches) != 1:
            raise ValueError(
                f"Golden evidence selector for {required.id} matched {len(matches)} blocks"
            )
        return EvidenceRef(
            source_id=document.source_manifest.source_id,
            block_id=matches[0],
            structural_sha256=document.structural_sha256,
            support="explicit",
        )

    @staticmethod
    def _blocks_by_id(document: AnalyzedDocument) -> dict[str, ContentBlock]:
        blocks = {block.id: block for block in document.preamble}

        def collect(section: Section) -> None:
            blocks.update({block.id: block for block in section.blocks})
            for child in section.children:
                collect(child)

        for section in document.sections:
            collect(section)
        return blocks

    @staticmethod
    def _fact_matches(
        fact: PatchFact | SemanticAnnotation,
        required: GoldenRequiredFact,
        evidence: EvidenceRef,
    ) -> bool:
        return (
            fact.kind == required.kind
            and fact.value == required.value
            and len(fact.evidence) == 1
            and fact.evidence[0] == evidence
        )

    @staticmethod
    def _patch_invalid_evidence_ids(
        patch: EnrichmentPatch,
        errors: list[PatchValidationIssue],
    ) -> list[str]:
        result = []
        for index, fact in enumerate(patch.facts):
            prefix = f"facts[{index}]."
            if any(
                issue.path is not None
                and issue.path.startswith(prefix)
                and (
                    issue.code.startswith("EVIDENCE_")
                    or issue.code == "FACT_VALUE_NOT_IN_EVIDENCE"
                )
                for issue in errors
            ):
                result.append(fact.id)
        return result

    @staticmethod
    def _candidate_contract_issues(
        candidate: KnowledgeCandidate,
    ) -> tuple[list[str], list[str]]:
        report = candidate_contract_report(candidate)
        invalid_fact_indexes = {
            index
            for index in range(len(candidate.annotations))
            if any(
                issue.path is not None and issue.path.startswith(f"facts[{index}].")
                for issue in report.errors
            )
        }
        return (
            [candidate.annotations[index].id for index in sorted(invalid_fact_indexes)],
            [issue.code for issue in report.errors],
        )

    @staticmethod
    def _expected_markdown(
        candidate: KnowledgeCandidate,
    ) -> str:
        lines = [
            "<!-- GENERATED — DO NOT EDIT -->",
            f"<!-- approved_candidate_sha256: {candidate.candidate_sha256} -->",
            f"<!-- source_id: {candidate.source_id} -->",
            "",
            "# Approved knowledge",
            "",
        ]
        for kind in COPILOT_CONTEXT_KINDS:
            facts = sorted(
                (fact for fact in candidate.annotations if fact.kind == kind),
                key=lambda fact: (fact.kind, fact.id, fact.value),
            )
            if not facts:
                continue
            lines.extend([f"## {COPILOT_CONTEXT_LABELS[kind]}", ""])
            for fact in facts:
                lines.extend(TopicRenderer.fact_markdown_lines(fact))
            lines.append("")
        return "\n".join(lines)
