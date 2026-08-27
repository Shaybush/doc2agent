from __future__ import annotations

import json
from pathlib import Path

from doc2agent.pipeline import Doc2AgentPipeline


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "hebrew_test_document.docx"
)


def test_pipeline_creates_validation_report(
    tmp_path: Path,
) -> None:
    assert FIXTURE.exists(), (
        f"Missing fixture: {FIXTURE}. "
        "Copy hebrew_test_document.docx to tests/fixtures/."
    )

    result = Doc2AgentPipeline().run(
        FIXTURE,
        tmp_path / "output",
    )

    assert result.validation_json.exists()

    report = json.loads(
        result.validation_json.read_text(
            encoding="utf-8"
        )
    )

    assert report["valid"] is True
    assert report["errors"] == []
