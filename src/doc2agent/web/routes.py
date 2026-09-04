from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates

from doc2agent.application import (
    CandidateProof,
    DocumentProcessingError,
    ProcessingResult,
)
from doc2agent.converter import safe_name

logger = logging.getLogger(__name__)

ALLOWED_UPLOAD_EXTENSIONS = {".doc", ".docx"}
SESSION_COOKIE = "doc2agent_web_session"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False}


class ProcessedArtifactStore:
    """In-memory download payloads keyed by server-generated IDs only."""

    def __init__(self) -> None:
        self._items: dict[str, ProcessingResult] = {}
        self._document_ids: dict[tuple[str, str], str] = {}
        self._candidates: dict[str, CandidateProof] = {}
        self._lock = Lock()

    def add(self, result: ProcessingResult) -> str:
        artifact_id = str(uuid4())
        with self._lock:
            self._items[artifact_id] = result
        return artifact_id

    def get(self, artifact_id: str) -> ProcessingResult | None:
        with self._lock:
            return self._items.get(artifact_id)

    def document_id_for(self, session_id: str, filename: str) -> str | None:
        with self._lock:
            return self._document_ids.get((session_id, filename))

    def remember_document(self, session_id: str, result: ProcessingResult) -> None:
        with self._lock:
            self._document_ids[(session_id, result.filename)] = result.document_id

    def candidate(self, artifact_id: str) -> CandidateProof | None:
        with self._lock:
            return self._candidates.get(artifact_id)

    def set_candidate(self, artifact_id: str, candidate: CandidateProof) -> None:
        with self._lock:
            self._candidates[artifact_id] = candidate

    def replace(self, artifact_id: str, result: ProcessingResult) -> None:
        with self._lock:
            self._items[artifact_id] = result


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @router.post("/documents/process")
    async def process_document(
        request: Request,
        file: UploadFile,
        approve_review: bool = Form(False),
    ):
        filename = _safe_upload_name(file.filename)
        if filename is None:
            return _error_response(
                request,
                "Upload",
                "Choose a .doc or .docx file.",
            )

        service = request.app.state.processing_service
        session_id = request.cookies.get(SESSION_COOKIE) or str(uuid4())
        artifacts = request.app.state.processed_artifacts
        try:
            with TemporaryDirectory(prefix="doc2agent-upload-") as temporary_directory:
                upload_path = Path(temporary_directory) / filename
                await _save_upload(file, upload_path, request.app.state.max_upload_bytes)
                result = service.process(
                    upload_path,
                    document_id=artifacts.document_id_for(session_id, filename),
                    approve_review=approve_review,
                )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        except Exception:
            logger.exception("Unexpected document processing failure")
            return _error_response(
                request,
                "Processing",
                "The document could not be processed. Check the server logs for details.",
            )
        finally:
            await file.close()

        artifacts.remember_document(session_id, result)
        artifact_id = artifacts.add(result)
        response = templates.TemplateResponse(
            request=request,
            name="partials/result.html",
            context={"result": result, "artifact_id": artifact_id},
        )
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.post("/documents/{artifact_id}/candidate")
    def generate_candidate(artifact_id: str, request: Request):
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        if result is None:
            return _error_response(request, "Candidate", "Processing result was not found.")
        try:
            proof = request.app.state.processing_service.generate_candidate(result)
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        except Exception:
            logger.exception("Candidate generation failed")
            return _error_response(
                request, "Candidate", "Could not generate a candidate. Check server logs."
            )
        artifacts.set_candidate(artifact_id, proof)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/figma-import-request")
    async def request_figma_import(artifact_id: str, request: Request):
        form = await request.form()
        figma_url = str(form.get("figma_url", ""))
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Figma", "Generate and review a candidate first.")
        try:
            request.app.state.processing_service.request_figma_capture(result, figma_url)
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/design-alignment")
    async def create_design_alignment(artifact_id: str, request: Request):
        form = await request.form()
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Figma", "Generate and review a candidate first.")
        try:
            annotation_id, evidence_block_id = _binding_fields(form)
            request.app.state.processing_service.create_design_alignment_draft(
                result,
                proof,
                snapshot_sha256=str(form.get("snapshot_sha256", "")),
                annotation_id=annotation_id,
                evidence_block_id=evidence_block_id,
                figma_node_id=str(form.get("figma_node_id", "")),
                status=str(form.get("status", "")),
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/approve")
    async def approve_candidate(artifact_id: str, request: Request):
        form = await request.form()
        approved_by = str(form.get("approved_by", ""))
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Approval", "Generate and review a candidate first.")
        try:
            final_markdown = request.app.state.processing_service.approve_and_publish(
                result, proof, approved_by
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        updated_result = result.model_copy(
            update={"published_copilot_markdown": final_markdown}
        )
        artifacts.replace(artifact_id, updated_result)
        return templates.TemplateResponse(
            request=request,
            name="partials/result.html",
            context={"artifact_id": artifact_id, "result": updated_result},
        )

    @router.post("/documents/{artifact_id}/approve-and-publish-figma")
    async def approve_and_publish_figma(artifact_id: str, request: Request):
        form = await request.form()
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Approval", "Generate and review a candidate first.")
        try:
            final_markdown = request.app.state.processing_service.approve_and_publish_with_figma(
                result, proof, str(form.get("approved_by", ""))
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        updated_result = result.model_copy(
            update={"published_copilot_markdown": final_markdown}
        )
        artifacts.replace(artifact_id, updated_result)
        return templates.TemplateResponse(
            request=request,
            name="partials/result.html",
            context={"artifact_id": artifact_id, "result": updated_result},
        )

    @router.post("/documents/{artifact_id}/design-alignment-package")
    async def create_design_alignment_package(artifact_id: str, request: Request):
        form = await request.form()
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Figma", "Generate and review a candidate first.")
        try:
            request.app.state.processing_service.create_design_alignment_package(
                result, proof, [str(value) for value in form.getlist("alignment_sha256")]
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/reuse-design-alignment")
    async def reuse_design_alignment(artifact_id: str, request: Request):
        form = await request.form()
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Figma", "Generate and review a candidate first.")
        try:
            request.app.state.processing_service.reuse_design_alignment_draft(
                result, proof, str(form.get("alignment_sha256", ""))
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/approve-knowledge")
    async def approve_knowledge(artifact_id: str, request: Request):
        form = await request.form()
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Approval", "Generate and review a candidate first.")
        try:
            request.app.state.processing_service.approve_knowledge(
                result, proof, str(form.get("approved_by", ""))
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/approve-design-alignment")
    async def approve_design_alignment(artifact_id: str, request: Request):
        form = await request.form()
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Approval", "Generate and review a candidate first.")
        try:
            request.app.state.processing_service.approve_design_alignment(
                result,
                proof,
                str(form.get("alignment_sha256", "")),
                str(form.get("approved_by", "")),
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        return _candidate_response(request, artifact_id, result, proof)

    @router.post("/documents/{artifact_id}/publish-copilot-with-design")
    def publish_copilot_with_design(artifact_id: str, request: Request):
        artifacts = request.app.state.processed_artifacts
        result = artifacts.get(artifact_id)
        proof = artifacts.candidate(artifact_id)
        if result is None or proof is None:
            return _error_response(request, "Approval", "Generate and review a candidate first.")
        try:
            final_markdown = request.app.state.processing_service.publish_copilot_with_design(
                result, proof
            )
        except DocumentProcessingError as exc:
            return _error_response(request, exc.stage, exc.reason)
        updated_result = result.model_copy(
            update={"published_copilot_markdown": final_markdown}
        )
        artifacts.replace(artifact_id, updated_result)
        return templates.TemplateResponse(
            request=request,
            name="partials/result.html",
            context={"artifact_id": artifact_id, "result": updated_result},
        )

    @router.get("/documents/{artifact_id}/markdown")
    def download_markdown(artifact_id: str, request: Request):
        result = request.app.state.processed_artifacts.get(artifact_id)
        if result is None or result.markdown is None:
            return PlainTextResponse("Markdown artifact not found.", status_code=404)
        return PlainTextResponse(
            result.markdown,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_download_stem(result.filename)}.md"'
                )
            },
        )

    @router.get("/documents/{artifact_id}/copilot-markdown")
    def download_copilot_markdown(artifact_id: str, request: Request):
        result = request.app.state.processed_artifacts.get(artifact_id)
        if result is None or result.published_copilot_markdown is None:
            return PlainTextResponse("Published Copilot Markdown not found.", status_code=404)
        return PlainTextResponse(
            result.published_copilot_markdown,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_download_stem(result.filename)}.copilot.md"'
                )
            },
        )

    @router.get("/documents/{artifact_id}/json")
    def download_json(artifact_id: str, request: Request):
        result = request.app.state.processed_artifacts.get(artifact_id)
        if result is None:
            return PlainTextResponse("JSON artifact not found.", status_code=404)
        return PlainTextResponse(
            json.dumps(result.document_json, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_download_stem(result.filename)}.json"'
                )
            },
        )

    return router


def _safe_upload_name(filename: str | None) -> str | None:
    if not filename:
        return None
    original_name = PurePosixPath(filename.replace("\\", "/")).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        return None
    return f"{safe_name(Path(original_name).stem)}{suffix}"


async def _save_upload(
    file: UploadFile,
    destination: Path,
    max_upload_bytes: int,
) -> None:
    size = 0
    with destination.open("wb") as target:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_upload_bytes:
                raise DocumentProcessingError(
                    "Upload",
                    "File exceeds the configured upload limit.",
                )
            target.write(chunk)


def _download_stem(filename: str) -> str:
    return safe_name(Path(filename).stem)


def _binding_fields(form) -> tuple[str, str]:
    """Accept one UI-selected requirement/evidence pair or the legacy fields."""

    choice = form.get("binding_choice")
    if choice is None:
        return str(form.get("annotation_id", "")), str(form.get("evidence_block_id", ""))
    try:
        annotation_id, evidence_block_id = json.loads(str(choice))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DocumentProcessingError("Figma", "Choose one requirement and its evidence.") from exc
    if not (
        isinstance(annotation_id, str)
        and annotation_id
        and isinstance(evidence_block_id, str)
        and evidence_block_id
    ):
        raise DocumentProcessingError("Figma", "Choose one requirement and its evidence.")
    return annotation_id, evidence_block_id


def _candidate_response(
    request: Request,
    artifact_id: str,
    result: ProcessingResult,
    proof: CandidateProof,
):
    try:
        service = request.app.state.processing_service
        figma_snapshots = service.list_figma_snapshots(result)
        figma_import_requests = service.list_figma_import_requests(result)
        design_alignment_drafts = service.list_design_alignment_drafts(result, proof)
    except DocumentProcessingError as exc:
        return _error_response(request, exc.stage, exc.reason)
    return templates.TemplateResponse(
        request=request,
        name="partials/candidate.html",
        context={
            "artifact_id": artifact_id,
            "proof": proof,
            "figma_snapshots": figma_snapshots,
            "figma_import_requests": figma_import_requests,
            "design_alignment_drafts": design_alignment_drafts,
        },
    )


def _error_response(request: Request, stage: str, reason: str):
    return templates.TemplateResponse(
        request=request,
        name="partials/error.html",
        context={"stage": stage, "reason": reason},
    )
