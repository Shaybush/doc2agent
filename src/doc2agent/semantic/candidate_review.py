from __future__ import annotations

from dataclasses import dataclass

from doc2agent.artifacts import canonical_json_sha256
from doc2agent.models.document import AnalyzedDocument, ContentBlock, Section
from doc2agent.semantic.candidate import KnowledgeCandidate, validate_candidate_contract
from doc2agent.semantic.models import SemanticAnnotation
from doc2agent.semantic.semantic_analyzer import SemanticAnalyzer


@dataclass(frozen=True)
class ReviewedEvidence:
    support: str
    section_path: tuple[str, ...]
    start_line: int
    end_line: int
    source_text: str


@dataclass(frozen=True)
class ReviewedFact:
    annotation: SemanticAnnotation
    evidence: tuple[ReviewedEvidence, ...]


@dataclass(frozen=True)
class CandidateReview:
    candidate_sha256: str
    source_id: str
    structural_sha256: str
    reviewed_block_ids: tuple[str, ...]
    facts: tuple[ReviewedFact, ...]


class CandidateReviewer:
    """Create a human-readable, evidence-first view of a candidate."""

    def review(self, candidate: KnowledgeCandidate) -> CandidateReview:
        self._validate_candidate(candidate)
        blocks = self.blocks_by_id(candidate.document)
        facts = tuple(
            ReviewedFact(
                annotation=annotation,
                evidence=tuple(
                    ReviewedEvidence(
                        support=evidence.support,
                        section_path=blocks[evidence.block_id][0],
                        start_line=blocks[evidence.block_id][1].start_line,
                        end_line=blocks[evidence.block_id][1].end_line,
                        source_text=self._block_text(blocks[evidence.block_id][1]),
                    )
                    for evidence in annotation.evidence
                ),
            )
            for annotation in candidate.annotations
        )
        return CandidateReview(
            candidate_sha256=candidate.candidate_sha256,
            source_id=candidate.source_id,
            structural_sha256=candidate.structural_sha256,
            reviewed_block_ids=tuple(candidate.reviewed_block_ids),
            facts=facts,
        )

    @staticmethod
    def render(review: CandidateReview) -> str:
        lines = [
            f"Candidate hash: {review.candidate_sha256}",
            f"Source: {review.source_id}",
            "Reviewed content blocks: " + ", ".join(review.reviewed_block_ids),
            f"Facts: {len(review.facts)}",
        ]
        for index, fact in enumerate(review.facts, start=1):
            lines.extend(
                [
                    "",
                    f"{index}. [{fact.annotation.kind}] {fact.annotation.value}",
                ]
            )
            for evidence in fact.evidence:
                location = " / ".join(evidence.section_path) or "Preamble"
                lines.extend(
                    [
                        f"   Source ({evidence.support}): {location}, "
                        f"lines {evidence.start_line}-{evidence.end_line}",
                        f"   {evidence.source_text}",
                    ]
                )
        return "\n".join(lines)

    @staticmethod
    def _validate_candidate(candidate: KnowledgeCandidate) -> None:
        expected_hash = canonical_json_sha256(
            candidate.model_dump(mode="json", exclude={"candidate_sha256"})
        )
        if candidate.candidate_sha256 != expected_hash:
            raise ValueError("Candidate hash does not match candidate content")
        validate_candidate_contract(candidate)
        SemanticAnalyzer.validate_annotations(candidate.document, candidate.annotations)

    @staticmethod
    def blocks_by_id(
        document: AnalyzedDocument,
    ) -> dict[str, tuple[tuple[str, ...], ContentBlock]]:
        blocks = {((), block.id): block for block in document.preamble}

        def collect(section: Section) -> None:
            for block in section.blocks:
                blocks[(tuple(section.path), block.id)] = block
            for child in section.children:
                collect(child)

        for section in document.sections:
            collect(section)
        return {block_id: (path, block) for (path, block_id), block in blocks.items()}

    @staticmethod
    def _block_text(block: ContentBlock) -> str:
        if block.text:
            return block.text
        if block.items:
            return "; ".join(block.items)
        if block.headers or block.rows:
            rows = [" | ".join(block.headers)] + [" | ".join(row) for row in block.rows]
            return " / ".join(rows)
        if block.alt or block.src:
            return f"Image: {block.alt or ''} {block.src or ''}".strip()
        return "[Empty source block]"
