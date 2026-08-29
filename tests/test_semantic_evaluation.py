from __future__ import annotations

from pathlib import Path

from doc2agent.semantic import NoOpSemanticProvider, SemanticEvaluationRunner

EVALUATION_MARKDOWN = (
    Path(__file__).parent
    / "fixtures"
    / "semantic-evaluation.md"
)
EVALUATION_CASES = (
    Path(__file__).parent
    / "fixtures"
    / "semantic-evaluation-cases.json"
)


def test_fixture_evaluation_passes_all_cases(tmp_path: Path) -> None:
    runner = SemanticEvaluationRunner()

    report = runner.run(EVALUATION_MARKDOWN, EVALUATION_CASES)
    output_path = runner.write_json(
        report,
        tmp_path / "report.json",
    )

    assert report.provider == "fixture"
    assert report.expected_count == 9
    assert len(report.matched_case_ids) == 9
    assert report.missing_case_ids == []
    assert report.unexpected_annotation_ids == []
    assert report.passed is True
    assert output_path.exists()


def test_noop_provider_fails_for_missing_annotations() -> None:
    report = SemanticEvaluationRunner().run(
        EVALUATION_MARKDOWN,
        EVALUATION_CASES,
        provider=NoOpSemanticProvider(),
    )

    assert report.provider == "noop"
    assert report.matched_case_ids == []
    assert len(report.missing_case_ids) == 9
    assert report.passed is False
