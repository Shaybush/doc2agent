"""Immutable on-disk storage for normalized Figma snapshots."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from doc2agent.design.alignment import FigmaSnapshot


class FigmaSnapshotStore:
    """Store a snapshot under the immutable source revision it was reviewed with."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    def path_for(self, source_id: str, snapshot_sha256: str) -> Path:
        source_key = self._source_key(source_id)
        return self.storage_root / "artifacts" / source_key / "figma" / (
            f"{snapshot_sha256}.figma.json"
        )

    def write(self, source_id: str, snapshot: FigmaSnapshot) -> Path:
        path = self.path_for(source_id, snapshot.snapshot_sha256)
        payload = snapshot.model_dump_json(indent=2, exclude_none=True)
        if path.exists():
            existing = FigmaSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != snapshot:
                raise ValueError("Figma snapshot hash is already used by other content")
            return path
        self._atomic_write(path, payload)
        return path

    def load(self, source_id: str, snapshot_sha256: str) -> FigmaSnapshot:
        path = self.path_for(source_id, snapshot_sha256)
        if not path.is_file():
            raise ValueError(f"Figma snapshot was not found: {snapshot_sha256}")
        return FigmaSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, source_id: str) -> list[FigmaSnapshot]:
        """Return snapshots already imported for exactly one source revision."""

        source_key = self._source_key(source_id)
        directory = self.storage_root / "artifacts" / source_key / "figma"
        return [
            FigmaSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.figma.json"))
        ]

    @staticmethod
    def _source_key(source_id: str) -> str:
        if not source_id.startswith("sha256:"):
            raise ValueError("Source ID must be a SHA-256 source identifier")
        return source_id.replace(":", "-")

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
