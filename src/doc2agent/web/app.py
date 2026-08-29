from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from doc2agent.application import DocumentProcessingService
from doc2agent.ingest import DocxPreflightLimits

from .routes import ProcessedArtifactStore, create_router


def create_app(
    service: DocumentProcessingService | None = None,
    max_upload_bytes: int | None = None,
) -> FastAPI:
    app = FastAPI(title="Doc2Agent")
    app.state.processing_service = service or DocumentProcessingService()
    app.state.processed_artifacts = ProcessedArtifactStore()
    app.state.max_upload_bytes = (
        max_upload_bytes or DocxPreflightLimits().max_uncompressed_bytes
    )
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(create_router())
    return app


app = create_app()
