"""Explicit hand-off from browser review to a separately configured Figma capture."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doc2agent.artifacts import canonical_json_sha256
from doc2agent.design.alignment import FigmaSnapshot, validate_figma_url


def figma_design_file_key(figma_url: str) -> str:
    """Return the design file key from a canonical Figma design URL."""

    validate_figma_url(figma_url)
    parts = [part for part in urlparse(figma_url).path.split("/") if part]
    if len(parts) < 2 or parts[0] != "design":
        raise ValueError("Figma URL must point to a /design/<file-key> file")
    return parts[1]


class FigmaImportRequest(BaseModel):
    """A source-revision-scoped request; it is not a Figma snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    figma_url: str = Field(min_length=1)
    requested_at: datetime
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "captured"] = "pending"
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> FigmaImportRequest:
        figma_design_file_key(self.figma_url)
        expected = canonical_json_sha256(
            {"source_id": self.source_id, "figma_url": self.figma_url}
        )
        if self.request_sha256 != expected:
            raise ValueError("Figma import request hash does not match request identity")
        if self.status == "pending" and self.snapshot_sha256 is not None:
            raise ValueError("Pending Figma import request must not have a snapshot")
        if self.status == "captured" and self.snapshot_sha256 is None:
            raise ValueError("Captured Figma import request requires a snapshot hash")
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

    def list_pending(self, source_id: str | None = None) -> list[FigmaImportRequest]:
        """Return only work explicitly handed off to a capture adapter."""

        if source_id is not None:
            requests = self.list(source_id)
        else:
            requests = [
                FigmaImportRequest.model_validate_json(path.read_text(encoding="utf-8"))
                for path in sorted(
                    (self.storage_root / "figma-import-requests").glob(
                        "*/*.figma-import-request.json"
                    )
                )
            ]
        return [request for request in requests if request.status == "pending"]

    def assert_can_fulfill(
        self,
        source_id: str,
        request_sha256: str,
        snapshot: FigmaSnapshot,
    ) -> FigmaImportRequest:
        request = self._load(source_id, request_sha256)
        if snapshot.figma_file_key != figma_design_file_key(request.figma_url):
            raise ValueError("Figma snapshot belongs to a different Figma file")
        if request.status == "captured" and request.snapshot_sha256 != snapshot.snapshot_sha256:
            raise ValueError("Figma import request was already captured with another snapshot")
        return request

    def fulfill(
        self,
        source_id: str,
        request_sha256: str,
        snapshot: FigmaSnapshot,
    ) -> FigmaImportRequest:
        request = self.assert_can_fulfill(source_id, request_sha256, snapshot)
        if request.status == "captured":
            return request
        captured = request.model_copy(
            update={"status": "captured", "snapshot_sha256": snapshot.snapshot_sha256}
        )
        self._atomic_write(
            self.path_for(source_id, request_sha256),
            captured.model_dump_json(indent=2),
        )
        return captured

    def path_for(self, source_id: str, request_sha256: str) -> Path:
        return self._directory(source_id) / f"{request_sha256}.figma-import-request.json"

    def _load(self, source_id: str, request_sha256: str) -> FigmaImportRequest:
        path = self.path_for(source_id, request_sha256)
        if not path.is_file():
            raise ValueError(f"Figma import request was not found: {request_sha256}")
        return FigmaImportRequest.model_validate_json(path.read_text(encoding="utf-8"))

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
