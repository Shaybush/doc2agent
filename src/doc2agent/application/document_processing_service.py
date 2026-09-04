from __future__ import annotations

from pathlib import Path

from doc2agent.design import FigmaSnapshotStore
from doc2agent.ingest import IngestService
from doc2agent.pipeline import Doc2AgentPipeline, PipelineStatus
from doc2agent.security.models import SecurityDecision
from doc2agent.semantic import (
    ApprovalService,
    CandidateBuilder,
    CandidateReviewer,
    KnowledgeCandidate,
    OpenAICompatibleEnrichmentProvider,
    TopicRenderer,
    copilot_context_output_path,
    deepseek_settings_from_environment,
)
from doc2agent.validator import ValidationReport

from .models import (
    CandidateProof,
    FigmaNodePreview,
    FigmaSnapshotPreview,
    ProcessingResult,
    ProofEvidence,
    ProofFact,
)


class DocumentProcessingError(Exception):
    """A safe error description that the web layer may present to a user."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


class DocumentProcessingService:
    """Orchestrate the existing safe intake and deterministic pipeline."""

    def __init__(
        self,
        storage_root: str | Path = "storage",
        ingest_service: IngestService | None = None,
        pipeline: Doc2AgentPipeline | None = None,
    ) -> None:
        self.storage_root = Path(storage_root)
        self.ingest_service = ingest_service or IngestService(self.storage_root)
        self.pipeline = pipeline or Doc2AgentPipeline()

    def process(
        self,
        source: Path,
        *,
        document_id: str | None = None,
        approve_review: bool = False,
    ) -> ProcessingResult:
        ingested = self.ingest_service.ingest(
            source,
            document_id=document_id,
            approve_review=approve_review,
        )
        if not ingested.accepted or ingested.source_path is None:
            diagnostics = (
                "; ".join(diagnostic.message for diagnostic in ingested.diagnostics)
                or "The document could not be accepted safely."
            )
            raise DocumentProcessingError("Intake", diagnostics)

        source_key = ingested.manifest.source_id.replace(":", "-")
        artifact_root = self.storage_root / "artifacts" / source_key

        try:
            pipeline_result = self.pipeline.run(ingested, artifact_root)
            self.ingest_service.record_artifact_root(
                ingested.manifest.source_id,
                artifact_root,
            )
        except DocumentProcessingError:
            raise
        except Exception as exc:
            raise DocumentProcessingError("Pipeline", "Unable to process the document.") from exc

        if pipeline_result.status == PipelineStatus.BLOCKED:
            raise DocumentProcessingError(
                "Security", "The document was blocked by the security gateway."
            )

        markdown: str | None = None
        if pipeline_result.security.decision == SecurityDecision.PASS:
            markdown = pipeline_result.normalized_markdown.read_text(encoding="utf-8")

        return ProcessingResult(
            filename=ingested.manifest.filename,
            document_id=ingested.document_id or "",
            source_id=ingested.manifest.source_id,
            status=pipeline_result.status.value,
            markdown=markdown,
            document_json=pipeline_result.security.sanitized_document,
            validation_errors=[issue.message for issue in pipeline_result.validation.errors],
            security_findings=len(pipeline_result.security.findings),
        )

    def generate_candidate(self, result: ProcessingResult) -> CandidateProof:
        document_path = self._artifact_path(result.source_id, "structured", ".document.json")
        validation_path = self._artifact_path(result.source_id, "validation", ".validation.json")
        security_path = self._artifact_path(result.source_id, "security", ".security.json")
        from doc2agent.models.document import AnalyzedDocument
        from doc2agent.security import SecurityResult

        document = AnalyzedDocument.model_validate_json(document_path.read_text(encoding="utf-8"))
        validation = ValidationReport.model_validate_json(
            validation_path.read_text(encoding="utf-8")
        )
        security = SecurityResult.model_validate_json(security_path.read_text(encoding="utf-8"))
        if not validation.valid:
            raise DocumentProcessingError(
                "Validation", "Document validation must pass before enrichment."
            )
        if security.decision == SecurityDecision.BLOCK:
            raise DocumentProcessingError(
                "Security", "Security review blocked this document from model use."
            )
        sanitized_document = AnalyzedDocument.model_validate(security.sanitized_document)
        if (
            document.source_manifest != sanitized_document.source_manifest
            or document.structural_sha256 != sanitized_document.structural_sha256
        ):
            raise DocumentProcessingError(
                "Security", "Security artifact belongs to a different document revision."
            )

        settings = deepseek_settings_from_environment()
        patch = OpenAICompatibleEnrichmentProvider(settings).create_patch(
            sanitized_document
        )
        candidate = CandidateBuilder().build(patch, sanitized_document)
        candidate_path = (
            document_path.parent.parent
            / "candidate"
            / f"{candidate.candidate_sha256}.knowledge.json"
        )
        CandidateBuilder.write_json(candidate, candidate_path)
        review = CandidateReviewer().review(candidate)
        return CandidateProof(
            candidate_sha256=review.candidate_sha256,
            reviewed_block_ids=list(review.reviewed_block_ids),
            facts=[
                ProofFact(
                    kind=f.annotation.kind,
                    value=f.annotation.value,
                    evidence=[
                        ProofEvidence(
                            support=e.support,
                            section_path=list(e.section_path),
                            start_line=e.start_line,
                            end_line=e.end_line,
                            source_text=e.source_text,
                        )
                        for e in f.evidence
                    ],
                )
                for f in review.facts
            ],
        )

    def approve_and_publish(
        self, result: ProcessingResult, proof: CandidateProof, approved_by: str
    ) -> str:
        candidate_path = (
            self.storage_root
            / "artifacts"
            / result.source_id.replace(":", "-")
            / "candidate"
            / f"{proof.candidate_sha256}.knowledge.json"
        )
        try:
            candidate = KnowledgeCandidate.model_validate_json(
                candidate_path.read_text(encoding="utf-8")
            )
            if candidate.source_id != result.source_id:
                raise ValueError("Candidate does not belong to this document revision")
            ApprovalService(self.storage_root).approve(
                candidate, document_id=result.document_id, approved_by=approved_by
            )
            output = copilot_context_output_path(candidate, self.storage_root, result.document_id)
            return (
                TopicRenderer().render(candidate, output).context_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Approval", str(exc)) from exc

    def list_figma_snapshots(
        self, result: ProcessingResult
    ) -> list[FigmaSnapshotPreview]:
        """Expose already imported immutable snapshots without fetching Figma."""

        try:
            snapshots = FigmaSnapshotStore(self.storage_root).list(result.source_id)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return [
            FigmaSnapshotPreview(
                snapshot_sha256=snapshot.snapshot_sha256,
                figma_url=snapshot.figma_url,
                captured_at=snapshot.captured_at.isoformat(),
                nodes=[
                    FigmaNodePreview(
                        node_id=node.node_id,
                        name=node.name,
                        kind=node.kind,
                        figma_url=node.figma_url,
                    )
                    for node in snapshot.nodes
                ],
            )
            for snapshot in snapshots
        ]

    def _artifact_path(self, source_id: str, directory: str, suffix: str) -> Path:
        paths = sorted(
            (self.storage_root / "artifacts" / source_id.replace(":", "-") / directory).glob(
                f"*{suffix}"
            )
        )
        if len(paths) != 1:
            raise DocumentProcessingError(
                "Artifacts", f"Expected one {directory} artifact for this source revision."
            )
        return paths[0]
