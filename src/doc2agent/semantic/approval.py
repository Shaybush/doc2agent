from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from doc2agent.artifacts import canonical_json_sha256
from doc2agent.semantic.candidate import KnowledgeCandidate, validate_candidate_contract
from doc2agent.semantic.semantic_analyzer import SemanticAnalyzer


class ApprovalService:
    """Validate, archive, and atomically publish approved knowledge candidates."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    @property
    def approved_dir(self) -> Path:
        return self.storage_root / "approved"

    def document_dir(self, document_id: str) -> Path:
        """Return the isolated approved-state directory for one logical document."""

        if not document_id.strip():
            raise ValueError("Document ID must not be empty")
        readable_id = quote(document_id, safe="-._")
        suffix = sha256(document_id.encode("utf-8")).hexdigest()[:12]
        return self.approved_dir / f"{readable_id}--{suffix}"

    def current_path(self, document_id: str) -> Path:
        return self.document_dir(document_id) / "current.knowledge.json"

    @property
    def log_path(self) -> Path:
        return self.storage_root / "approval-log.jsonl"

    def approve(
        self,
        candidate: KnowledgeCandidate,
        *,
        document_id: str,
        approved_by: str,
    ) -> Path:
        self._require_actor(approved_by)
        self._validate_candidate(candidate)
        archive_path = self._archive_path(document_id, candidate.candidate_sha256)
        payload = candidate.model_dump_json(indent=2, exclude_none=True)
        self._write_if_missing(archive_path, payload)
        current_path = self.current_path(document_id)
        self._atomic_write(current_path, payload)
        self._append_log("approved", candidate, document_id, approved_by)
        return current_path

    def rollback(
        self,
        candidate_sha256: str,
        *,
        document_id: str,
        approved_by: str,
    ) -> Path:
        self._require_actor(approved_by)
        archive_path = self._archive_path(document_id, candidate_sha256)
        if not archive_path.is_file():
            raise ValueError(f"Approved candidate was not found: {candidate_sha256}")
        candidate = KnowledgeCandidate.model_validate_json(
            archive_path.read_text(encoding="utf-8")
        )
        if candidate.candidate_sha256 != candidate_sha256:
            raise ValueError("Approved candidate filename does not match its hash")
        self._validate_candidate(candidate)
        current_path = self.current_path(document_id)
        self._atomic_write(current_path, candidate.model_dump_json(indent=2, exclude_none=True))
        self._append_log("rolled_back", candidate, document_id, approved_by)
        return current_path

    def restore_current_snapshot(
        self,
        document_id: str,
        snapshot: str | None,
    ) -> None:
        """Restore current approval state after a failed coupled publication."""

        current_path = self.current_path(document_id)
        if snapshot is None:
            current_path.unlink(missing_ok=True)
            return
        self._atomic_write(current_path, snapshot)

    def resolve_current_path(self, document_id: str | None = None) -> Path:
        """Resolve one current snapshot, rejecting ambiguous multi-document state."""

        if document_id is not None:
            current_path = self.current_path(document_id)
            if not current_path.is_file():
                raise ValueError(f"No approved knowledge for document: {document_id}")
            return current_path

        current_paths = sorted(self.approved_dir.glob("*/current.knowledge.json"))
        if len(current_paths) == 1:
            return current_paths[0]
        if not current_paths:
            raise ValueError("No approved knowledge exists")
        raise ValueError("Several documents are approved; pass --document-id")

    def _archive_path(self, document_id: str, candidate_sha256: str) -> Path:
        return self.document_dir(document_id) / f"{candidate_sha256}.knowledge.json"

    def require_approved_snapshot(
        self,
        path: Path,
        candidate: KnowledgeCandidate,
        *,
        document_id: str,
    ) -> None:
        """Reject a candidate file that was not published through approval."""

        self._validate_candidate(candidate)
        allowed_paths = {
            self.current_path(document_id),
            self._archive_path(document_id, candidate.candidate_sha256),
        }
        if path.resolve() not in {allowed_path.resolve() for allowed_path in allowed_paths}:
            raise ValueError("Copilot context requires an approved snapshot")

    @staticmethod
    def _require_actor(approved_by: str) -> None:
        if not approved_by.strip():
            raise ValueError("Approval actor must not be empty")

    @staticmethod
    def _validate_candidate(candidate: KnowledgeCandidate) -> None:
        expected_hash = canonical_json_sha256(
            candidate.model_dump(mode="json", exclude={"candidate_sha256"})
        )
        if candidate.candidate_sha256 != expected_hash:
            raise ValueError("Candidate hash does not match candidate content")

        for annotation in candidate.annotations:
            if not annotation.evidence:
                raise ValueError(
                    "Approval requires explicit evidence for every annotation"
                )
            for evidence in annotation.evidence:
                if evidence.support != "explicit":
                    raise ValueError(
                        "Approval requires explicit evidence for every annotation"
                    )
        validate_candidate_contract(candidate)
        SemanticAnalyzer.validate_annotations(candidate.document, candidate.annotations)

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
            existing = KnowledgeCandidate.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            self._validate_candidate(existing)
            if existing != KnowledgeCandidate.model_validate_json(payload):
                raise ValueError("Approved candidate hash is already used by other content")
            return
        self._atomic_write(path, payload)

    def _append_log(
        self,
        event: str,
        candidate: KnowledgeCandidate,
        document_id: str,
        approved_by: str,
    ) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "event": event,
            "candidate_sha256": candidate.candidate_sha256,
            "document_id": document_id,
            "source_id": candidate.source_id,
            "structural_sha256": candidate.structural_sha256,
            "actor": approved_by.strip(),
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        with self.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            log_file.write("\n")
