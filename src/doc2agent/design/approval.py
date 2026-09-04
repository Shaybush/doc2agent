"""Atomic approval storage for design alignment records."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from doc2agent.design.alignment import (
    ApprovedDesignAlignment,
    DesignAlignmentBuilder,
    DesignAlignmentCandidate,
    FigmaSnapshot,
)
from doc2agent.semantic.approval import ApprovalService
from doc2agent.semantic.candidate import KnowledgeCandidate


class DesignAlignmentApprovalService:
    """Approve design mappings only for an already approved knowledge snapshot."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    @property
    def approved_dir(self) -> Path:
        return self.storage_root / "design-approved"

    def document_dir(self, document_id: str) -> Path:
        if not document_id.strip():
            raise ValueError("Document ID must not be empty")
        readable_id = quote(document_id, safe="-._")
        suffix = sha256(document_id.encode("utf-8")).hexdigest()[:12]
        return self.approved_dir / f"{readable_id}--{suffix}"

    def current_path(self, document_id: str) -> Path:
        return self.document_dir(document_id) / "current.design-alignment.json"

    def approve(
        self,
        alignment: DesignAlignmentCandidate,
        knowledge: KnowledgeCandidate,
        snapshot: FigmaSnapshot,
        *,
        document_id: str,
        approved_by: str,
        knowledge_path: Path,
        approved_at: datetime | None = None,
    ) -> Path:
        if not approved_by.strip():
            raise ValueError("Approval actor must not be empty")
        ApprovalService(self.storage_root).require_approved_snapshot(
            knowledge_path,
            knowledge,
            document_id=document_id,
        )
        self._validate(alignment, knowledge, snapshot)
        approved = ApprovedDesignAlignment(
            document_id=document_id,
            alignment=alignment,
            snapshot=snapshot,
            approved_by=approved_by.strip(),
            approved_at=approved_at or datetime.now(UTC),
        )
        payload = approved.model_dump_json(indent=2, exclude_none=True)
        archive_path = self.document_dir(document_id) / (
            f"{alignment.alignment_sha256}.design-alignment.json"
        )
        self._write_if_missing(archive_path, payload)
        current_path = self.current_path(document_id)
        self._atomic_write(current_path, payload)
        return current_path

    def load_current(self, document_id: str) -> ApprovedDesignAlignment:
        path = self.current_path(document_id)
        if not path.is_file():
            raise ValueError(f"No approved design alignment for document: {document_id}")
        return ApprovedDesignAlignment.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _validate(
        alignment: DesignAlignmentCandidate,
        knowledge: KnowledgeCandidate,
        snapshot: FigmaSnapshot,
    ) -> None:
        if alignment.source_id != knowledge.source_id:
            raise ValueError("Design alignment belongs to a different source")
        if alignment.structural_sha256 != knowledge.structural_sha256:
            raise ValueError("Design alignment belongs to a different structural revision")
        if alignment.candidate_sha256 != knowledge.candidate_sha256:
            raise ValueError("Design alignment belongs to a different knowledge candidate")
        if alignment.figma_snapshot_sha256 != snapshot.snapshot_sha256:
            raise ValueError("Design alignment belongs to a different Figma snapshot")
        DesignAlignmentBuilder._validate_bindings(knowledge, snapshot, alignment.bindings)

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_text(payload, encoding="utf-8")
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _write_if_missing(self, path: Path, payload: str) -> None:
        if path.exists():
            existing = ApprovedDesignAlignment.model_validate_json(path.read_text(encoding="utf-8"))
            candidate = ApprovedDesignAlignment.model_validate_json(payload)
            if existing != candidate:
                raise ValueError("Design alignment hash is already used by other content")
            return
        self._atomic_write(path, payload)
