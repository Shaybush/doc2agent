"""Explicit hand-off from browser review to a separately configured Figma capture."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doc2agent.artifacts import canonical_json_sha256
from doc2agent.design.alignment import validate_figma_url


class FigmaImportRequest(BaseModel):
    """A source-revision-scoped request; it is not a Figma snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    figma_url: str = Field(min_length=1)
    requested_at: datetime
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> FigmaImportRequest:
        validate_figma_url(self.figma_url)
        expected = canonical_json_sha256(
            {"source_id": self.source_id, "figma_url": self.figma_url}
        )
        if self.request_sha256 != expected:
            raise ValueError("Figma import request hash does not match request identity")
        return self

    @classmethod
    def build(
        cls,
        *,
        source_id: str,
        figma_url: str,
        requested_at: datetime | None = None,
    ) -> FigmaImportRequest:
        request_sha256 = canonical_json_sha256(
            {"source_id": source_id, "figma_url": figma_url}
        )
        return cls(
            source_id=source_id,
            figma_url=figma_url,
            requested_at=requested_at or datetime.now(UTC),
            request_sha256=request_sha256,
        )


class FigmaImportRequestStore:
    """Persist pending capture requests separately from immutable snapshots."""

    def __init__(self, storage_root: str | Path = "storage") -> None:
        self.storage_root = Path(storage_root)

    def create(self, source_id: str, figma_url: str) -> FigmaImportRequest:
        request = FigmaImportRequest.build(source_id=source_id, figma_url=figma_url)
        path = self.path_for(source_id, request.request_sha256)
        if path.exists():
            return FigmaImportRequest.model_validate_json(path.read_text(encoding="utf-8"))
        self._atomic_write(path, request.model_dump_json(indent=2))
        return request

    def list(self, source_id: str) -> list[FigmaImportRequest]:
        directory = self._directory(source_id)
        return [
            FigmaImportRequest.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.figma-import-request.json"))
        ]

    def path_for(self, source_id: str, request_sha256: str) -> Path:
        return self._directory(source_id) / f"{request_sha256}.figma-import-request.json"

    def _directory(self, source_id: str) -> Path:
        if not source_id.startswith("sha256:"):
            raise ValueError("Source ID must be a SHA-256 source identifier")
        return self.storage_root / "figma-import-requests" / source_id.replace(":", "-")

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
