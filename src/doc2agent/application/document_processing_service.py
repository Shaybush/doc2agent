from __future__ import annotations

from pathlib import Path

from doc2agent.design import (
    DesignAlignmentApprovalService,
    DesignAlignmentBuilder,
    DesignAlignmentStore,
    DesignBinding,
    FigmaImportRequestStore,
    FigmaSnapshotStore,
)
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
    DesignAlignmentApprovalPreview,
    DesignAlignmentDraftPreview,
    FigmaImportRequestPreview,
    FigmaNodePreview,
    FigmaSnapshotPreview,
    KnowledgeApprovalPreview,
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
                    annotation_id=f.annotation.id,
                    kind=f.annotation.kind,
                    value=f.annotation.value,
                    evidence=[
                        ProofEvidence(
                            block_id=e.block_id,
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

    def approve_knowledge(
        self, result: ProcessingResult, proof: CandidateProof, approved_by: str
    ) -> KnowledgeApprovalPreview:
        try:
            candidate = self._candidate_for_proof(result, proof)
            ApprovalService(self.storage_root).approve(
                candidate, document_id=result.document_id, approved_by=approved_by
            )
            return KnowledgeApprovalPreview(candidate_sha256=candidate.candidate_sha256)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Approval", str(exc)) from exc

    def current_knowledge_approval(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> KnowledgeApprovalPreview | None:
        try:
            candidate = self._candidate_for_proof(result, proof)
            self._require_current_knowledge(result, candidate)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Approval", str(exc)) from exc
        return KnowledgeApprovalPreview(candidate_sha256=candidate.candidate_sha256)

    def approve_design_alignment(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        alignment_sha256: str,
        approved_by: str,
    ) -> DesignAlignmentApprovalPreview:
        try:
            candidate = self._candidate_for_proof(result, proof)
            knowledge_path = self._require_current_knowledge(result, candidate)
            alignment = DesignAlignmentStore(self.storage_root).load(
                result.source_id, alignment_sha256
            )
            snapshot = FigmaSnapshotStore(self.storage_root).load(
                result.source_id, alignment.figma_snapshot_sha256
            )
            DesignAlignmentApprovalService(self.storage_root).approve(
                alignment,
                candidate,
                snapshot,
                document_id=result.document_id,
                approved_by=approved_by,
                knowledge_path=knowledge_path,
            )
            return DesignAlignmentApprovalPreview(
                alignment_sha256=alignment.alignment_sha256,
                figma_snapshot_sha256=alignment.figma_snapshot_sha256,
            )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Approval", str(exc)) from exc

    def current_design_alignment_approval(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> DesignAlignmentApprovalPreview | None:
        try:
            candidate = self._candidate_for_proof(result, proof)
            approved = DesignAlignmentApprovalService(self.storage_root).load_current(
                result.document_id
            )
        except ValueError as exc:
            if str(exc).startswith("No approved design alignment"):
                return None
            raise DocumentProcessingError("Approval", str(exc)) from exc
        except OSError as exc:
            raise DocumentProcessingError("Approval", str(exc)) from exc
        if approved.alignment.candidate_sha256 != candidate.candidate_sha256:
            return None
        return DesignAlignmentApprovalPreview(
            alignment_sha256=approved.alignment.alignment_sha256,
            figma_snapshot_sha256=approved.alignment.figma_snapshot_sha256,
        )

    def publish_copilot_with_design(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> str:
        try:
            candidate = self._candidate_for_proof(result, proof)
            self._require_current_knowledge(result, candidate)
            design_approval = DesignAlignmentApprovalService(self.storage_root).load_current(
                result.document_id
            )
            if design_approval.alignment.candidate_sha256 != candidate.candidate_sha256:
                raise ValueError("Approved Figma mapping belongs to a different candidate")
            output = copilot_context_output_path(candidate, self.storage_root, result.document_id)
            return TopicRenderer().render(
                candidate, output, design_alignment=design_approval
            ).context_path.read_text(encoding="utf-8")
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

    def request_figma_capture(
        self, result: ProcessingResult, figma_url: str
    ) -> FigmaImportRequestPreview:
        """Record an explicit URL hand-off; no browser request fetches Figma."""

        try:
            request = FigmaImportRequestStore(self.storage_root).create(
                result.source_id, figma_url
            )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return FigmaImportRequestPreview(
            request_sha256=request.request_sha256,
            figma_url=request.figma_url,
            requested_at=request.requested_at.isoformat(),
            status=request.status,
            snapshot_sha256=request.snapshot_sha256,
        )

    def list_figma_import_requests(
        self, result: ProcessingResult
    ) -> list[FigmaImportRequestPreview]:
        try:
            requests = FigmaImportRequestStore(self.storage_root).list(result.source_id)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return [
            FigmaImportRequestPreview(
                request_sha256=request.request_sha256,
                figma_url=request.figma_url,
                requested_at=request.requested_at.isoformat(),
                status=request.status,
                snapshot_sha256=request.snapshot_sha256,
            )
            for request in requests
        ]

    def create_design_alignment_draft(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        *,
        snapshot_sha256: str,
        annotation_id: str,
        evidence_block_id: str,
        figma_node_id: str,
        status: str,
    ) -> DesignAlignmentDraftPreview:
        """Create one explicit, not-yet-approved requirement-to-frame decision."""

        try:
            candidate = self._candidate_for_proof(result, proof)
            snapshot = FigmaSnapshotStore(self.storage_root).load(
                result.source_id, snapshot_sha256
            )
            alignment = DesignAlignmentBuilder().build(
                candidate,
                snapshot,
                [
                    DesignBinding(
                        annotation_id=annotation_id,
                        evidence_block_id=evidence_block_id,
                        figma_node_id=figma_node_id,
                        status=status,
                    )
                ],
            )
            DesignAlignmentStore(self.storage_root).write(alignment)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return self._alignment_preview(alignment)

    def list_design_alignment_drafts(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[DesignAlignmentDraftPreview]:
        try:
            alignments = DesignAlignmentStore(self.storage_root).list(result.source_id)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return [
            self._alignment_preview(alignment)
            for alignment in alignments
            if alignment.candidate_sha256 == proof.candidate_sha256
        ]

    def _candidate_for_proof(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> KnowledgeCandidate:
        candidate_path = (
            self.storage_root
            / "artifacts"
            / result.source_id.replace(":", "-")
            / "candidate"
            / f"{proof.candidate_sha256}.knowledge.json"
        )
        candidate = KnowledgeCandidate.model_validate_json(
            candidate_path.read_text(encoding="utf-8")
        )
        if candidate.source_id != result.source_id:
            raise ValueError("Candidate does not belong to this document revision")
        return candidate

    def _require_current_knowledge(
        self, result: ProcessingResult, candidate: KnowledgeCandidate
    ) -> Path:
        current_path = ApprovalService(self.storage_root).current_path(result.document_id)
        approved = KnowledgeCandidate.model_validate_json(current_path.read_text(encoding="utf-8"))
        if approved.candidate_sha256 != candidate.candidate_sha256:
            raise ValueError("Current approved knowledge belongs to a different candidate")
        ApprovalService(self.storage_root).require_approved_snapshot(
            current_path, candidate, document_id=result.document_id
        )
        return current_path

    @staticmethod
    def _alignment_preview(alignment) -> DesignAlignmentDraftPreview:
        binding = alignment.bindings[0]
        if not binding.annotation_id or not binding.evidence_block_id or not binding.figma_node_id:
            raise ValueError("Draft alignment is missing a requirement-to-frame binding")
        return DesignAlignmentDraftPreview(
            alignment_sha256=alignment.alignment_sha256,
            figma_snapshot_sha256=alignment.figma_snapshot_sha256,
            annotation_id=binding.annotation_id,
            evidence_block_id=binding.evidence_block_id,
            figma_node_id=binding.figma_node_id,
            status=binding.status,
        )

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
