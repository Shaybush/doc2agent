from __future__ import annotations

from pathlib import Path

from doc2agent.design import (
    DesignAlignmentApprovalService,
    DesignAlignmentBuilder,
    DesignAlignmentReviewStore,
    DesignAlignmentStore,
    DesignBinding,
    FigmaImportRequestStore,
    FigmaSnapshotReviewStore,
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
    DesignAlignmentBindingPreview,
    DesignAlignmentDraftPreview,
    FigmaFrameMappingPreview,
    FigmaImportRequestPreview,
    FigmaNodePreview,
    FigmaSnapshotPreview,
    KnowledgeApprovalPreview,
    ProcessingResult,
    ProofEvidence,
    ProofFact,
    UnmappedFigmaNodePreview,
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
        except ValueError as exc:
            if str(exc) == "Current approved knowledge belongs to a different candidate":
                return None
            raise DocumentProcessingError("Approval", str(exc)) from exc
        except OSError as exc:
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

    def approve_and_publish_with_figma(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        approved_by: str,
        figma_snapshot_sha256: str,
    ) -> str:
        """Approve one candidate with mappings from one explicitly selected snapshot."""

        try:
            candidate = self._candidate_for_proof(result, proof)
            excluded = FigmaSnapshotReviewStore(
                self.storage_root
            ).excluded_snapshot_hashes(result.source_id)
            excluded_alignments = DesignAlignmentReviewStore(
                self.storage_root
            ).excluded_alignment_hashes(result.source_id)
            if figma_snapshot_sha256 in excluded:
                raise ValueError("Figma snapshot is excluded from the current review")
            drafts = [
                alignment
                for alignment in DesignAlignmentStore(self.storage_root).list(result.source_id)
                if (
                    alignment.candidate_sha256 == candidate.candidate_sha256
                    and len(alignment.bindings) == 1
                    and alignment.figma_snapshot_sha256 == figma_snapshot_sha256
                    and alignment.alignment_sha256 not in excluded_alignments
                )
            ]
            if not drafts:
                raise ValueError(
                    "Choose a Figma snapshot with at least one mapping before saving Markdown"
                )
            annotation_ids = [
                binding.annotation_id
                for draft in drafts
                for binding in draft.bindings
                if binding.annotation_id is not None
            ]
            duplicates = sorted(
                annotation_id
                for annotation_id in set(annotation_ids)
                if annotation_ids.count(annotation_id) > 1
            )
            if duplicates:
                raise ValueError(
                    "Each requirement can map to one frame per Figma snapshot. "
                    "Remove the duplicate mapping: " + ", ".join(duplicates)
                )
            snapshot = FigmaSnapshotStore(self.storage_root).load(
                result.source_id, figma_snapshot_sha256
            )
            unmapped_node_ids = self._unmapped_node_ids(snapshot, drafts)
            if unmapped_node_ids:
                raise ValueError(
                    "Map every frame in the selected Figma snapshot before saving Markdown: "
                    + ", ".join(sorted(unmapped_node_ids))
                )
            alignment = DesignAlignmentBuilder().build(
                candidate,
                snapshot,
                [
                    binding
                    for draft in sorted(drafts, key=lambda draft: draft.alignment_sha256)
                    for binding in draft.bindings
                ],
            )
            DesignAlignmentStore(self.storage_root).write(alignment)
            approval = ApprovalService(self.storage_root)
            knowledge_path = approval.approve(
                candidate, document_id=result.document_id, approved_by=approved_by
            )
            design_approval = DesignAlignmentApprovalService(self.storage_root)
            design_approval.approve(
                alignment,
                candidate,
                snapshot,
                document_id=result.document_id,
                approved_by=approved_by,
                knowledge_path=knowledge_path,
            )
            output = copilot_context_output_path(candidate, self.storage_root, result.document_id)
            return TopicRenderer().render(
                candidate,
                output,
                design_alignment=design_approval.load_current(result.document_id),
            ).context_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Approval", str(exc)) from exc

    def list_figma_snapshots(
        self, result: ProcessingResult
    ) -> list[FigmaSnapshotPreview]:
        """Expose already imported immutable snapshots without fetching Figma."""

        try:
            snapshots = FigmaSnapshotStore(self.storage_root).list(result.source_id)
            excluded = FigmaSnapshotReviewStore(
                self.storage_root
            ).excluded_snapshot_hashes(result.source_id)
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
            if snapshot.snapshot_sha256 not in excluded
        ]

    def list_excluded_figma_snapshots(
        self, result: ProcessingResult
    ) -> list[FigmaSnapshotPreview]:
        """Expose excluded snapshots only for an explicit restore action."""

        try:
            snapshots = FigmaSnapshotStore(self.storage_root).list(result.source_id)
            excluded = FigmaSnapshotReviewStore(
                self.storage_root
            ).excluded_snapshot_hashes(result.source_id)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return [
            FigmaSnapshotPreview(
                snapshot_sha256=snapshot.snapshot_sha256,
                figma_url=snapshot.figma_url,
                captured_at=snapshot.captured_at.isoformat(),
                nodes=[],
            )
            for snapshot in snapshots
            if snapshot.snapshot_sha256 in excluded
        ]

    def exclude_figma_snapshot(self, result: ProcessingResult, snapshot_sha256: str) -> None:
        """Remove one snapshot from the active review without deleting it."""

        try:
            FigmaSnapshotStore(self.storage_root).load(result.source_id, snapshot_sha256)
            FigmaSnapshotReviewStore(self.storage_root).exclude(
                result.source_id, snapshot_sha256
            )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc

    def restore_figma_snapshot(self, result: ProcessingResult, snapshot_sha256: str) -> None:
        """Return one previously excluded snapshot to the active review."""

        try:
            FigmaSnapshotStore(self.storage_root).load(result.source_id, snapshot_sha256)
            FigmaSnapshotReviewStore(self.storage_root).restore(
                result.source_id, snapshot_sha256
            )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc

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
            excluded = FigmaSnapshotReviewStore(
                self.storage_root
            ).excluded_snapshot_hashes(result.source_id)
            excluded_alignments = DesignAlignmentReviewStore(
                self.storage_root
            ).excluded_alignment_hashes(result.source_id)
            if snapshot_sha256 in excluded:
                raise ValueError("Figma snapshot is excluded from the current review")
            existing = [
                alignment
                for alignment in DesignAlignmentStore(self.storage_root).list(result.source_id)
                if (
                    alignment.candidate_sha256 == candidate.candidate_sha256
                    and alignment.figma_snapshot_sha256 == snapshot_sha256
                    and alignment.alignment_sha256 not in excluded_alignments
                    and any(
                        binding.annotation_id == annotation_id
                        for binding in alignment.bindings
                    )
                )
            ]
            if existing:
                raise ValueError(
                    "This requirement is already mapped in the selected Figma snapshot. "
                    "Remove that mapping before choosing another frame."
                )
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
            excluded_alignments = DesignAlignmentReviewStore(
                self.storage_root
            ).excluded_alignment_hashes(result.source_id)
            excluded = FigmaSnapshotReviewStore(
                self.storage_root
            ).excluded_snapshot_hashes(result.source_id)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return [
            self._alignment_preview(alignment)
            for alignment in alignments
            if (
                alignment.candidate_sha256 == proof.candidate_sha256
                and alignment.figma_snapshot_sha256 not in excluded
                and alignment.alignment_sha256 not in excluded_alignments
            )
        ]

    def list_figma_frame_mappings(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[FigmaFrameMappingPreview]:
        facts = {fact.annotation_id: fact for fact in proof.facts}
        mappings: list[FigmaFrameMappingPreview] = []
        for draft in self.list_design_alignment_drafts(result, proof):
            for binding in draft.bindings:
                fact = facts.get(binding.annotation_id or "")
                if fact is None or binding.figma_node_id is None:
                    continue
                mappings.append(
                    FigmaFrameMappingPreview(
                        alignment_sha256=draft.alignment_sha256,
                        snapshot_sha256=draft.figma_snapshot_sha256,
                        figma_node_id=binding.figma_node_id,
                        annotation_id=fact.annotation_id,
                        requirement=f"{fact.kind}: {fact.value}",
                    )
                )
        return mappings

    def exclude_figma_mapping(self, result: ProcessingResult, alignment_sha256: str) -> None:
        try:
            DesignAlignmentStore(self.storage_root).load(result.source_id, alignment_sha256)
            DesignAlignmentReviewStore(self.storage_root).exclude(
                result.source_id, alignment_sha256
            )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc

    def list_unmapped_figma_nodes(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[UnmappedFigmaNodePreview]:
        """Show selected frames that still need an explicit mapping decision."""

        try:
            candidate = self._candidate_for_proof(result, proof)
            excluded = FigmaSnapshotReviewStore(
                self.storage_root
            ).excluded_snapshot_hashes(result.source_id)
            excluded_alignments = DesignAlignmentReviewStore(
                self.storage_root
            ).excluded_alignment_hashes(result.source_id)
            drafts = [
                alignment
                for alignment in DesignAlignmentStore(self.storage_root).list(result.source_id)
                if (
                    alignment.candidate_sha256 == candidate.candidate_sha256
                    and len(alignment.bindings) == 1
                    and alignment.figma_snapshot_sha256 not in excluded
                    and alignment.alignment_sha256 not in excluded_alignments
                )
            ]
            by_snapshot: dict[str, list] = {}
            for draft in drafts:
                by_snapshot.setdefault(draft.figma_snapshot_sha256, []).append(draft)
            unmapped: list[UnmappedFigmaNodePreview] = []
            for snapshot_sha256, snapshot_drafts in by_snapshot.items():
                snapshot = FigmaSnapshotStore(self.storage_root).load(
                    result.source_id, snapshot_sha256
                )
                for node_id in self._unmapped_node_ids(snapshot, snapshot_drafts):
                    node = next(node for node in snapshot.nodes if node.node_id == node_id)
                    unmapped.append(
                        UnmappedFigmaNodePreview(
                            snapshot_sha256=snapshot_sha256,
                            node_id=node.node_id,
                            name=node.name,
                        )
                    )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return unmapped

    def list_reusable_design_alignment_drafts(
        self, result: ProcessingResult, proof: CandidateProof
    ) -> list[DesignAlignmentDraftPreview]:
        """Offer only older drafts whose bindings still validate for this candidate."""

        try:
            candidate = self._candidate_for_proof(result, proof)
            store = DesignAlignmentStore(self.storage_root)
            reusable: list[DesignAlignmentDraftPreview] = []
            for alignment in store.list(result.source_id):
                if (
                    alignment.structural_sha256 != candidate.structural_sha256
                    or alignment.candidate_sha256 == candidate.candidate_sha256
                ):
                    continue
                snapshot = FigmaSnapshotStore(self.storage_root).load(
                    result.source_id, alignment.figma_snapshot_sha256
                )
                try:
                    DesignAlignmentBuilder().build(candidate, snapshot, alignment.bindings)
                except ValueError:
                    continue
                reusable.append(
                    self._alignment_preview(alignment).model_copy(
                        update={"source_candidate_sha256": alignment.candidate_sha256}
                    )
                )
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return reusable

    def reuse_design_alignment_draft(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        alignment_sha256: str,
    ) -> DesignAlignmentDraftPreview:
        """Copy a still-valid draft to the current candidate without approving it."""

        try:
            candidate = self._candidate_for_proof(result, proof)
            store = DesignAlignmentStore(self.storage_root)
            existing = store.load(result.source_id, alignment_sha256)
            if existing.candidate_sha256 == candidate.candidate_sha256:
                raise ValueError("Mapping draft already belongs to the current candidate")
            if existing.structural_sha256 != candidate.structural_sha256:
                raise ValueError("Mapping draft belongs to a different document revision")
            snapshot = FigmaSnapshotStore(self.storage_root).load(
                result.source_id, existing.figma_snapshot_sha256
            )
            copied = DesignAlignmentBuilder().build(candidate, snapshot, existing.bindings)
            store.write(copied)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return self._alignment_preview(copied)

    def create_design_alignment_package(
        self,
        result: ProcessingResult,
        proof: CandidateProof,
        alignment_sha256s: list[str],
    ) -> DesignAlignmentDraftPreview:
        """Combine reviewed single mappings from one immutable Figma snapshot."""

        selected_hashes = sorted(alignment_sha256s)
        if len(selected_hashes) < 2:
            raise DocumentProcessingError(
                "Figma", "Choose at least two mapping drafts to create a review package."
            )
        if len(set(selected_hashes)) != len(selected_hashes):
            raise DocumentProcessingError("Figma", "Choose each mapping draft only once.")
        try:
            candidate = self._candidate_for_proof(result, proof)
            store = DesignAlignmentStore(self.storage_root)
            drafts = [
                store.load(result.source_id, alignment_hash)
                for alignment_hash in selected_hashes
            ]
            snapshot_hashes = {draft.figma_snapshot_sha256 for draft in drafts}
            if len(snapshot_hashes) != 1:
                raise ValueError(
                    "A review package can contain mappings from only one Figma snapshot."
                )
            for draft in drafts:
                if (
                    draft.source_id != candidate.source_id
                    or draft.structural_sha256 != candidate.structural_sha256
                    or draft.candidate_sha256 != candidate.candidate_sha256
                ):
                    raise ValueError("A selected mapping draft belongs to a different candidate")
            snapshot = FigmaSnapshotStore(self.storage_root).load(
                result.source_id, next(iter(snapshot_hashes))
            )
            package = DesignAlignmentBuilder().build(
                candidate,
                snapshot,
                [binding for draft in drafts for binding in draft.bindings],
            )
            DesignAlignmentStore(self.storage_root).write(package)
        except (OSError, ValueError) as exc:
            raise DocumentProcessingError("Figma", str(exc)) from exc
        return self._alignment_preview(package)

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
    def _unmapped_node_ids(snapshot, drafts) -> set[str]:
        mapped_node_ids = {
            binding.figma_node_id
            for draft in drafts
            for binding in draft.bindings
            if binding.figma_node_id is not None
        }
        return {node.node_id for node in snapshot.nodes} - mapped_node_ids

    @staticmethod
    def _alignment_preview(alignment) -> DesignAlignmentDraftPreview:
        return DesignAlignmentDraftPreview(
            alignment_sha256=alignment.alignment_sha256,
            figma_snapshot_sha256=alignment.figma_snapshot_sha256,
            bindings=[
                DesignAlignmentBindingPreview(
                    annotation_id=binding.annotation_id,
                    evidence_block_id=binding.evidence_block_id,
                    figma_node_id=binding.figma_node_id,
                    status=binding.status,
                )
                for binding in alignment.bindings
            ],
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
