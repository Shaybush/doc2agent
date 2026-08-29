from __future__ import annotations

import hashlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SourceManifest(BaseModel):
    """Immutable identity and intake metadata for one source file."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    source_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    filename: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    mime_type: str = Field(min_length=1)
    received_at: datetime

    @classmethod
    def from_path(
        cls,
        source: str | Path,
        *,
        received_at: datetime | None = None,
    ) -> SourceManifest:
        path = Path(source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        mime_type, _ = mimetypes.guess_type(path.name)

        return cls(
            source_id=f"sha256:{digest}",
            filename=path.name,
            byte_size=path.stat().st_size,
            mime_type=mime_type or "application/octet-stream",
            received_at=received_at or datetime.now(UTC),
        )
