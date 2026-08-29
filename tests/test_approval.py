from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc2agent import cli
from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.artifacts import canonical_json_sha256
from doc2agent.semantic import (
    ApprovalService,
    CandidateBuilder,
    CandidateReviewer,
    EnrichmentPatch,
    EnrichmentPatchValidator,
    EvidenceRef,
    PatchFact,
    PipelineStep,
)
from doc2agent.source_manifest import SourceManifest

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.md"
DOCUMENT_ID = "document:test"


def _document():
    return StructuralAnalyzer().analyze(
        SAMPLE,
        source_manifest=SourceManifest.from_path(SAMPLE),
    )


def _candidate(*, value: str = "General English paragraph."):
    document = _document()
    block_id = document.sections[0].blocks[0].id
    patch = EnrichmentPatch(
        schema_version="2.0",
        source_id=document.source_manifest.source_id,
        structural_sha256=document.structural_sha256,
        scope_block_ids=[block_id],
        reviewed_block_ids=sorted(EnrichmentPatchValidator._block_ids(document)),
        facts=[
            PatchFact(
                id="fact:requirement",
                kind="requirement",
                value=value,
                evidence=[
                    EvidenceRef(
                        source_id=document.source_manifest.source_id,
                        block_id=block_id,
                        structural_sha256=document.structural_sha256,
                        support="explicit",
                    )
                ],
            )
        ],
    )
    return CandidateBuilder().build(patch, document)


def _self_hash(candidate):
    return candidate.model_copy(
        update={
            "candidate_sha256": canonical_json_sha256(
                candidate.model_dump(mode="json", exclude={"candidate_sha256"})
            )
        }
    )


def test_approve_archives_and_publishes_candidate_atomically(tmp_path: Path) -> None:
    candidate = _candidate()
    service = ApprovalService(tmp_path / "storage")

    current_path = service.approve(
        candidate, document_id=DOCUMENT_ID, approved_by="reviewer"
    )

    assert current_path == service.current_path(DOCUMENT_ID)
    assert current_path.exists()
    archived = service.document_dir(DOCUMENT_ID) / f"{candidate.candidate_sha256}.knowledge.json"
    assert archived.exists()
    current = json.loads(current_path.read_text(encoding="utf-8"))
    assert current["candidate_sha256"] == candidate.candidate_sha256
    assert current["schema_version"] == "2.0"
    assert current["reviewed_block_ids"] == candidate.reviewed_block_ids
    entries = [
        json.loads(line)
        for line in (tmp_path / "storage/approval-log.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert entries[0]["event"] == "approved"
    assert entries[0]["actor"] == "reviewer"


def test_approval_rejects_tampered_candidate_without_changing_current(tmp_path: Path) -> None:
    first = _candidate()
    service = ApprovalService(tmp_path / "storage")
    service.approve(first, document_id=DOCUMENT_ID, approved_by="reviewer")
    before = service.current_path(DOCUMENT_ID).read_text(encoding="utf-8")
    tampered = first.model_copy(update={"source_id": "sha256:" + "0" * 64})

    with pytest.raises(ValueError, match="Candidate hash"):
        service.approve(tampered, document_id=DOCUMENT_ID, approved_by="reviewer")

    assert service.current_path(DOCUMENT_ID).read_text(encoding="utf-8") == before


def test_candidate_review_and_approval_reject_self_hashed_invalid_review_coverage(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    invalid = _self_hash(candidate.model_copy(update={"reviewed_block_ids": []}))

    with pytest.raises(ValueError, match="Candidate violates enrichment contract"):
        CandidateReviewer().review(invalid)
    with pytest.raises(ValueError, match="Candidate violates enrichment contract"):
        ApprovalService(tmp_path / "storage").approve(
            invalid,
            document_id=DOCUMENT_ID,
            approved_by="reviewer",
        )


def test_candidate_review_and_approval_reject_self_hashed_invented_pipeline_step(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    invalid = candidate.model_copy(
        update={
            "annotations": [
                candidate.annotations[0].model_copy(
                    update={
                        "kind": "architecture_pipeline",
                        "pipeline_step": PipelineStep(
                            component="Invented",
                            input="DOCX",
                            output="Markdown",
                        )
                    }
                )
            ]
        }
    )
    invalid = _self_hash(invalid)

    with pytest.raises(ValueError, match="PIPELINE_STEP_NOT_IN_EVIDENCE_ROW"):
        CandidateReviewer().review(invalid)
    with pytest.raises(ValueError, match="PIPELINE_STEP_NOT_IN_EVIDENCE_ROW"):
        ApprovalService(tmp_path / "storage").approve(
            invalid,
            document_id=DOCUMENT_ID,
            approved_by="reviewer",
        )


def test_approval_rejects_inferred_evidence(tmp_path: Path) -> None:
    candidate = _candidate()
    inferred = candidate.model_copy(
        update={
            "annotations": [
                candidate.annotations[0].model_copy(
                    update={
                        "evidence": [
                            candidate.annotations[0].evidence[0].model_copy(
                                update={"support": "inferred"}
                            )
                        ]
                    }
                )
            ]
        }
    )
    inferred = inferred.model_copy(
        update={
            "candidate_sha256": canonical_json_sha256(
                inferred.model_dump(mode="json", exclude={"candidate_sha256"})
            )
        }
    )

    with pytest.raises(ValueError, match="explicit evidence"):
        ApprovalService(tmp_path / "storage").approve(
            inferred, document_id=DOCUMENT_ID, approved_by="reviewer"
        )


def test_approval_rejects_annotation_without_evidence(tmp_path: Path) -> None:
    candidate = _candidate()
    without_evidence = candidate.model_copy(
        update={
            "annotations": [
                candidate.annotations[0].model_copy(
                    update={"evidence": [], "source_ids": [candidate.document.document.id]}
                )
            ]
        }
    )
    without_evidence = without_evidence.model_copy(
        update={
            "candidate_sha256": canonical_json_sha256(
                without_evidence.model_dump(
                    mode="json",
                    exclude={"candidate_sha256"},
                )
            )
        }
    )

    with pytest.raises(ValueError, match="explicit evidence"):
        ApprovalService(tmp_path / "storage").approve(
            without_evidence,
            document_id=DOCUMENT_ID,
            approved_by="reviewer",
        )


def test_rollback_restores_an_archived_approved_candidate(tmp_path: Path) -> None:
    first = _candidate(value="General English")
    second = _candidate(value="English paragraph.")
    service = ApprovalService(tmp_path / "storage")
    service.approve(first, document_id=DOCUMENT_ID, approved_by="reviewer")
    service.approve(second, document_id=DOCUMENT_ID, approved_by="reviewer")

    service.rollback(
        first.candidate_sha256, document_id=DOCUMENT_ID, approved_by="reviewer"
    )

    current = json.loads(service.current_path(DOCUMENT_ID).read_text(encoding="utf-8"))
    assert current["candidate_sha256"] == first.candidate_sha256
    events = [
        json.loads(line)["event"]
        for line in service.log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["approved", "approved", "rolled_back"]


def test_approve_cli_publishes_candidate(tmp_path: Path) -> None:
    candidate = _candidate()
    candidate_path = tmp_path / "candidate.knowledge.json"
    candidate_path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "approve",
            str(candidate_path),
            "--storage",
            str(tmp_path / "storage"),
            "--document-id",
            DOCUMENT_ID,
            "--by",
            "reviewer",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Approved knowledge.json:" in result.output


def test_approved_current_snapshots_are_isolated_by_document(tmp_path: Path) -> None:
    service = ApprovalService(tmp_path / "storage")
    first = _candidate(value="General English")
    second = _candidate(value="English paragraph.")

    service.approve(first, document_id="document:first", approved_by="reviewer")
    second_current = service.approve(
        second, document_id="document:second", approved_by="reviewer"
    )

    first_current = service.current_path("document:first")
    first_payload = json.loads(first_current.read_text(encoding="utf-8"))
    second_payload = json.loads(second_current.read_text(encoding="utf-8"))
    assert first_payload["candidate_sha256"] == first.candidate_sha256
    assert second_payload["candidate_sha256"] == second.candidate_sha256
    with pytest.raises(ValueError, match="Several documents"):
        service.resolve_current_path()
