"""Atomic storage for reviewable, not-yet-approved design alignments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from doc2agent.design.alignment import DesignAlignmentCandidate


class DesignAlignmentStore:
    """Keep candidate-scoped mappings separate from approved design context."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    def path_for(self, source_id: str, alignment_sha256: str) -> Path:
        return self._directory(source_id) / f"{alignment_sha256}.design-alignment.json"

    def write(self, alignment: DesignAlignmentCandidate) -> Path:
        path = self.path_for(alignment.source_id, alignment.alignment_sha256)
        payload = alignment.model_dump_json(indent=2, exclude_none=True)
        if path.exists():
            existing = DesignAlignmentCandidate.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if existing != alignment:
                raise ValueError("Design alignment hash is already used by other content")
            return path
        self._atomic_write(path, payload)
        return path

    def list(self, source_id: str) -> list[DesignAlignmentCandidate]:
        return [
            DesignAlignmentCandidate.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self._directory(source_id).glob("*.design-alignment.json"))
        ]

    def load(self, source_id: str, alignment_sha256: str) -> DesignAlignmentCandidate:
        path = self.path_for(source_id, alignment_sha256)
        if not path.is_file():
            raise ValueError(f"Design alignment was not found: {alignment_sha256}")
        alignment = DesignAlignmentCandidate.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if alignment.alignment_sha256 != alignment_sha256:
            raise ValueError("Design alignment filename does not match its hash")
        return alignment

    def _directory(self, source_id: str) -> Path:
        if not source_id.startswith("sha256:"):
            raise ValueError("Source ID must be a SHA-256 source identifier")
        return (
            self.storage_root
            / "artifacts"
            / source_id.replace(":", "-")
            / "design-alignment"
        )

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


class DesignAlignmentReviewStore:
    """Reversible exclusions for unapproved mapping drafts."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    def excluded_alignment_hashes(self, source_id: str) -> set[str]:
        path = self._path_for(source_id)
        if not path.is_file():
            return set()
        payload = json.loads(path.read_text(encoding="utf-8"))
        hashes = payload.get("excluded_alignment_sha256s", [])
        if not isinstance(hashes, list) or any(
            not isinstance(value, str) or len(value) != 64 for value in hashes
        ):
            raise ValueError("Figma mapping review exclusions are invalid")
        return set(hashes)

    def exclude(self, source_id: str, alignment_sha256: str) -> None:
        self._write(source_id, self.excluded_alignment_hashes(source_id) | {alignment_sha256})

    def _write(self, source_id: str, hashes: set[str]) -> None:
        if any(len(value) != 64 for value in hashes):
            raise ValueError("Figma mapping hash must be a SHA-256 value")
        path = self._path_for(source_id)
        DesignAlignmentStore._atomic_write(
            path,
            json.dumps({"excluded_alignment_sha256s": sorted(hashes)}, indent=2) + "\n",
        )

    def _path_for(self, source_id: str) -> Path:
        return (
            self.storage_root
            / "artifacts"
            / source_id.replace(":", "-")
            / "design-alignment-review"
            / "excluded-mappings.json"
        )
